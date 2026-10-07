# printpapi — self-hosted PrintNode alternative. Elastic License 2.0 (see LICENSE).
"""Load a `.env` file into the environment, so `python -m app.server` needs no inline variables.

A variable already in the real environment always wins: a token passed on the command line or by
docker must never be silently replaced by a stale file.

# ponytail: KEY=VALUE lines, `#` comments, optional `export ` and surrounding quotes. No variable
# expansion, no multi-line values, no escape sequences — reach for python-dotenv if that's needed.
"""
import os
from pathlib import Path


def _value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    # Unquoted: ` #` starts a comment, a bare `#` inside the value (a#b) does not.
    for i, ch in enumerate(raw):
        if ch == "#" and i > 0 and raw[i - 1].isspace():
            return raw[:i].rstrip()
    return raw


def load(path=".env", env=None):
    """Read `path` into `env` (default `os.environ`), skipping keys already set. A missing file
    is not an error — the environment alone is a valid configuration."""
    env = os.environ if env is None else env
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if key and key not in env:
            env[key] = _value(raw)
