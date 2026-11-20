"""One name per machine, what each is for, and the tools that keep them apart."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from fulla import hosts as hosts_mod
from fulla.hosts import (
    HostStore,
    NameTaken,
    Refusals,
    announce,
    announce_until_named,
    parse_announcement,
    sign,
)
from fulla.install import default_host_name, gather, parser, render_env
from fulla.server import serve
from fulla.tools import machines

KEY = "nrn_test"


def test_a_name_stays_with_the_machine_that_took_it(tmp_path):
    store = HostStore(tmp_path / "hosts.json")
    first = parse_announcement({"name": "box", "platform": "linux", "sessions": [], "machine_id": "aaaa"})
    assert store.register(first) and store.get("box").machine_id == "aaaa"
    # The same machine again, with fewer or more things to say: fine.
    assert not store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": [], "machine_id": "aaaa"}))
    # Another machine with the same name: refused, and nothing changes.
    with pytest.raises(NameTaken, match="belongs to another machine"):
        store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": ["w"], "machine_id": "bbbb"}))
    with pytest.raises(NameTaken):
        store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": []}))  # no id at all
    assert store.get("box").sessions == [] and HostStore(tmp_path / "hosts.json").get("box").machine_id == "aaaa"
    # Forgotten, the name is free; the next machine takes it.
    assert store.forget("box")
    assert store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": [], "machine_id": "bbbb"}))
    # A machine installed before ids existed is adopted by the first id that announces its name.
    assert store.register(parse_announcement({"name": "pi", "platform": "linux", "sessions": []}))
    assert store.get("pi").machine_id == ""
    assert not store.register(parse_announcement({"name": "pi", "platform": "linux", "sessions": [], "machine_id": "cccc"}))
    assert store.get("pi").machine_id == "cccc"
    with pytest.raises(NameTaken):
        store.register(parse_announcement({"name": "pi", "platform": "linux", "sessions": [], "machine_id": "dddd"}))


def test_what_a_machine_is_for_survives_announcements_and_shows_in_the_prompt(tmp_path):
    store = HostStore(tmp_path / "hosts.json")
    store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": [], "machine_id": "a"}))
    assert not store.describe("nope", "x")
    assert store.describe("box", "  my dev server with the widgets checkout  ")
    assert store.get("box").purpose == "my dev server with the widgets checkout"
    store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": ["w"], "shell": True, "machine_id": "a"}))
    assert HostStore(tmp_path / "hosts.json").get("box").purpose == "my dev server with the widgets checkout"
    out = store.render("Robin")
    assert "`box_` is box, a linux machine, my dev server with the widgets checkout (tmux sessions allowed: w; shell: on;" in out
    store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": []}))
    store.describe("mac", "my laptop")
    assert "`mac_` is Robin's Mac, my laptop (tmux sessions allowed: none; shell: off)." in store.render("Robin")


def test_the_announcement_carries_the_id_and_a_409_stops_the_worker(monkeypatch):
    seen = {}

    def fake_post(url, *, content, headers, timeout):
        seen["body"] = json.loads(content)
        return httpx.Response(409, json={"error": "the name 'box' belongs to another machine; give this one another FULLA_HOST"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(hosts_mod.httpx, "post", fake_post)
    with pytest.raises(NameTaken, match="belongs to another machine"):
        announce("https://fulla.fly.dev", KEY, name="box", sessions=[], worker_id="fulla-box", machine_id="abcd")
    assert seen["body"]["machine_id"] == "abcd"


class FakeChannel:
    def handle(self, *a, **k):
        return None

    def claim(self, *a, **k):
        raise AssertionError


def test_the_server_answers_409_for_a_taken_name(tmp_path):
    store = HostStore(tmp_path / "hosts.json")

    def on_host(data):
        host = parse_announcement(data)
        store.register(host)
        return [h.name for h in store.all()]

    srv = serve(FakeChannel(), port=0, telegram_secret="t", norns_secret=KEY, on_host=on_host, host_secret=KEY)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
        first = json.dumps({"name": "box", "machine_id": "one"}).encode()
        assert http.post("/hosts", content=first, headers={"X-Norns-Signature": sign(KEY, first)}).status_code == 200
        second = json.dumps({"name": "box", "machine_id": "two"}).encode()
        resp = http.post("/hosts", content=second, headers={"X-Norns-Signature": sign(KEY, second)})
        assert resp.status_code == 409 and "belongs to another machine" in resp.json()["error"]
        assert store.get("box").machine_id == "one"
    finally:
        srv.shutdown()


@pytest.fixture
def tools(tmp_path):
    store = HostStore(tmp_path / "hosts.json")
    pushes = []
    machines.init(store=store, push_prompt=lambda: pushes.append(1))
    return store, pushes


def test_list_describe_and_forget(tools):
    store, pushes = tools
    assert machines.list_machines.handler() == "No machine has announced itself yet."
    store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": ["work"], "shell": True}))
    store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": [], "machine_id": "a"}))
    out = machines.list_machines.handler()
    assert out.startswith("- `mac_` the Mac (nothing said about what it is for). mac; tmux sessions: work; shell: on;")
    assert "- `box_` box (nothing said about what it is for). linux; tmux sessions: none; shell: off;" in out

    assert machines.describe_machine.handler("box_", "my dev server") == "Noted: box is my dev server."
    assert pushes == [1] and store.get("box").purpose == "my dev server"
    assert "- `box_` box: my dev server. linux;" in machines.list_machines.handler()
    assert machines.describe_machine.handler("pi", "x") == "No machine named 'pi'; known: mac, box."
    assert machines.describe_machine.handler("box", "  ") == "Say what the machine is for."
    assert machines.describe_machine.handler("Mac", "my laptop") == "Noted: the Mac is my laptop."

    assert machines.forget_machine.handler("pi") == "No machine named 'pi' to forget."
    assert machines.forget_machine.handler("box").startswith("Forgot box.")
    assert store.get("box") is None and len(pushes) == 3
    # The Mac too: a rebuilt Mac has a new id, and this is how it gets its name back.
    store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": [], "machine_id": "old"}))
    with pytest.raises(NameTaken):
        store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": [], "machine_id": "new"}))
    assert machines.forget_machine.handler("mac").startswith("Forgot the Mac.")
    assert store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": [], "machine_id": "new"}))
    assert len(pushes) == 4
    assert machines.describe_machine.handler("box", "x").startswith("No machine named 'box'")
    assert all(t.side_effect for t in (machines.describe_machine, machines.forget_machine))
    assert not machines.list_machines.side_effect


def test_the_installer_makes_an_id_once_and_names_the_machine_after_the_host(monkeypatch, tmp_path):
    monkeypatch.setattr("fulla.install.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("fulla.install.socket.gethostname", lambda: "Dev-Box-01.local")
    assert default_host_name() == "devbox01"
    monkeypatch.setattr("fulla.install.socket.gethostname", lambda: "mac")
    assert default_host_name() == ""
    monkeypatch.setattr("fulla.install.socket.gethostname", lambda: "01-server")
    assert default_host_name() == "server"

    args = parser("host").parse_args(["--norns-url", "https://n", "--norns-api-key", "k", "--fulla-url", "https://f",
                                      "--host", "box", "--no-shell"])
    v = gather("host", args, repo=tmp_path, interactive=False)
    assert len(v.machine_id) == 16 and "FULLA_MACHINE_ID" not in v.extras
    text = render_env("host", v)
    assert f"\nFULLA_MACHINE_ID={v.machine_id}\n" in text
    # A second run keeps the id: the name stays with the machine across reinstalls.
    (tmp_path / ".config" / "fulla").mkdir(parents=True)
    (tmp_path / ".config" / "fulla" / "host.env").write_text(text)
    again = gather("host", parser("host").parse_args(["--no-shell"]), repo=tmp_path, interactive=False)
    assert again.machine_id == v.machine_id and again.host == "box"
    # The hostname is the default name when nothing was set before.
    monkeypatch.setattr("fulla.install.socket.gethostname", lambda: "pi4")
    (tmp_path / ".config" / "fulla" / "host.env").unlink()
    fresh = gather("host", parser("host").parse_args(["--norns-url", "https://n", "--norns-api-key", "k",
                                                      "--fulla-url", "https://f", "--no-shell"]), repo=tmp_path, interactive=False)
    assert fresh.host == "pi4" and fresh.machine_id != v.machine_id


def test_a_refused_worker_waits_and_tries_again_instead_of_exiting(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.ERROR, logger="fulla.hosts")
    answers = [409, 409, 200]

    def fake_post(url, *, content, headers, timeout):
        code = answers.pop(0)
        body = {"error": "the name 'box' belongs to another machine"} if code == 409 else {"ok": True, "known": ["box"]}
        return httpx.Response(code, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(hosts_mod.httpx, "post", fake_post)
    naps = []
    assert announce_until_named("https://fulla.fly.dev", KEY, sleep=naps.append, wait=5, name="box", sessions=[],
                                worker_id="fulla-box", machine_id="two")
    assert naps == [5, 5] and answers == []
    assert caplog.text.count("not serving tools: the name 'box' belongs to another machine") == 2
    assert "forget the name" in caplog.text


def test_the_owner_hears_about_a_taken_name_once_a_day():
    now = [1000.0]
    r = Refusals(every=100, clock=lambda: now[0])
    assert r.should_tell("box") and not r.should_tell("box")
    assert r.should_tell("pi")
    now[0] += 99
    assert not r.should_tell("box")
    now[0] += 1
    assert r.should_tell("box")


def test_a_sleeping_fly_app_gets_a_second_try(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="fulla.hosts")
    answers = ["timeout", "timeout", 200]

    def fake_post(url, *, content, headers, timeout):
        a = answers.pop(0)
        if a == "timeout":
            raise httpx.ReadTimeout("The read operation timed out")
        return httpx.Response(200, json={"ok": True, "known": ["mac"]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(hosts_mod.httpx, "post", fake_post)
    naps = []
    assert announce_until_named("https://fulla.fly.dev", KEY, sleep=naps.append, backoff=(1, 2, 3), name="mac",
                                sessions=[], worker_id="fulla-mac", machine_id="x")
    assert naps == [1, 2] and answers == []
    assert "trying again in 1s" in caplog.text and "trying again in 2s" in caplog.text

    # Still nothing after the last pause: give up for this start, and say so; the worker serves anyway.
    def dead(url, **kw):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(hosts_mod.httpx, "post", dead)
    naps.clear()
    assert not announce_until_named("https://fulla.fly.dev", KEY, sleep=naps.append, backoff=(1, 2), name="mac",
                                    sessions=[], worker_id="fulla-mac")
    assert naps == [1, 2] and "after 3 tries" in caplog.text and "serving anyway" in caplog.text
    # The plain announce keeps its old contract: one try, False when unreachable.
    assert not announce("https://fulla.fly.dev", KEY, name="mac", sessions=[], worker_id="w")
