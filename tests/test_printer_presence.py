"""An agent's printer list is what it reports *now*: a printer it stopped reporting is removed
(hidden, refused for new jobs) and comes back under its old id if it returns."""
import json, threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

import pytest

from app import server, store

BOTH = [{"name": "Zebra"}, {"name": "EPSON", "can_pdf": True}]
ZEBRA_ONLY = [{"name": "Zebra"}]


def _mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _names(conn, **kw):
    return [p["name"] for p in store.list_printers(conn, 60, **kw)]


def test_a_printer_the_agent_stopped_reporting_is_removed():
    conn = _mem()
    ids = store.register_agent(conn, "pc", "k", BOTH)["printer_ids"]
    again = store.register_agent(conn, "pc", "k", ZEBRA_ONLY)
    assert again["printer_ids"] == {"Zebra": ids["Zebra"]}
    assert _names(conn) == ["Zebra"]
    assert store.get_printer(conn, ids["EPSON"]) is None
    assert store.list_agents(conn, 60)[0]["printers"] == 1
    assert store.metrics(conn, 60)["printers_total"] == 1


def test_a_printer_that_returns_gets_its_old_id_back():
    conn = _mem()
    ids = store.register_agent(conn, "pc", "k", BOTH)["printer_ids"]
    store.register_agent(conn, "pc", "k", ZEBRA_ONLY)
    assert store.register_agent(conn, "pc", "k", BOTH)["printer_ids"] == ids
    assert _names(conn) == ["Zebra", "EPSON"]


def test_new_jobs_for_a_removed_printer_are_refused_but_its_history_stays():
    conn = _mem()
    ids = store.register_agent(conn, "pc", "k", BOTH)["printer_ids"]
    jid = store.enqueue_job(conn, ids["EPSON"], "raw_base64", "raw", b"x")
    store.register_agent(conn, "pc", "k", ZEBRA_ONLY)
    with pytest.raises(store.UnknownPrinter, match="removed"):
        store.enqueue_job(conn, ids["EPSON"], "raw_base64", "raw", b"x")
    [job] = store.recent_jobs(conn)
    assert job["id"] == jid and job["printer_name"] == "EPSON"


def test_other_agents_printers_are_untouched():
    conn = _mem()
    store.register_agent(conn, "pc1", "k1", [{"name": "P"}])
    store.register_agent(conn, "pc2", "k2", [{"name": "P"}, {"name": "Q"}])
    store.register_agent(conn, "pc1", "k1", [])
    assert sorted(_names(conn)) == ["P", "Q"]        # pc2's P is a different printer


def _req(method, url, body=None, token="t"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_lists_only_present_printers_and_explains_a_refused_job():
    conn = _mem()
    handler = server.make_handler(conn=conn, token="t", long_poll_timeout=0.3, poll_interval=0.05)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        _, reg = _req("POST", base + "/agent/register", {"name": "pc", "printers": BOTH},
                      token="ak")
        _req("POST", base + "/agent/register", {"name": "pc", "printers": ZEBRA_ONLY}, token="ak")
        code, body = _req("GET", base + "/printers")
        assert code == 200 and [p["name"] for p in body["printers"]] == ["Zebra"]
        code, body = _req("POST", base + "/jobs", {"printer_id": reg["printer_ids"]["EPSON"],
                                                   "type": "raw_base64", "content": "QUJD"})
        assert code == 400 and "removed" in body["error"]
    finally:
        httpd.shutdown()
