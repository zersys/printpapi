"""Which computer an agent key belongs to: renames, machine identity, a key shared by two PCs."""
import json, threading, time, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

import pytest

from app import printapi, server, store

PC_A = {"id": "a" * 32, "hostname": "DESKTOP-A", "mac": "aa:bb:cc:dd:ee:01", "os": "Windows-11"}
PC_B = {"id": "b" * 32, "hostname": "DESKTOP-B", "mac": "aa:bb:cc:dd:ee:02", "os": "Windows-10"}
PRINTERS = [{"name": "EPSON", "can_pdf": True}]


def _mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def _serve(conn, token="t"):
    handler = server.make_handler(conn=conn, token=token, long_poll_timeout=0.3, poll_interval=0.05)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _post(url, key, body):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    r.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- rename -------------------------------------------------------------------------------------

def test_same_key_new_name_renames_and_keeps_every_id():
    conn = _mem()
    first = store.register_agent(conn, "office-pc", "k", PRINTERS, machine=PC_A)
    again = store.register_agent(conn, "DESKTOP-A", "k", PRINTERS, machine=PC_A)
    assert again == first                      # same computer id, same printer ids
    [agent] = store.list_agents(conn, 60)
    assert agent["name"] == "DESKTOP-A"


def test_an_agent_without_machine_info_is_renamed_too():
    # Older agents send no machine block; the key alone already proves which agent it is.
    conn = _mem()
    first = store.register_agent(conn, "office-pc", "k", PRINTERS)
    assert store.register_agent(conn, "front-desk", "k", PRINTERS) == first
    assert store.list_agents(conn, 60)[0]["name"] == "front-desk"


def test_rename_onto_a_name_another_agent_holds_is_refused():
    conn = _mem()
    store.register_agent(conn, "office-pc", "k1", PRINTERS)
    store.register_agent(conn, "warehouse", "k2", PRINTERS)
    with pytest.raises(store.AuthError, match="warehouse"):
        store.register_agent(conn, "warehouse", "k1", PRINTERS)


def test_a_name_held_by_another_key_is_still_refused():
    conn = _mem()
    store.register_agent(conn, "office-pc", "k1", PRINTERS)
    with pytest.raises(store.AuthError):
        store.register_agent(conn, "office-pc", "k2", PRINTERS)


# --- machine identity ---------------------------------------------------------------------------

def test_machine_info_is_stored_and_listed():
    conn = _mem()
    store.register_agent(conn, "office-pc", "k", PRINTERS, machine=PC_A)
    assert store.list_agents(conn, 60)[0]["machine"] == PC_A
    store.register_agent(conn, "office-pc", "k", PRINTERS, machine=dict(PC_A, os="Windows-11 24H2"))
    assert store.list_agents(conn, 60)[0]["machine"]["os"] == "Windows-11 24H2"   # kept current


def test_machine_info_is_optional_and_junk_is_ignored():
    conn = _mem()
    store.register_agent(conn, "a", "k1", PRINTERS)
    store.register_agent(conn, "b", "k2", PRINTERS, machine="not an object")
    store.register_agent(conn, "c", "k3", PRINTERS,
                         machine={"id": "x" * 500, "hostname": 42, "extra": "dropped"})
    machines = [a["machine"] for a in store.list_agents(conn, 60)]
    assert machines[0] is None and machines[1] is None
    assert machines[2] == {"id": "x" * 128}       # bounded, strings only, known keys only


def test_a_second_live_pc_with_the_same_key_is_refused():
    conn = _mem()
    store.register_agent(conn, "office-pc", "k", PRINTERS, machine=PC_A)
    with pytest.raises(store.KeyInUse, match="DESKTOP-A"):
        store.register_agent(conn, "DESKTOP-B", "k", PRINTERS, machine=PC_B)
    [agent] = store.list_agents(conn, 60)
    assert agent["name"] == "office-pc" and agent["machine"] == PC_A       # nothing changed


def test_a_replacement_pc_takes_over_the_key_once_the_old_one_is_offline():
    conn = _mem()
    first = store.register_agent(conn, "office-pc", "k", PRINTERS, machine=PC_A,
                                 now=time.time() - 600)
    again = store.register_agent(conn, "office-pc", "k", PRINTERS, machine=PC_B)
    assert again == first
    assert store.list_agents(conn, 60)[0]["machine"] == PC_B


def test_a_pc_that_could_not_read_its_id_is_not_treated_as_a_different_pc():
    conn = _mem()
    store.register_agent(conn, "office-pc", "k", PRINTERS, machine=PC_A)
    no_id = {k: v for k, v in PC_A.items() if k != "id"}
    store.register_agent(conn, "office-pc", "k", PRINTERS, machine=no_id)
    assert store.list_agents(conn, 60)[0]["machine"]["id"] == PC_A["id"]   # the id is kept


# --- HTTP ---------------------------------------------------------------------------------------

def test_register_over_http_renames_and_refuses_a_second_live_pc_with_a_reason():
    conn = _mem()
    httpd, base = _serve(conn)
    try:
        url = base + "/agent/register"
        code, first = _post(url, "k", {"name": "office-pc", "printers": PRINTERS, "machine": PC_A})
        assert code == 200
        code, again = _post(url, "k", {"name": "DESKTOP-A", "printers": PRINTERS, "machine": PC_A})
        assert (code, again) == (200, first)
        code, body = _post(url, "k", {"name": "DESKTOP-B", "printers": PRINTERS, "machine": PC_B})
        assert code == 409 and "another computer" in body["error"]
        assert "own key" in body["error"]
    finally:
        httpd.shutdown()


def test_computers_carry_the_machine_info():
    conn = _mem()
    store.register_agent(conn, "office-pc", "k", PRINTERS, machine=PC_A)
    [agent] = store.list_agents(conn, 60)
    pc = printapi.computer(agent)
    assert pc["hostname"] == "DESKTOP-A" and pc["name"] == "office-pc"
    store.register_agent(conn, "legacy", "k2", PRINTERS)
    legacy = printapi.computer(store.list_agents(conn, 60)[1])
    assert legacy["hostname"] == "legacy"                  # no machine info: the name stands in
