"""When each API key was last used — by an integration's requests or by an agent's polls."""
import json, threading, time, urllib.request
from http.server import ThreadingHTTPServer

from app import server, store


def _mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _key(conn, kid):
    return next(k for k in store.list_api_keys(conn) if k["id"] == kid)


def test_a_new_key_was_never_used():
    conn = _mem()
    kid = store.add_api_key(conn, "laravel", "ck")
    k = _key(conn, kid)
    assert k["last_used_at"] is None and k["used_by_agent"] is None


def test_using_a_key_records_when_at_most_once_a_minute():
    conn = _mem()
    kid = store.add_api_key(conn, "laravel", "ck")
    t0 = time.time()
    assert store.authenticate_client(conn, "ck", now=t0)["id"] == kid
    assert _key(conn, kid)["last_used_at"] == t0
    store.authenticate_client(conn, "ck", now=t0 + 30)       # within the minute: no write
    assert _key(conn, kid)["last_used_at"] == t0
    store.authenticate_client(conn, "ck", now=t0 + 61)
    assert _key(conn, kid)["last_used_at"] == t0 + 61


def test_a_failed_or_revoked_key_records_nothing():
    conn = _mem()
    kid = store.add_api_key(conn, "old", "ck")
    store.revoke_api_key(conn, kid)
    assert store.authenticate_client(conn, "ck") is None
    assert store.authenticate_client(conn, "nope") is None
    assert _key(conn, kid)["last_used_at"] is None


def test_an_agent_key_counts_the_agents_polls_and_names_the_agent():
    conn = _mem()
    kid = store.add_api_key(conn, "office-pc", "ak")
    t0 = time.time()
    store.authenticate_client(conn, "ak", now=t0 - 600)                 # e.g. its register call
    reg = store.register_agent(conn, "office-pc", "ak", [], now=t0 - 600)
    store.claim_job(conn, reg["computer_id"], now=t0)                   # a poll, 10 min later
    k = _key(conn, kid)
    assert k["last_used_at"] == t0 and k["used_by_agent"] == "office-pc"


def test_apikeys_listing_over_http_shows_last_use():
    conn = _mem()
    kid = store.add_api_key(conn, "laravel", "ck")
    handler = server.make_handler(conn=conn, token="t", long_poll_timeout=0.3, poll_interval=0.05)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path, token):
        r = urllib.request.Request(base + path)
        r.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read())
    try:
        before = time.time()
        get("/printers", "ck")                                         # the integration calls in
        [k] = [k for k in get("/apikeys", "t")["keys"] if k["id"] == kid]
        assert k["last_used_at"] >= before
    finally:
        httpd.shutdown()
