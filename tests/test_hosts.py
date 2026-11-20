"""The registry of machines: announced by their workers, remembered by Fulla,
rendered into the manager's prompt, allowlist and per-turn note."""

import json
import logging
import threading

import httpx
import pytest

from fulla import hosts as hosts_mod
from fulla.hosts import HostStore, announce, parse_announcement, sign
from fulla.norns_api import NornsApi
from fulla.server import serve
from fulla.shell import tool_names as shell_names
from fulla.signature import verify
from fulla.tmux import tool_names
from fulla.wake import ServingNorns

KEY = "norns-api-key"


def test_parse_announcement_validates():
    h = parse_announcement({"name": "box", "platform": "linux", "sessions": ["work", " ", "scratch"], "worker_id": "fulla-box"})
    assert (h.name, h.platform, h.sessions, h.worker_id) == ("box", "linux", ["work", "scratch"], "fulla-box")
    assert h.seen_at.endswith("+00:00") and h.prefix == "box_" and h.label == "the machine box"
    assert h.tool_names() == ["box_tmux_list", "box_tmux_read", "box_tmux_send", "box_tmux_new", "box_tmux_rename",
                              "box_exec", "box_read_file", "box_write_file", "box_edit_file", "box_grep", "box_glob"]
    assert h.shell is False and parse_announcement({"name": "box", "shell": True}).shell is True
    assert parse_announcement({"name": "pi"}).worker_id == "fulla-pi"
    with pytest.raises(ValueError, match="name must be"):
        parse_announcement({"name": "My Box"})
    with pytest.raises(ValueError, match="name must be"):
        parse_announcement({})
    with pytest.raises(ValueError, match="sessions must be a list"):
        parse_announcement({"name": "box", "sessions": "work"})
    mac = parse_announcement({"name": "mac", "platform": "mac", "sessions": ["*"]})
    assert mac.prefix == "mac_" and mac.label == "the Mac"


def test_store_remembers_and_says_when_the_manager_must_change(tmp_path):
    store = HostStore(tmp_path / "hosts.json")
    assert store.names() == [] and store.tool_names() == []
    assert store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": ["work"]}))
    assert not store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": ["work"]}))
    assert store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": ["work", "lab"]}))
    assert store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": ["norns"]}))
    assert store.register(parse_announcement({"name": "alpha", "platform": "linux", "sessions": []}))
    assert store.names() == ["alpha", "box"]  # the Mac is always known; it is not a "host"
    assert store.tool_names() == [*tool_names("alpha"), *shell_names("alpha"), *tool_names("box"), *shell_names("box")]
    assert "box_tmux_new" in store.tool_names() and "alpha_tmux_rename" in store.tool_names()
    assert [h.name for h in store.all()] == ["mac", "alpha", "box"]

    again = HostStore(tmp_path / "hosts.json")
    assert again.names() == ["alpha", "box"] and again.all()[0].sessions == ["norns"]
    assert again.forget("alpha") and not again.forget("alpha")
    assert HostStore(tmp_path / "hosts.json").names() == ["box"]


def test_render_lists_every_machine_by_prefix(tmp_path):
    store = HostStore(tmp_path / "hosts.json")
    out = store.render("Robin")
    assert out.startswith("The machines you know of, by the prefix of their tools:\n")
    assert "`mac_` is Robin's Mac (tmux sessions allowed: not announced yet; shell: not announced yet)." in out
    store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": ["norns", "work"]}))
    store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": []}))
    out = store.render("Robin")
    assert "`mac_` is Robin's Mac (tmux sessions allowed: norns, work; shell: off)." in out
    assert "`box_` is box, a linux machine (tmux sessions allowed: none; shell: off; its worker last started 20" in out


def test_sign_round_trips_with_the_servers_verify():
    body = b'{"name":"box"}'
    assert verify(KEY, body, sign(KEY, body))
    assert not verify("other", body, sign(KEY, body))
    assert not verify(KEY, body + b" ", sign(KEY, body))


def test_announce_posts_a_signed_body(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="fulla.hosts")
    seen = {}

    def fake_post(url, *, content, headers, timeout):
        seen.update(url=url, content=content, headers=headers)
        return httpx.Response(200, json={"ok": True, "known": ["mac", "box"]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(hosts_mod.httpx, "post", fake_post)
    assert announce("https://fulla.fly.dev/", KEY, name="box", sessions=["work"], worker_id="fulla-box", system="linux")
    assert seen["url"] == "https://fulla.fly.dev/hosts"
    assert json.loads(seen["content"]) == {"name": "box", "sessions": ["work"], "worker_id": "fulla-box", "platform": "linux",
                                               "imessage": {}, "shell": False, "machine_id": ""}
    assert verify(KEY, seen["content"], seen["headers"]["X-Norns-Signature"])
    assert "machines known: mac, box" in caplog.text

    # No URL: nothing sent, nothing raised. A refusal or a dead app: the same.
    assert not announce("", KEY, name="box", sessions=[], worker_id="w")

    def refuse(url, **kw):
        return httpx.Response(401, json={"error": "bad signature"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(hosts_mod.httpx, "post", refuse)
    assert not announce("https://fulla.fly.dev", KEY, name="box", sessions=[], worker_id="w")

    def dead(url, **kw):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(hosts_mod.httpx, "post", dead)
    assert not announce("https://fulla.fly.dev", KEY, name="box", sessions=[], worker_id="w")


class FakeChannel:
    def handle(self, inbound):
        pass

    def handle_event(self, event):
        pass


@pytest.fixture
def server():
    announced = []

    def on_host(data):
        host = parse_announcement(data)
        announced.append(host)
        return ["mac", host.name]

    srv = serve(FakeChannel(), port=0, telegram_secret="t", norns_secret="n",
                host_secret=KEY, on_host=on_host)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield announced, httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


def test_hosts_endpoint_takes_a_signed_announcement(server):
    announced, http = server
    body = json.dumps({"name": "box", "platform": "linux", "sessions": ["work"], "worker_id": "fulla-box"}).encode()
    resp = http.post("/hosts", content=body, headers={"X-Norns-Signature": sign(KEY, body)})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "known": ["mac", "box"]}
    assert [h.name for h in announced] == ["box"]

    assert http.post("/hosts", content=body, headers={"X-Norns-Signature": sign("wrong", body)}).status_code == 401
    assert http.post("/hosts", content=body).status_code == 401
    bad = b'{"name": "My Box"}'
    resp = http.post("/hosts", content=bad, headers={"X-Norns-Signature": sign(KEY, bad)})
    assert resp.status_code == 400 and "name must be" in resp.json()["error"]
    assert len(announced) == 1


def test_hosts_endpoint_is_absent_without_a_handler():
    srv = serve(FakeChannel(), port=0, telegram_secret="t", norns_secret="n")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
        body = b'{"name":"box"}'
        assert http.post("/hosts", content=body, headers={"X-Norns-Signature": sign(KEY, body)}).status_code == 404
    finally:
        srv.shutdown()


def test_update_agent_keeps_the_rest_of_model_config():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.method == "GET" and request.url.path == "/api/v1/agents":
            return httpx.Response(200, json={"data": [{"id": 4, "name": "fulla"}]})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"id": 4, "name": "fulla", "model_config": {
                "mode": "conversation", "context_window": 50, "subagents": {"mode": "disabled"},
                "tools": {"mode": "allowlist", "allowed_tools": ["now"]}}}})
        return httpx.Response(200, json={"data": {"id": 4}})

    api = NornsApi("http://norns", "k")
    api._http = httpx.Client(base_url="http://norns", transport=httpx.MockTransport(handler))
    api.update_agent("fulla", system_prompt="hi", allowed_tools=["now", "box_tmux_read"])
    method, path, body = calls[-1]
    assert (method, path) == ("PUT", "/api/v1/agents/4")
    assert body == {"system_prompt": "hi", "model_config": {
        "mode": "conversation", "context_window": 50, "subagents": {"mode": "disabled"},
        "tools": {"mode": "allowlist", "allowed_tools": ["now", "box_tmux_read"]}}}


def test_serving_norns_reads_the_registry_each_turn(tmp_path, monkeypatch):
    import asyncio

    from norns import Norns

    seen = {}

    async def fake(self, task):
        seen["prompt"] = task["system_prompt"]
        return {}

    monkeypatch.setattr(Norns, "_handle_llm_task", fake)
    store = HostStore(tmp_path / "hosts.json")
    worker = ServingNorns("http://unused", api_key="k", hosts=store.names)
    task = {"system_prompt": "... `mac_things_add` ...", "tools": [{"name": "now"}], "messages": []}
    asyncio.run(worker._handle_llm_task(task))
    assert "box" not in seen["prompt"]
    store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": ["work"]}))
    asyncio.run(worker._handle_llm_task(dict(task, tools=[{"name": "box_tmux_read"}])))
    assert "The machine box is connected on this turn" in seen["prompt"] and "the Mac is off" in seen["prompt"]


def test_the_mac_announces_who_imessage_may_go_to(tmp_path):
    store = HostStore(tmp_path / "hosts.json")
    assert store.imessage_allow() == {}
    store.register(parse_announcement({"name": "mac", "imessage": {"Mom": "mom@icloud.com"}}))
    assert HostStore(tmp_path / "hosts.json").imessage_allow() == {"Mom": "mom@icloud.com"}
    # Only the Mac sends iMessages; another machine's list is not kept.
    assert parse_announcement({"name": "box", "imessage": {"X": "+1"}}).imessage == {}
    with pytest.raises(ValueError):
        parse_announcement({"name": "mac", "imessage": ["mom"]})
