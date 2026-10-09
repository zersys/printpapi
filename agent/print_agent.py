# printpapi — self-hosted PrintNode alternative. Elastic License 2.0 (see LICENSE).
import configparser
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

_UA = "printpapi-agent"

# Network settings, set once by main() from agent.ini (see http_settings). Module state rather
# than two more parameters on every http fn: register/run_once take injectable
# (url, key[, body]) callables, and threading headers + timeout through all of them buys nothing.
#   headers - extra request headers, e.g. an auth proxy's service-token pair in front of /agent/*
#   timeout - socket timeout per request. Must outlast the server's long-poll (25 s). Without one,
#             a NAT flow that is silently dropped mid-poll hangs the agent forever while the
#             server already shows it offline.
_HTTP_DEFAULT_TIMEOUT = 60.0
_MIN_TIMEOUT = 30.0
_HTTP = {"headers": {}, "timeout": _HTTP_DEFAULT_TIMEOUT}
_RESERVED_HEADERS = {"authorization", "user-agent", "content-type"}


def configure_http(headers=None, timeout=None):
    _HTTP["headers"] = dict(headers or {})
    if timeout is not None:
        _HTTP["timeout"] = float(timeout)


def raw_to_printer(printer, data):
    import win32print  # only on Windows; injected in tests
    h = win32print.OpenPrinter(printer)
    try:
        win32print.StartDocPrinter(h, 1, ("print-agent", None, "RAW"))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, data)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


# Job 'options' (duplex/paper/bin/color/pages, validated by the server) map onto each
# backend's native flags: SumatraPDF -print-settings on Windows, lp -o on CUPS.
_SUMATRA_DUPLEX = {"long-edge": "duplexlong", "short-edge": "duplexshort", "one-sided": "simplex"}
_CUPS_DUPLEX = {"long-edge": "two-sided-long-edge", "short-edge": "two-sided-short-edge",
                "one-sided": "one-sided"}


def _sumatra_settings(options):
    """options dict -> SumatraPDF -print-settings value (comma-separated list)."""
    parts = []
    if "pages" in options:
        parts.append(options["pages"])
    if "duplex" in options:
        parts.append(_SUMATRA_DUPLEX[options["duplex"]])
    if "paper" in options:
        parts.append(f"paper={options['paper']}")
    if "bin" in options:
        parts.append(f"bin={options['bin']}")
    if "color" in options:
        parts.append("color" if options["color"] else "monochrome")
    return ",".join(parts)


def _lp_options(options):
    """options dict -> ['-o', 'k=v', ...] for lp. lp splits one -o value on spaces, so a
    value with whitespace could smuggle extra options (e.g. 'raw') in — refuse those."""
    pairs = []
    if "duplex" in options:
        pairs.append(("sides", _CUPS_DUPLEX[options["duplex"]]))
    if "paper" in options:
        pairs.append(("media", options["paper"]))
    if "bin" in options:
        pairs.append(("InputSlot", options["bin"]))
    if "color" in options:
        pairs.append(("print-color-mode", "color" if options["color"] else "monochrome"))
    if "pages" in options:
        pairs.append(("page-ranges", options["pages"]))
    out = []
    for k, v in pairs:
        if any(c.isspace() for c in v):
            raise ValueError(f"option {k} value must not contain whitespace: {v!r}")
        out += ["-o", f"{k}={v}"]
    return out


def pdf_to_printer(printer, data, options=None, sumatra="SumatraPDF.exe", run=subprocess.run):
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        cmd = [sumatra, "-print-to", printer]
        if options:
            cmd += ["-print-settings", _sumatra_settings(options)]
        run(cmd + ["-silent", path], check=True, timeout=60)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def raw_to_printer_cups(printer, data, run=subprocess.run):
    # Already-rendered bytes (ZPL/ESC-POS) straight to the queue — CUPS must NOT re-render them.
    run(["lp", "-d", printer, "-o", "raw"], input=data, check=True, timeout=60)


def pdf_to_printer_cups(printer, data, options=None, run=subprocess.run):
    # CUPS renders PDF through its own filter chain (gotcha #1: never send PDF as raw).
    run(["lp", "-d", printer] + (_lp_options(options) if options else []),
        input=data, check=True, timeout=60)


def raw_to_socket(target, data, connect=socket.create_connection, timeout=30):
    # Raw bytes straight to a network printer's socket (e.g. Zebra :9100). Already-rendered only
    # (ZPL/ESC-POS) — gotcha #1: a bare socket has no renderer, so PDF must never reach here.
    addr = target[len("socket://"):] if target.startswith("socket://") else target
    host, _, port = addr.rpartition(":")  # ponytail: IPv4 host:port; no IPv6-in-brackets support
    with connect((host, int(port)), timeout=timeout) as s:
        s.sendall(data)


def file_target_dir(target):
    """`file:///srv/inbox` -> `/srv/inbox` (and `file:///C:/inbox` -> `C:\\inbox` on Windows).
    url2pathname does the drive-letter and %20 handling so we don't."""
    return urllib.request.url2pathname(target[len("file://"):])


def write_to_file(target, data, *, mode="raw", job_id=None, index=1):
    """"Virtual print server": drop the job in a directory instead of on paper — archival and
    paperless workflows. A pdf job's payload already *is* a PDF, so nothing renders here; raw
    payloads (ZPL/ESC-POS) are written verbatim as `.prn`. Returns the path written."""
    directory = file_target_dir(target)
    os.makedirs(directory, exist_ok=True)
    stem = f"job-{job_id}" if job_id is not None else "job"
    if index > 1:
        stem += f"-{index}"        # copies of one job must not overwrite each other
    path = os.path.join(directory, stem + (".pdf" if mode == "pdf" else ".prn"))
    with open(path, "wb") as f:
        f.write(data)
    return path


# --- printer capabilities (best-effort discovery, reported at registration) ---------------------
# Shape: {"papers": [...], "bins": [...], "duplex": bool, "color": bool} — any subset. A driver
# quirk or missing tool must never block registration: collectors return None on any failure.

def _caps_from_lpoptions(text):
    """Parse `lpoptions -p <queue> -l` output -> capabilities dict, or None if nothing useful."""
    caps = {}
    for line in text.splitlines():
        head, sep, choices = line.partition(":")
        if not sep:
            continue
        key = head.split("/", 1)[0].strip()
        vals = [c.lstrip("*") for c in choices.split()]
        if key == "PageSize":
            caps["papers"] = vals
        elif key == "InputSlot":
            caps["bins"] = vals
        elif key == "Duplex":
            caps["duplex"] = any(v != "None" for v in vals)
        elif key == "ColorModel":
            caps["color"] = any(v.lower() not in ("gray", "grayscale") for v in vals)
    return caps or None


def collect_capabilities_cups(printer, run=subprocess.run):
    try:
        r = run(["lpoptions", "-p", printer, "-l"], capture_output=True, check=True, timeout=10)
        return _caps_from_lpoptions(r.stdout.decode(errors="replace"))
    except Exception:
        return None


def collect_capabilities_windows(printer, wp=None):
    try:
        if wp is None:
            import win32print as wp
        h = wp.OpenPrinter(printer)
        try:
            port = wp.GetPrinter(h, 2)["pPortName"]
        finally:
            wp.ClosePrinter(h)
        papers = wp.DeviceCapabilities(printer, port, 16)   # DC_PAPERNAMES
        bins = wp.DeviceCapabilities(printer, port, 12)     # DC_BINNAMES
        duplex = wp.DeviceCapabilities(printer, port, 7)    # DC_DUPLEX
        color = wp.DeviceCapabilities(printer, port, 32)    # DC_COLORDEVICE
        return {"papers": [p.strip("\x00 ") for p in papers or []],
                "bins": [b.strip("\x00 ") for b in bins or []],
                "duplex": bool(duplex), "color": bool(color)}
    except Exception:
        return None


def select_caps_collector(platform=sys.platform):
    return collect_capabilities_windows if platform.startswith("win") else collect_capabilities_cups


def add_capabilities(printers, caps_fn):
    """Attach discovered capabilities to parse_printers() entries. socket:// and file:// targets
    have no driver/queue to ask; a None result (collector failed) just leaves the entry as-is."""
    for p in printers:
        if not p["target"].startswith(("socket://", "file://")):
            caps = caps_fn(p["target"])
            if caps:
                p["capabilities"] = caps
    return printers


def select_backend(platform=sys.platform, sumatra="SumatraPDF.exe"):
    """(raw_fn, pdf_fn) for the host OS. Windows: win32print + SumatraPDF; else CUPS lp
    (Linux and macOS — macOS is CUPS underneath, see docs/agent.md#macos)."""
    if platform.startswith("win"):
        return raw_to_printer, lambda p, d, o=None: pdf_to_printer(p, d, options=o, sumatra=sumatra)
    return raw_to_printer_cups, pdf_to_printer_cups


def parse_printers(spec):
    """agent.ini 'printers' (semicolon-separated) -> [{name, can_pdf, target}].
    Grammar: name [|pdf] [= target].
      - no '='  -> target is the name (a CUPS queue / Windows printer).
      - '= socket://host:port' -> agent opens a raw TCP socket to it.
      - '= file:///path/to/dir' -> agent writes the job into that directory (no paper).
    Append '|pdf' to declare a document printer PDF-capable; default is raw-only so a label
    printer is never auto-sent a PDF (gotcha #1). '|raw' says the same explicitly - it is how
    an `auto` list (PDF by default, see resolve_printers) turns PDF off for one printer. A socket:// target is always raw-only, a
    file:// target always takes both (a directory cannot misrender anything)."""
    out = []
    for entry in spec.split(";"):
        left, _, target = entry.partition("=")
        name, _, tag = left.strip().partition("|")
        name = name.strip()
        if not name:
            continue
        target = target.strip() or name
        can_pdf = tag.strip().lower() == "pdf"
        if target.startswith("socket://"):
            can_pdf = False  # gotcha #1: no renderer behind a bare socket
            addr = target[len("socket://"):]
            host, sep, port = addr.rpartition(":")
            if not host or not sep or not port.isdigit():
                raise ValueError(
                    f"invalid socket printer target {target!r} for {name!r}: expected socket://host:port")
        elif target.startswith("file://"):
            can_pdf = True  # a directory takes pdf and raw alike
            if not file_target_dir(target).strip():
                raise ValueError(
                    f"invalid file printer target {target!r} for {name!r}: expected file:///path/to/dir")
        out.append({"name": name, "can_pdf": can_pdf, "target": target})
    return out


# Windows "printers" that are really dialogs or apps: a job sent to one waits forever for a click.
# ponytail: matched by port (Print to PDF / XPS = PORTPROMPT:, Fax = SHRFAX:, OneNote = nul:) plus a
# name prefix for OneNote variants on other ports. Add to the list if another one turns up.
_VIRTUAL_PORTS = {"PORTPROMPT:", "SHRFAX:", "NUL:"}


def discover_printers_windows(wp=None):
    """Names of the installed printers (local + connected network shares), virtual ones skipped."""
    if wp is None:
        import win32print as wp
    found = wp.EnumPrinters(wp.PRINTER_ENUM_LOCAL | wp.PRINTER_ENUM_CONNECTIONS, None, 2)
    return [p["pPrinterName"] for p in found
            if (p.get("pPortName") or "").upper() not in _VIRTUAL_PORTS
            and "onenote" not in p["pPrinterName"].lower()]


def discover_printers_cups(run=subprocess.run):
    """CUPS destinations (`lpstat -e`, Linux and macOS). No CUPS -> nothing found."""
    try:
        r = run(["lpstat", "-e"], capture_output=True, check=True, timeout=10)
    except Exception:
        return []
    return [line.strip() for line in r.stdout.decode(errors="replace").splitlines() if line.strip()]


def select_discoverer(platform=sys.platform):
    return discover_printers_windows if platform.startswith("win") else discover_printers_cups


def resolve_printers(spec, discover):
    """agent.ini 'printers' -> entries, where an `auto` item expands to every installed printer.
    Discovered printers take PDF: each is a queue with a driver, so the PDF is rendered
    (SumatraPDF / CUPS) before it reaches the printer, never sent raw (gotcha #1). An older one
    whose driver cannot render is turned off with `|raw`. Explicit entries win over a discovered
    one of the same name, so `auto ; Old Zebra|raw ; netz = socket://…` keeps one raw-only and
    adds a socket printer."""
    items = [e for e in spec.split(";") if e.strip()]
    if not any(e.strip().lower() == "auto" for e in items):
        return parse_printers(spec)
    explicit = parse_printers(";".join(e for e in items if e.strip().lower() != "auto"))
    by_name = {p["name"]: p for p in explicit}
    found = discover()
    out = [by_name.pop(n, {"name": n, "can_pdf": True, "target": n}) for n in found]
    out += [p for p in explicit if p["name"] in by_name]
    if not out:
        raise SystemExit("printers = auto: no printers found on this computer - install one, "
                         "or list them in agent.ini")
    return out


def printers_spec(agent_cfg):
    """The 'printers' line; left out it means auto."""
    return agent_cfg.get("printers", "").strip() or "auto"


def agent_name(agent_cfg, hostname=socket.gethostname):
    """`name = auto` -> this computer's name. Unset stays "agent": the server binds a key to its
    name on first contact, so silently renaming an existing install would lock it out (409)."""
    name = agent_cfg.get("name", "agent").strip() or "agent"
    return hostname() if name.lower() == "auto" else name


def _req(url, key, *, data=None, method="GET", as_bytes=False):
    r = urllib.request.Request(url, data=data, method=method)
    for name, value in _HTTP["headers"].items():
        r.add_header(name, value)
    r.add_header("Authorization", f"Bearer {key}")
    r.add_header("User-Agent", _UA)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=_HTTP["timeout"]) as resp:
            raw = resp.read()
            if resp.status == 204:
                return None
            return raw if as_bytes else (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        # The server says *why* in a JSON {"error": ...} body; a proxy's HTML page says nothing
        # useful, so then the bare status it is.
        try:
            reason = json.loads(e.read() or b"{}").get("error")
        except Exception:
            reason = None
        raise OSError(f"server returned {e.code}" + (f": {reason}" if reason else "")) from e
    except urllib.error.URLError as e:
        raise OSError(f"connection failed: {e.reason}") from e


def _get(url, key):
    return _req(url, key, method="GET")


def _get_bytes(url, key):
    return _req(url, key, method="GET", as_bytes=True)


def _post(url, key, body):
    return _req(url, key, data=json.dumps(body).encode(), method="POST")


def _read_machine_guid():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
        return winreg.QueryValueEx(k, "MachineGuid")[0]


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def raw_machine_id(platform_name=sys.platform, *, read_registry=_read_machine_guid,
                   read_file=_read_text, run=subprocess.run):
    """The OS's own id for this computer, or None. Stable across reboots, renames and network
    changes - unlike a MAC address (Wi-Fi vs cable, docks, VPNs, Windows' random MACs).
    Windows: MachineGuid (new on reinstall). Linux: machine-id. macOS: the hardware UUID."""
    try:
        if platform_name.startswith("win"):
            return read_registry().strip() or None
        if platform_name == "darwin":
            out = run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                      check=True, timeout=10).stdout.decode(errors="replace")
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("=", 1)[1].strip().strip('"') or None
            return None
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                return read_file(path).strip() or None
            except OSError:
                continue
        return None
    except Exception:
        return None


def mac_address(getnode=uuid.getnode):
    """This PC's MAC as aa:bb:..., or None when uuid.getnode() had to make one up (it then sets
    the multicast bit). Display only: which NIC answers first can change."""
    node = getnode()
    if (node >> 40) & 1:
        return None
    return ":".join(f"{(node >> s) & 0xFF:02x}" for s in range(40, -1, -8))


def machine_info(*, raw_id=raw_machine_id, hostname=socket.gethostname, mac=mac_address,
                 os_name=platform.platform):
    """What the server learns about this PC at register: which computer holds the key (so a
    second PC on the same key is caught) and what to show on the Devices page. The OS id is sent
    hashed - the server only compares it, it never needs the raw value."""
    info = {}
    rid = raw_id()
    if rid:
        info["id"] = hashlib.sha256(f"printpapi-machine:{rid}".encode()).hexdigest()[:32]
    for k, fn in (("hostname", hostname), ("mac", mac), ("os", os_name)):
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            info[k] = v
    return info


def register(base, key, name, printers, http_post=_post, machine=None):
    body = {"name": name, "printers": printers}
    if machine:
        body["machine"] = machine
    return http_post(base + "/agent/register", key, body)


def _log_error(msg):
    """Append one line to the crash log. Under pythonw there is no console; this file is what
    an operator reads over remote desktop."""
    logdir = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    try:
        with open(os.path.join(logdir, "print_agent-error.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def register_with_retry(base, key, name, printers, *, http_post=_post, sleep=time.sleep,
                        log=_log_error, max_wait=300, machine=None):
    """register() until it works. At boot the network (or an auth proxy) is often not there yet;
    a one-shot register killed the process, and a logon-triggered task never restarted it.
    Retries on anything - OSError (network/HTTP status) and ValueError (a proxy's HTML page
    instead of JSON) alike; a permanent misconfiguration shows up in the log, once per try.
    Also retries a reply that parsed but is not what main() needs (e.g. an empty {} from a proxy
    that ate the body) instead of letting main() crash on it after only one attempt."""
    attempt = 0
    while True:
        try:
            reg = register(base, key, name, printers, http_post=http_post, machine=machine)
            if not isinstance(reg, dict) or not isinstance(reg.get("printer_ids"), dict) \
                    or "computer_id" not in reg:
                raise ValueError(f"unexpected register reply: {reg!r:.200}")
            return reg
        except Exception as e:
            wait = min(2 ** attempt, max_wait)
            msg = f"register failed (try {attempt + 1}), retry in {wait}s: {e}"
            # stderr too, not just the log file: an interactive hand test (python.exe, not
            # pythonw) would otherwise sit silently waiting with nothing on the console.
            print(msg, file=sys.stderr)
            log(msg)
            sleep(wait)
            attempt += 1


def poll_job(base, key, http_get=_get):
    return http_get(base + "/agent/jobs", key)


def download_payload(base, key, job_id, http_get_bytes=_get_bytes):
    return http_get_bytes(base + f"/agent/jobs/{job_id}/payload", key)


def report_result(base, key, job_id, ok, error=None, http_post=_post):
    http_post(base + f"/agent/jobs/{job_id}/result", key, {"ok": ok, "error": error})


def _report_with_retry(base, key, job_id, ok, error=None, *, http_post=_post,
                       attempts=5, sleep=time.sleep):
    # A lost result makes the server's reaper requeue the job -> duplicate print. Retry hard.
    for i in range(attempts):
        try:
            report_result(base, key, job_id, ok, error, http_post=http_post)
            return True
        except OSError as e:
            print(f"report_result failed (try {i + 1}/{attempts}): {e}", file=sys.stderr)
            if i + 1 < attempts:
                sleep(min(2 ** i, 30))
    return False  # ponytail: after 5 tries we drop the result; reaper requeues -> possible dup


def print_job(mode, entry, data, copies=1, options=None, raw_fn=raw_to_printer,
              pdf_fn=pdf_to_printer, socket_fn=raw_to_socket, file_fn=write_to_file,
              job_id=None):
    target = entry["target"]
    if mode not in ("raw", "pdf"):
        raise ValueError(f"bad mode: {mode}")
    # ponytail: loop the whole send per copy — correct on every backend without a per-driver
    # copies flag (win32/Sumatra/CUPS/socket differ). Native flags (lp -n, Sumatra "Nx") would
    # be one call instead of N; not worth the branching for the small copy counts labels use.
    if target.startswith("socket://"):
        if mode != "raw":
            raise ValueError("network socket printer is raw-only (cannot render PDF)")
        send = lambda i: socket_fn(target, data)
    elif target.startswith("file://"):
        # options are print-hardware settings (tray/duplex/…) — nothing to apply to a file
        send = lambda i: file_fn(target, data, mode=mode, job_id=job_id, index=i)
    elif mode == "raw":
        send = lambda i: raw_fn(target, data)
    else:
        # 3-arg call only when options are set: plain jobs keep working with 2-arg pdf fns
        send = (lambda i: pdf_fn(target, data, options)) if options else (lambda i: pdf_fn(target, data))
    for i in range(1, copies + 1):
        send(i)


def run_once(base, key, printer_by_id, *, http_get=_get, http_get_bytes=_get_bytes,
             http_post=_post, raw_fn=raw_to_printer, pdf_fn=pdf_to_printer,
             report_sleep=time.sleep):
    job = poll_job(base, key, http_get=http_get)
    if job is None:
        return False
    job_id = job["job_id"]
    printer = printer_by_id.get(job["printer_id"])
    try:
        data = download_payload(base, key, job_id, http_get_bytes=http_get_bytes)
        if printer is None:
            raise ValueError(f"unknown printer id: {job['printer_id']}")
        print_job(job["mode"], printer, data, copies=job.get("copies", 1),
                  options=job.get("options"), raw_fn=raw_fn, pdf_fn=pdf_fn, job_id=job_id)
    except Exception as e:
        _report_with_retry(base, key, job_id, False, str(e), http_post=http_post,
                           sleep=report_sleep)
        return True
    _report_with_retry(base, key, job_id, True, None, http_post=http_post, sleep=report_sleep)
    return True


def load_config(base_dir):
    ini = os.path.join(base_dir, "agent.ini")
    cfg = configparser.ConfigParser()
    if not cfg.read(ini) or "agent" not in cfg:
        raise SystemExit(
            f"missing or invalid {ini}: need an [agent] section with server_url and api_key")
    return cfg["agent"]


def http_settings(agent_cfg):
    """agent.ini -> (extra headers, timeout).

    Headers come from an optional [headers] section, one `Name = value` per line. configparser
    lower-cases the names; HTTP header names are case-insensitive, so that is harmless. Values are
    read raw (no interpolation) so a secret containing '%' survives. `timeout` is an optional key
    in [agent]."""
    parser = agent_cfg.parser
    headers = {}
    if parser.has_section("headers"):
        defaults = parser.defaults()
        for name, value in parser.items("headers", raw=True):
            if name in defaults:
                continue
            if name.lower() in _RESERVED_HEADERS:
                raise SystemExit(f"agent.ini [headers]: {name} is set by the agent itself")
            headers[name] = value
    timeout = agent_cfg.getfloat("timeout", fallback=_HTTP_DEFAULT_TIMEOUT)
    if timeout < _MIN_TIMEOUT:
        raise SystemExit(
            f"agent.ini timeout = {timeout:g}: must be >= {_MIN_TIMEOUT:g} s "
            f"(the server long-polls 25 s)")
    return headers, timeout


def _poll_forever(base, key, printer_by_id, *, raw_fn, pdf_fn, run_once=run_once,
                  sleep=time.sleep, log=_log_error):
    """The main loop: poll, print, repeat. Logs only on a failing/recovered transition, not every
    error - at the 2 s retry interval a down server or a revoked token would otherwise write
    about 43k lines/day and bury the one thing an operator needs from the crash log. Still prints
    every error to the console for a foreground/hand-test run."""
    failing = False
    while True:
        try:
            run_once(base, key, printer_by_id, raw_fn=raw_fn, pdf_fn=pdf_fn)
        except Exception as e:
            if not failing:
                log(f"poll error: {e}")
                failing = True
            print(f"poll error: {e}")
            sleep(2)
        else:
            if failing:
                log("poll recovered")
                failing = False


def main():
    base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    agent_cfg = load_config(base_dir)
    configure_http(*http_settings(agent_cfg))
    base = agent_cfg["server_url"].rstrip("/")
    key = agent_cfg["api_key"]
    name = agent_name(agent_cfg)
    bundled = os.path.join(base_dir, "SumatraPDF.exe")
    sumatra = bundled if os.path.exists(bundled) else "SumatraPDF.exe"
    raw_fn, pdf_fn = select_backend(sumatra=sumatra)
    printers = resolve_printers(printers_spec(agent_cfg), select_discoverer())
    add_capabilities(printers, select_caps_collector())
    reg = register_with_retry(base, key, name, printers, machine=machine_info())
    entry_by_name = {p["name"]: p for p in printers}
    printer_by_id = {pid: entry_by_name[pname] for pname, pid in reg["printer_ids"].items()}
    print(f"print-agent registered as computer {reg['computer_id']}, printers={printer_by_id}")
    _poll_forever(base, key, printer_by_id, raw_fn=raw_fn, pdf_fn=pdf_fn)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # load_config() and http_settings() raise this for a bad agent.ini; it is a BaseException,
        # so it would otherwise skip the log entirely (and under pythonw, skip the console too).
        if e.code not in (None, 0):
            _log_error(f"exit: {e.code}")
        raise
    except Exception:
        import traceback
        _log_error(traceback.format_exc())
        raise
