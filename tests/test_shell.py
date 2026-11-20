"""The shell tools: a command and files on one of the owner's machines, on
only where the owner said so, under a timeout and an output cap."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from fulla import hosts as hosts_mod
from fulla.config import HostConfig, load_config
from fulla.directives import DirectiveStore
from fulla.host import carrier
from fulla.hosts import HostStore, announce, parse_announcement
from fulla.install import Values, gather, parser, render_env
from fulla.mac import tools as mac
from fulla.shell import (
    MAX_OUTPUT,
    MAX_TIMEOUT,
    TIMED_OUT,
    Result,
    owner_started,
    run_command,
    run_origin,
    shell_tools,
    tool_names,
)
from fulla.toolworker import ToolWorker
from fulla.worker import build_agents, capability_map, host_tool_names, turn_note


class FakeRunner:
    def __init__(self):
        self.calls: list[tuple[list[str], str, int]] = []
        self.replies: list[Result] = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append((argv, cwd, timeout))
        return self.replies.pop(0) if self.replies else Result(0, "ok\n", "", 0.5)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "proj" / "src").mkdir(parents=True)
    (h / "proj" / "src" / "a.py").write_text("import os\nimport re\nx = 1\n")
    (h / "notes.md").write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n")
    return h


@pytest.fixture
def box(home):
    fake = FakeRunner()
    switch = {"on": True}
    started = {"by": "message"}
    tools = {t.name: t for t in shell_tools("box", lambda: switch["on"], run=fake, home=home, shell="/bin/bash",
                                            origin=lambda: started["by"])}
    tools["started"] = started
    return fake, switch, tools


def test_names_and_side_effects(box):
    _, _, tools = box
    assert [n for n in tools if n != "started"] == tool_names("box") == [
        "box_exec", "box_read_file", "box_write_file", "box_edit_file", "box_grep", "box_glob"]
    assert [n for n, t in tools.items() if n != "started" and t.side_effect] == ["box_exec", "box_write_file", "box_edit_file"]
    with pytest.raises(ValueError):
        shell_tools("My Box", lambda: True)


def test_everything_refuses_while_the_shell_is_off(box):
    fake, switch, tools = box
    switch["on"] = False
    refusal = "Refused: the shell on box is off. The owner can turn it on with FULLA_SHELL=1 in that machine's env file."
    assert tools["box_exec"].handler("ls") == refusal
    assert tools["box_read_file"].handler("notes.md") == refusal
    assert tools["box_write_file"].handler("x", "y") == refusal
    assert tools["box_edit_file"].handler("x", "a", "b") == refusal
    assert tools["box_grep"].handler("x") == refusal
    assert tools["box_glob"].handler("*") == refusal
    assert fake.calls == []
    switch["on"] = True  # read on every call: the worker turns it on after the tools exist
    assert tools["box_exec"].handler("ls").startswith("exit 0")


def test_exec_runs_in_the_directory_under_a_timeout(box, home):
    fake, _, tools = box
    out = tools["box_exec"].handler("make test")
    assert fake.calls[0] == (["/bin/bash", "-c", "make test"], str(home), 120)
    assert out == "exit 0 (0.5s, in ~ on box)\n--- stdout\nok"
    tools["box_exec"].handler("ls", cwd="proj/src")
    assert fake.calls[-1][1] == str(home / "proj" / "src")
    tools["box_exec"].handler("ls", cwd=str(home / "proj"), timeout=5000)
    assert fake.calls[-1][1:] == (str(home / "proj"), MAX_TIMEOUT)
    tools["box_exec"].handler("ls", timeout=0)
    assert fake.calls[-1][2] == 1
    n = len(fake.calls)
    assert tools["box_exec"].handler("ls", cwd="nowhere") == "No directory 'nowhere' on box."
    assert tools["box_exec"].handler("  ") == "Nothing to run: give a command."
    assert len(fake.calls) == n


def test_exec_reports_exit_code_stderr_timeouts_and_clips(box):
    fake, _, tools = box
    fake.replies.append(Result(2, "", "no such file\n", 0.2))
    assert tools["box_exec"].handler("cat x") == "exit 2 (0.2s, in ~ on box)\n--- stderr\nno such file"
    fake.replies.append(Result(TIMED_OUT, "partial\n", "", 120.0))
    assert tools["box_exec"].handler("sleep 999").startswith(
        "Timed out after 120s and was killed (120.0s, in ~ on box)\n--- stdout\npartial")
    fake.replies.append(Result(0, "x" * (MAX_OUTPUT + 1000), "", 1.0))
    out = tools["box_exec"].handler("yes | head -c 30000")
    assert "characters left out" in out and len(out) < MAX_OUTPUT + 200 and out.endswith("x" * 100)


def test_file_tools_read_write_edit_and_search_for_real(box, home):
    fake, _, tools = box
    read, write, edit, grep, glob = (tools[f"box_{n}"].handler for n in ("read_file", "write_file", "edit_file", "grep", "glob"))
    assert read("notes.md", offset=3, limit=2) == "~/notes.md, lines 3-4 of 10 (6 more after these; read on with offset)\nline 3\nline 4"
    assert read(str(home / "notes.md"), limit=1).startswith("~/notes.md, lines 1-1 of 10")
    assert read("notes.md", offset=50) == "~/notes.md has 10 lines; offset 50 is past the end."
    assert read("missing").startswith("Could not read missing on box: No such file")
    (home / "empty").touch()
    assert read("empty") == "~/empty is empty."

    assert write("new/dir/f.txt", "hello\n") == "Wrote 6 bytes to ~/new/dir/f.txt on box."
    assert (home / "new" / "dir" / "f.txt").read_text() == "hello\n"

    assert edit("proj/src/a.py", "x = 1", "x = 2") == "Replaced 1 occurrence in ~/proj/src/a.py on box."
    assert (home / "proj" / "src" / "a.py").read_text().endswith("x = 2\n")
    assert edit("proj/src/a.py", "import", "from") == "old_str occurs 2 times in ~/proj/src/a.py; make it unique, or set replace_all."
    assert edit("proj/src/a.py", "import", "from", replace_all=True) == "Replaced 2 occurrences in ~/proj/src/a.py on box."
    assert edit("proj/src/a.py", "zzz", "") == "old_str is not in ~/proj/src/a.py."
    assert edit("proj/src/a.py", "", "x") == "old_str is required: the exact text to replace."

    assert grep("^from", path="proj", glob="*.py") == "~/proj/src/a.py:1:from os\n~/proj/src/a.py:2:from re"
    assert grep("line 1", path="notes.md") == "~/notes.md:1:line 1\n~/notes.md:10:line 10"
    assert grep("zzz") == "No match for 'zzz' under ~ on box."
    assert grep("(") .startswith("Not a regular expression")
    assert grep("x", path="nope") == "No such path 'nope' on box."
    assert glob("**/*.py") == "~/proj/src/a.py"
    assert glob("*.txt", path="new/dir") == "~/new/dir/f.txt"
    assert glob("*.rs", path="proj") == "Nothing matches '*.rs' under ~/proj on box."
    assert fake.calls == []  # none of it went through a shell


def test_run_command_for_real_captures_kills_on_timeout_and_hides_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("NORNS_API_KEY", "nrn_secret")
    r = run_command([sys.executable, "-c", "import os,sys; print('out', os.environ.get('NORNS_API_KEY')); print('err', file=sys.stderr); sys.exit(3)"],
                    str(tmp_path), 30)
    assert (r.rc, r.out, r.err) == (3, "out None\n", "err\n") and r.seconds < 10
    # A shell's children die with it: the sleep is in the group the kill reaches.
    r = run_command(["/bin/sh", "-c", "sleep 30 & sleep 30"], str(tmp_path), 1)
    assert r.rc == TIMED_OUT and r.seconds < 5
    r = run_command(["/no/such/binary"], str(tmp_path), 5)
    assert r.rc == 127 and "No such file" in r.err


def test_the_mac_and_the_host_serve_them_and_the_switch_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("NORNS_API_KEY", "k")
    monkeypatch.setenv("FULLA_HOST", "box")
    monkeypatch.setenv("FULLA_SHELL", "1")
    config = load_config()
    assert config.host == HostConfig(name="box", tmux_sessions=[], shell=True) and config.mac.shell is True
    agent = carrier(config)
    assert [t.name for t in agent.tools][-6:] == tool_names("box") and "box_tmux_list" in [t.name for t in agent.tools]
    assert set(tool_names("mac")) <= {t.name for t in mac.mac_tools}
    monkeypatch.setenv("FULLA_SHELL", "")
    config = load_config()
    assert config.host.shell is False and config.mac.shell is False
    mac.init(config=config.mac, run=lambda *a, **k: "")
    assert mac.mac_exec.handler("ls").startswith("Refused: the shell on mac is off")
    assert mac.mac_glob.handler("*").startswith("Refused: the shell on mac is off")


def test_the_announcement_carries_the_switch_and_the_manager_allowlists_both_kinds(monkeypatch, tmp_path):
    seen = {}

    def fake_post(url, *, content, headers, timeout):
        seen["body"] = json.loads(content)
        return httpx.Response(200, json={"ok": True, "known": ["mac"]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(hosts_mod.httpx, "post", fake_post)
    announce("https://fulla.fly.dev", "k", name="box", sessions=[], worker_id="fulla-box", system="linux", shell=True)
    assert seen["body"]["shell"] is True
    store = HostStore(tmp_path / "hosts.json")
    assert store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": [], "shell": True}))
    assert not store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": [], "shell": True}))
    assert store.register(parse_announcement({"name": "box", "platform": "linux", "sessions": [], "shell": False}))
    assert store.tool_names() == [*hosts_mod.tool_names("box"), *tool_names("box")]
    assert HostStore(tmp_path / "hosts.json").all()[0].shell is False
    out = store.render("Robin")
    assert "`mac_` is Robin's Mac (tmux sessions allowed: not announced yet; shell: not announced yet)." in out
    assert "`box_` is box, a linux machine (tmux sessions allowed: none; shell: off; its worker last started" in out
    store.register(parse_announcement({"name": "mac", "platform": "mac", "sessions": ["w"], "shell": True}))
    assert "`mac_` is Robin's Mac (tmux sessions allowed: w; shell: on)." in store.render("Robin")

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    manager = build_agents(load_config(), DirectiveStore(tmp_path / "d.json"), store)[0]
    for name in ("mac_exec", "mac_read_file", "box_exec", "box_glob", "box_tmux_send"):
        assert name in manager.allowed_tools
    assert set(tool_names("box")) <= set(host_tool_names(store))
    assert "`<name>_exec` runs a" in manager.system_prompt
    cmap = capability_map("Robin", store)
    assert "(`mac_`, `box_`): tmux_list" in cmap and ", exec, read_file," in cmap
    assert "where Robin turned the shell on" in cmap


def test_turn_note_counts_shell_tools_as_the_machines(tmp_path):
    note = turn_note(["now", "mac_exec", "box_exec", "box_tmux_read"], ["box", "pi"])
    assert "The `mac_` tools in your tool list right now: mac_exec. The Mac is connected" in note
    assert "The `box_` tools in your tool list right now: box_exec, box_tmux_read. The machine box is connected" in note
    assert "No `pi_` tool is in your tool list right now: the machine pi is off" in note


def test_the_installer_asks_for_the_switch(home, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    v = gather("host", parser("host").parse_args(["--norns-url", "https://n", "--norns-api-key", "k", "--fulla-url",
                                                 "https://f", "--host", "box", "--shell"]), repo=tmp_path, interactive=False)
    assert v.shell is True and "\nFULLA_SHELL=1\n" in render_env("host", v)
    assert "\nFULLA_SHELL=\n" in render_env("mac", Values("https://n", "k", "https://f"))
    (tmp_path / ".env.mac").write_text("NORNS_URL=https://n\nNORNS_API_KEY=k\nFULLA_URL=https://f\nFULLA_SHELL=1\n")
    v = gather("mac", parser("mac").parse_args([]), repo=tmp_path, interactive=False)
    assert v.shell is True and "FULLA_SHELL" not in v.extras
    v = gather("mac", parser("mac").parse_args(["--no-shell"]), repo=tmp_path, interactive=False)
    assert v.shell is False
    answers = iter(["", "n", "", "", ""])  # sessions first, then the shell
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    v = gather("mac", parser("mac").parse_args([]), repo=tmp_path, interactive=True)
    assert v.shell is False


def test_only_a_run_the_owners_message_started_may_act(box, home):
    fake, _, tools = box
    for kind, why in (("schedule", "started by a timer (schedule)"), (None, "nothing says what started this run")):
        tools["started"]["by"] = kind
        for name, args in (("box_exec", ("ls",)), ("box_write_file", ("x", "y")), ("box_edit_file", ("notes.md", "line 1", "z"))):
            out = tools[name].handler(*args)
            assert out.startswith("Refused: ") and why in out and "Reading is fine" in out, (kind, name, out)
        # Reading goes on: a scheduled turn may look at a machine, not change it.
        assert tools["box_read_file"].handler("notes.md", limit=1).startswith("~/notes.md, lines 1-1")
        assert tools["box_grep"].handler("line 1", path="notes.md").startswith("~/notes.md:1:")
        assert tools["box_glob"].handler("*.md") == "~/notes.md"
    assert fake.calls == [] and (home / "x").exists() is False
    tools["started"]["by"] = "message"
    assert tools["box_exec"].handler("ls").startswith("exit 0")
    assert owner_started(lambda: "message") is None and owner_started(lambda: "webhook").startswith("Refused")


def test_run_origin_asks_norns_once_per_run_and_refuses_when_it_cannot():
    from types import SimpleNamespace

    from fulla import runctx

    asked = []

    class Api:
        def get_run(self, run_id):
            asked.append(run_id)
            if run_id == 13:
                raise RuntimeError("norns down")
            return SimpleNamespace(trigger_type="schedule" if run_id == 2 else "message")

    origin = run_origin(Api())
    assert origin() is None and asked == []  # no run at all: nothing to ask, nothing allowed
    for run_id, expect in ((1, "message"), (1, "message"), (2, "schedule"), (13, None), (13, None)):
        token = runctx.set_current_run(run_id)
        try:
            assert origin() == expect
        finally:
            runctx.reset_current_run(token)
    assert asked == [1, 2, 13, 13]  # remembered per run; a failure is asked again, and refused meanwhile


def test_the_mac_queue_holds_no_shell_and_the_mac_worker_wires_the_origin(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fulla import runctx
    from fulla.mac.worker import carrier as mac_carrier

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    monkeypatch.setenv("FULLA_SHELL", "1")
    config = load_config()
    manager, _, mac_task, *_ = build_agents(config, DirectiveStore(tmp_path / "d.json"))
    assert set(tool_names("mac")) <= set(manager.allowed_tools)
    assert not set(tool_names("mac")) & set(mac_task.allowed_tools) and "mac_things_add" in mac_task.allowed_tools

    class Api:
        def get_run(self, run_id):
            return SimpleNamespace(trigger_type="schedule")

    mac_carrier(config, Api())
    token = runctx.set_current_run(5)
    try:
        assert "started by a timer" in mac.mac_exec.handler("ls")
        assert mac.mac_glob.handler("*.nothing", path=str(tmp_path)).startswith("Nothing matches")
    finally:
        runctx.reset_current_run(token)
    mac_carrier(config)  # no client: the shell reads and never acts
    assert "nothing says what started" in mac.mac_write_file.handler(str(tmp_path / "f"), "x")


def test_the_tool_worker_lends_the_run_id_so_the_shell_can_ask_what_started_it(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from norns import Norns

    from fulla import runctx

    seen = []

    async def fake_run(self, ws, payload, tools_by_name):
        # What the SDK would do: call the tool handler on this task. Here, the shell asks its origin.
        seen.append(runctx.current_run_id())
        return tools_by_name["box_exec"].handler("true")

    monkeypatch.setattr(Norns, "_run_tool_task", fake_run)

    class Api:
        def get_run(self, run_id):
            return SimpleNamespace(trigger_type="message" if run_id == 42 else "schedule")

    tools = {t.name: t for t in shell_tools("box", lambda: True, run=lambda argv, cwd, timeout: Result(0, "", "", 0.1),
                                            home=Path("/"), origin=run_origin(Api()))}
    worker = ToolWorker("http://unused")
    assert asyncio.run(worker._run_tool_task(None, {"run_id": 42}, tools)).startswith("exit 0")  # pyright: ignore[reportAttributeAccessIssue]
    assert asyncio.run(worker._run_tool_task(None, {"run_id": 43}, tools)).startswith("Refused: this run was started by a timer")  # pyright: ignore[reportAttributeAccessIssue]
    assert seen == [42, 43] and runctx.current_run_id() is None
