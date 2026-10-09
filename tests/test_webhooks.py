"""printapi account webhooks: validation, the event queue, delivery and both APIs."""
import base64, json, threading, time, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

import pytest

from app import printapi, server, store, webhooks


def _mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _serve(conn, token="t"):
    handler = server.make_handler(conn=conn, token=token, long_poll_timeout=0.3, poll_interval=0.05)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _call(method, url, auth, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _pa(method, url, key="t", body=None):
    return _call(method, url, "Basic " + base64.b64encode(f"{key}:".encode()).decode(), body)


def _bearer(method, url, key="t", body=None):
    return _call(method, url, f"Bearer {key}", body)


def _printer(conn, org_id=store.DEFAULT_ORG, agent="pc", key="ak"):
    reg = store.register_agent(conn, agent, key, [{"name": "P", "can_pdf": True}], org_id=org_id)
    return reg["computer_id"], reg["printer_ids"]["P"]


def _events(conn):
    return [(e["type"], e["data"].get("state", e["data"].get("online")))
            for e in store.due_webhook_events(conn, now=time.time() + 3600)]


# --- validation (pure) --------------------------------------------------------------------------

def test_validate_a_full_webhook():
    assert webhooks.validate({"url": "https://h.example/x", "secret": "s",
                              "messages": ["print job state"]}) == {
        "url": "https://h.example/x", "secret": "s", "messages": ["print job state"]}


@pytest.mark.parametrize("body, why", [
    ({"secret": "s", "messages": ["*"]}, "url"),
    ({"url": "ftp://h/x", "secret": "s", "messages": ["*"]}, "url"),
    ({"url": "https://h/x", "messages": ["*"]}, "secret"),
    ({"url": "https://h/x", "secret": "  ", "messages": ["*"]}, "secret"),
    ({"url": "https://h/x", "secret": "s"}, "messages"),
    ({"url": "https://h/x", "secret": "s", "messages": []}, "messages"),
    ({"url": "https://h/x", "secret": "s", "messages": "*"}, "messages"),
    ({"url": "https://h/x", "secret": "s", "messages": ["*", "computer state"]}, "stand alone"),
    ({"url": "https://h/x", "secret": "s", "messages": ["printer state"]}, "printer state"),
    ("nope", "object"),
])
def test_validate_rejects(body, why):
    with pytest.raises(ValueError, match=why):
        webhooks.validate(body)


def test_validate_partial_takes_any_subset_but_not_nothing():
    assert webhooks.validate({"secret": "new"}, partial=True) == {"secret": "new"}
    with pytest.raises(ValueError, match="nothing to update"):
        webhooks.validate({}, partial=True)
    with pytest.raises(ValueError, match="url"):
        webhooks.validate({"url": "nope"}, partial=True)


# --- printapi shapes (pure) ---------------------------------------------------------------------

def test_webhook_object_shape():
    row = {"id": 7, "org_id": 2, "url": "https://h/x", "secret": "s", "messages": ["*"],
           "received_events": 3, "dropped_events": 1, "successful_requests": 2,
           "failed_requests": 1, "created_at": 0}
    assert printapi.webhook(row) == {
        "webhookId": 7, "url": "https://h/x", "secret": "s", "messages": ["*"],
        "counts": {"receivedEvents": 3, "droppedEvents": 1, "successfulRequests": 2,
                   "failedRequests": 1}}


@pytest.mark.parametrize("ours, error, theirs", [
    ("queued", None, "new"), ("claimed", None, "sent_to_client"), ("done", None, "done"),
    ("failed", "paper out", "error"), ("failed", "expired", "expired")])
def test_print_job_state_event(ours, error, theirs):
    ev = {"type": "print job state", "org_id": 4, "created_at": 0,
          "data": {"uid": "u-1", "job_id": 9, "state": ours, "error": error}}
    assert printapi.webhook_event(ev) == {
        "type": "print job state", "accountId": 4, "controllingAccountId": 4,
        "createdAt": "1970-01-01T00:00:00.000Z",
        "data": {"uid": "u-1", "state": theirs, "message": error or "", "printJobId": 9}}


def test_computer_state_event_online_and_offline():
    base = {"type": "computer state", "org_id": 1, "created_at": 60}
    on = printapi.webhook_event(dict(base, data={"computer_id": 3, "name": "office-pc",
                                                  "online": True, "last_seen_at": 0}))
    conn = {"serverUuid": None, "connectionTimestamp": "1970-01-01T00:00:00.000Z",
            "version": "", "edition": "printpapi", "hostname": "office-pc"}
    assert on["data"] == {"connections": [conn], "event": conn, "computerId": 3}
    off = printapi.webhook_event(dict(base, data={"computer_id": 3, "name": "office-pc",
                                                   "online": False, "last_seen_at": 0}))
    assert off["data"] == {"connections": [], "computerId": 3, "event": dict(
        conn, disconnectionTimestamp="1970-01-01T00:01:00.000Z")}


# --- store: webhooks and the event queue --------------------------------------------------------

def test_at_most_five_webhooks_per_org():
    conn = _mem()
    other = store.create_org(conn, "other")
    for i in range(5):
        store.create_webhook(conn, 1, f"https://h/{i}", "s", ["*"])
    with pytest.raises(store.WebhookLimit):
        store.create_webhook(conn, 1, "https://h/6", "s", ["*"])
    store.create_webhook(conn, other, "https://h/x", "s", ["*"])    # the cap is per org


def test_job_lifecycle_queues_stable_states_for_matching_webhooks_only():
    conn = _mem()
    agent, pid = _printer(conn)
    store.create_webhook(conn, 1, "https://h/jobs", "s", ["print job state"])
    store.create_webhook(conn, 1, "https://h/pcs", "s", ["computer state"])
    other = store.create_org(conn, "other")
    store.create_webhook(conn, other, "https://h/other", "s", ["*"])

    jid = store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    store.claim_job(conn, agent)
    store.finish_job(conn, jid, agent, True)
    evs = store.due_webhook_events(conn, now=time.time() + 1)
    assert [(e["url"], e["data"]["state"]) for e in evs] == [
        ("https://h/jobs", "queued"), ("https://h/jobs", "claimed"), ("https://h/jobs", "done")]
    assert {e["data"]["job_id"] for e in evs} == {jid}
    assert len({e["data"]["uid"] for e in evs}) == 3          # one uid per event, kept on retry
    assert store.list_webhooks(conn, org_id=1)[0]["received_events"] == 3


def test_cancel_and_requeue_are_not_stable_states():
    conn = _mem()
    agent, pid = _printer(conn)
    store.create_webhook(conn, 1, "https://h/x", "s", ["*"])
    j1 = store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    store.cancel_job(conn, j1)
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    store.claim_job(conn, agent, now=100)
    store.requeue_stale(conn, timeout_s=10, max_retries=2, now=200)   # back to queued, no event
    assert _events(conn) == [("print job state", "queued"), ("print job state", "queued"),
                             ("print job state", "claimed")]


def test_expiry_and_retry_limit_queue_terminal_events():
    conn = _mem()
    agent, pid = _printer(conn)
    store.create_webhook(conn, 1, "https://h/x", "s", ["print job state"])
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x", expire_after=1)
    store.expire_jobs(conn, now=time.time() + 10)
    j2 = store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    store.claim_job(conn, agent, now=100)
    store.requeue_stale(conn, timeout_s=10, max_retries=0, now=200)
    evs = store.due_webhook_events(conn, now=time.time() + 3600)
    terminal = [(e["data"]["state"], e["data"]["error"]) for e in evs
                if e["data"]["state"] == "failed"]
    assert terminal == [("failed", "expired"), ("failed", "retry limit exceeded")]
    assert evs[-1]["data"]["job_id"] == j2


def test_an_idempotent_resubmit_queues_nothing_new():
    conn = _mem()
    _, pid = _printer(conn)
    store.create_webhook(conn, 1, "https://h/x", "s", ["*"])
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x", idempotency_key="k")
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x", idempotency_key="k")
    assert len(_events(conn)) == 1


def test_computer_transitions_queue_events_even_without_an_event_url():
    conn = _mem()
    _printer(conn)
    store.create_webhook(conn, 1, "https://h/x", "s", ["computer state"])
    store.claim_agent_transitions(conn, 60, now=time.time() + 300)         # goes offline
    assert _events(conn) == [("computer state", False)]


def test_successful_request_clears_events_and_counts():
    conn = _mem()
    _, pid = _printer(conn)
    wid = store.create_webhook(conn, 1, "https://h/x", "s", ["*"])
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    ids = [e["id"] for e in store.due_webhook_events(conn)]
    store.webhook_request_done(conn, ids, ok=True)
    assert store.due_webhook_events(conn, now=time.time() + 3600) == []
    w = store.list_webhooks(conn, org_id=1)[0]
    assert (w["id"], w["successful_requests"], w["failed_requests"]) == (wid, 1, 0)


def test_failed_request_retries_once_after_five_seconds_then_drops():
    conn = _mem()
    _, pid = _printer(conn)
    store.create_webhook(conn, 1, "https://h/x", "s", ["*"])
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    now = time.time()
    ids = [e["id"] for e in store.due_webhook_events(conn, now=now)]
    store.webhook_request_done(conn, ids, ok=False, now=now)
    assert store.due_webhook_events(conn, now=now + 4) == []               # not due yet
    retry = store.due_webhook_events(conn, now=now + 5)
    assert [e["id"] for e in retry] == ids and retry[0]["attempts"] == 1
    store.webhook_request_done(conn, ids, ok=False, now=now + 5)
    assert store.due_webhook_events(conn, now=now + 3600) == []            # dropped
    w = store.list_webhooks(conn, org_id=1)[0]
    assert (w["failed_requests"], w["dropped_events"]) == (2, 1)


def test_queued_events_outlive_their_webhook_and_keep_its_url():
    conn = _mem()
    _, pid = _printer(conn)
    wid = store.create_webhook(conn, 1, "https://h/old", "s", ["*"])
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    store.update_webhook(conn, wid, {"url": "https://h/new"})
    store.delete_webhook(conn, wid)
    assert [e["url"] for e in store.due_webhook_events(conn)] == ["https://h/old"]


def test_update_and_delete_are_org_scoped():
    conn = _mem()
    other = store.create_org(conn, "other")
    wid = store.create_webhook(conn, 1, "https://h/x", "s", ["*"])
    assert store.update_webhook(conn, wid, {"secret": "n"}, org_id=other) is False
    assert store.delete_webhook(conn, wid, org_id=other) is False
    assert store.update_webhook(conn, wid, {"secret": "n", "messages": ["computer state"]},
                                org_id=1) is True
    w = store.get_webhook(conn, wid)
    assert (w["secret"], w["messages"]) == ("n", ["computer state"])
    assert store.delete_webhook(conn, wid) is True and store.get_webhook(conn, wid) is None


def test_deleting_an_org_removes_its_webhooks_and_queued_events():
    conn = _mem()
    org = store.create_org(conn, "gone")
    _, pid = _printer(conn, org_id=org, agent="pc2", key="k2")
    store.create_webhook(conn, org, "https://h/x", "s", ["*"])
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    store.delete_org(conn, org)
    assert store.list_webhooks(conn) == [] and store.due_webhook_events(conn) == []


# --- delivery -----------------------------------------------------------------------------------

def test_delivery_posts_one_array_per_target_with_the_secret_header():
    conn = _mem()
    agent, pid = _printer(conn)
    store.create_webhook(conn, 1, "https://h/a", "sa", ["print job state"])
    store.create_webhook(conn, 1, "https://h/b", "sb", ["print job state"])
    jid = store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")
    store.claim_job(conn, agent)
    sent = []
    server.deliver_webhook_events(conn, lambda url, body, headers: sent.append((url, body, headers)))
    assert [(u, h) for u, _, h in sent] == [("https://h/a", {"X-Webhook-Secret": "sa"}),
                                            ("https://h/b", {"X-Webhook-Secret": "sb"})]
    body = sent[0][1]
    assert [e["data"]["state"] for e in body] == ["new", "sent_to_client"]
    assert body[0]["type"] == "print job state" and body[0]["data"]["printJobId"] == jid
    assert body[0]["accountId"] == 1 and body[0]["createdAt"].endswith("Z")
    assert store.due_webhook_events(conn, now=time.time() + 3600) == []


def test_delivery_failure_is_retried_on_a_later_pass(capsys):
    conn = _mem()
    _, pid = _printer(conn)
    store.create_webhook(conn, 1, "https://h/a", "s", ["*"])
    store.enqueue_job(conn, pid, "raw_base64", "raw", b"x")

    def boom(url, body, headers):
        raise OSError("refused")
    now = time.time()
    server.deliver_webhook_events(conn, boom, now=now)                 # must not raise
    assert "refused" in capsys.readouterr().err
    sent = []
    server.deliver_webhook_events(conn, lambda u, b, h: sent.append(u), now=now + 5)
    assert sent == ["https://h/a"]


# --- printpapi's own API (Bearer, /orgs/{id}/webhooks) ------------------------------------------

def test_bearer_crud_for_an_org():
    conn = _mem()
    httpd, base = _serve(conn)
    try:
        url = base + "/orgs/1/webhooks"
        code, body = _bearer("POST", url, body={"url": "https://h/x", "secret": "s",
                                                "messages": ["*"]})
        assert code == 200 and body["url"] == "https://h/x" and body["messages"] == ["*"]
        wid = body["id"]
        code, body = _bearer("GET", url)
        assert code == 200 and [w["id"] for w in body["webhooks"]] == [wid]
        assert body["webhooks"][0]["secret"] == "s" and body["webhooks"][0]["received_events"] == 0
        code, body = _bearer("PATCH", f"{url}/{wid}", body={"messages": ["computer state"]})
        assert code == 200 and body["messages"] == ["computer state"]
        assert _bearer("PATCH", f"{url}/{wid}", body={"messages": ["bogus"]})[0] == 400
        assert _bearer("POST", url, body={"url": "https://h/x"})[0] == 400
        assert _bearer("DELETE", f"{url}/{wid}") == (200, {"ok": True})
        assert _bearer("DELETE", f"{url}/{wid}")[0] == 404
    finally:
        httpd.shutdown()


def test_bearer_api_is_manage_only_and_org_scoped():
    conn = _mem()
    other = store.create_org(conn, "other")
    store.add_api_key(conn, "machine", "mk", org_id=1)
    wid = store.create_webhook(conn, other, "https://h/x", "s", ["*"])
    httpd, base = _serve(conn)
    try:
        # A machine key prints; it does not manage the org.
        assert _bearer("GET", base + "/orgs/1/webhooks", key="mk")[0] == 401
        assert _bearer("GET", base + "/orgs/1/webhooks", key="nope")[0] == 401
        assert _bearer("GET", base + "/orgs/99/webhooks")[0] == 404
        # A webhook is reached through its own org only.
        assert _bearer("DELETE", f"{base}/orgs/1/webhooks/{wid}")[0] == 404
        for _ in range(4):
            store.create_webhook(conn, other, "https://h/y", "s", ["*"])
        code, body = _bearer("POST", f"{base}/orgs/{other}/webhooks",
                             body={"url": "https://h/z", "secret": "s", "messages": ["*"]})
        assert code == 400 and "5" in body["error"]
    finally:
        httpd.shutdown()


# --- printapi compatible API (Basic) ------------------------------------------------------------

def test_printapi_webhook_endpoints():
    conn = _mem()
    store.add_api_key(conn, "app", "ck", org_id=1)
    httpd, base = _serve(conn)
    try:
        assert _pa("GET", base + "/webhooks", key="ck") == (200, [])
        code, body = _pa("POST", base + "/webhook", key="ck",
                         body={"url": "https://h/x", "secret": "password", "messages": ["*"]})
        assert code == 200 and len(body) == 1                 # answers with the whole list
        hook = body[0]
        assert hook["url"] == "https://h/x" and hook["secret"] == "password"
        assert hook["messages"] == ["*"] and hook["counts"]["receivedEvents"] == 0
        wid = hook["webhookId"]
        code, body = _pa("PATCH", f"{base}/webhook/{wid}", key="ck", body={"secret": "password1"})
        assert code == 200 and body[0]["secret"] == "password1"
        code, body = _pa("POST", base + "/webhook", key="ck", body={"url": "https://h/x"})
        assert code == 400 and body["code"] == "BadRequest"
        assert _pa("DELETE", f"{base}/webhook/{wid}", key="ck") == (200, [])
        assert _pa("DELETE", f"{base}/webhook/{wid}", key="ck")[0] == 404
        assert _pa("GET", base + "/webhooks", key="nope")[0] == 401
    finally:
        httpd.shutdown()


def test_printapi_webhooks_are_org_scoped_and_root_must_name_no_org():
    conn = _mem()
    other = store.create_org(conn, "other")
    store.add_api_key(conn, "app", "ck", org_id=1)
    foreign = store.create_webhook(conn, other, "https://h/x", "s", ["*"])
    httpd, base = _serve(conn)
    try:
        assert _pa("GET", base + "/webhooks", key="ck") == (200, [])
        assert _pa("PATCH", f"{base}/webhook/{foreign}", key="ck", body={"secret": "x"})[0] == 404
        assert _pa("DELETE", f"{base}/webhook/{foreign}", key="ck")[0] == 404
        # The root token spans every org, so it lists them all but cannot create in "its" org.
        code, body = _pa("GET", base + "/webhooks")
        assert code == 200 and [w["webhookId"] for w in body] == [foreign]
        code, body = _pa("POST", base + "/webhook",
                         body={"url": "https://h/y", "secret": "s", "messages": ["*"]})
        assert code == 400 and "org" in body["message"]
    finally:
        httpd.shutdown()
