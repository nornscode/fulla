"""The tmux tools: an allowlist of sessions on one machine, read and typed into."""

import subprocess

import pytest

from fulla import shell, tmux
from fulla.config import HostConfig, MacConfig, load_config
from fulla.directives import DirectiveStore
from fulla.host import carrier
from fulla.hosts import HostStore, parse_announcement
from fulla.mac import tools as mac
from fulla.toolworker import ToolWorker
from fulla.worker import build_agents, host_tool_names, turn_note

PANES = (
    "work\t0\t0\teditor\tnvim\t/home/me/proj\t*\n"
    "work\t1\t0\tshell\tzsh\t/home/me/proj\t\n"
    "secrets\t0\t0\tpass\tgpg\t/home/me\t*\n"
)


class FakeTmux:
    """Answers like a tmux server with the panes above."""

    def __init__(self):
        self.calls = []
        self.screen = "$ make test\nok\n$ \n\n"
        self.error = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.error:
            raise tmux.TmuxError(self.error)
        if args[0] == "list-panes":
            return PANES.rstrip("\n")
        if args[0] == "display-message":
            target = args[args.index("-t") + 1]
            session = target.split(":")[0]
            if session not in ("work", "secrets"):
                raise tmux.TmuxError(f"can't find session: {session}")
            return session
        if args[0] == "capture-pane":
            return self.screen
        return ""


@pytest.fixture
def box():
    fake = FakeTmux()
    allow = ["work"]
    tools = {t.name: t for t in tmux.tmux_tools("box", lambda: allow, run=fake)}
    return fake, allow, tools


def test_names_and_side_effects(box):
    _, _, tools = box
    assert list(tools) == tmux.tool_names("box") == [
        "box_tmux_list", "box_tmux_read", "box_tmux_send", "box_tmux_new", "box_tmux_rename"]
    assert all(tools[n].side_effect for n in ("box_tmux_send", "box_tmux_new", "box_tmux_rename"))
    assert not tools["box_tmux_read"].side_effect
    with pytest.raises(ValueError):
        tmux.tmux_tools("My-Box", list)


def test_list_shows_only_allowed_sessions(box):
    _, allow, tools = box
    out = tools["box_tmux_list"].handler()
    assert out.splitlines() == [
        "work:0.0*  editor  nvim  /home/me/proj",
        "work:1.0  shell  zsh  /home/me/proj",
    ]
    assert "secrets" not in out
    allow[:] = ["*"]
    assert "secrets:0.0*  pass  gpg  /home/me" in tools["box_tmux_list"].handler()
    allow[:] = ["nothing"]
    assert tools["box_tmux_list"].handler() == "No tmux sessions Fulla may use are running on box."


def test_everything_is_refused_when_off(box):
    fake, allow, tools = box
    allow[:] = []
    for name, args in (("box_tmux_list", ()), ("box_tmux_read", ("work",)), ("box_tmux_send", ("work", "ls"))):
        out = tools[name].handler(*args)
        assert out.startswith("Refused: tmux access on box is off") and "FULLA_TMUX_SESSIONS" in out
    assert fake.calls == []


def test_read_checks_the_resolved_session(box):
    fake, _, tools = box
    assert tools["box_tmux_read"].handler("work:1.0", 10) == "$ make test\nok\n$"
    assert fake.calls == [
        ("display-message", "-p", "-t", "work:1.0", "#{session_name}"),
        ("capture-pane", "-p", "-e", "-t", "work:1.0", "-S", "-9"),
    ]
    out = tools["box_tmux_read"].handler("secrets:0")
    assert out == "Refused: tmux session 'secrets' is not one Fulla may use on box. Allowed: work."
    assert fake.calls[-1][0] == "display-message"  # never captured
    assert tools["box_tmux_read"].handler("nope").startswith("Could not find tmux target 'nope' on box")
    assert tools["box_tmux_read"].handler("").startswith("target is required")


def test_read_clamps_lines_and_reports_errors(box):
    fake, _, tools = box
    tools["box_tmux_read"].handler("work", 5000)
    assert fake.calls[-1][-1] == "-499"
    tools["box_tmux_read"].handler("work", 0)
    assert fake.calls[-1][-1] == "-0"
    fake.screen = ""
    assert tools["box_tmux_read"].handler("work") == "work on box shows nothing."
    fake.screen = "x" * 5000
    assert "1000 more characters" in tools["box_tmux_read"].handler("work")
    fake.error = "no tmux server is running"
    assert tools["box_tmux_read"].handler("work").startswith("Could not find tmux target")


def test_send_types_literally_then_enter_then_keys(box, monkeypatch):
    fake, _, tools = box
    slept = []
    monkeypatch.setattr(tmux.time, "sleep", slept.append)
    out = tools["box_tmux_send"].handler("work:1", "echo 'hi; there'", wait=0.5)
    assert out.startswith("Sent \"echo 'hi; there'\" to work:1 on box. Pane now shows:\n$ make test")
    assert fake.calls[1:] == [
        ("send-keys", "-t", "work:1", "-l", "echo 'hi; there'"),
        ("send-keys", "-t", "work:1", "Enter"),
        ("capture-pane", "-p", "-e", "-t", "work:1", "-S", "-29"),
    ]
    assert slept == [0.5]
    fake.calls.clear()
    tools["box_tmux_send"].handler("work", "y", enter=False, keys="C-c Up", wait=100)
    assert fake.calls[1:3] == [
        ("send-keys", "-t", "work", "-l", "y"),
        ("send-keys", "-t", "work", "C-c", "Up"),
    ]
    assert slept[-1] == tmux.MAX_WAIT
    fake.calls.clear()
    assert tools["box_tmux_send"].handler("work", keys="C-c").startswith("Sent C-c to work")
    assert fake.calls[1] == ("send-keys", "-t", "work", "C-c")


def test_send_refuses_bad_input(box):
    fake, _, tools = box
    assert tools["box_tmux_send"].handler("work") == "Nothing to send: give text, keys, or both."
    assert tools["box_tmux_send"].handler("work", keys="C-c ; rm").startswith("keys must be tmux key names")
    assert tools["box_tmux_send"].handler("secrets", "ls").startswith("Refused: tmux session 'secrets'")
    assert all(c[0] == "display-message" for c in fake.calls)


def test_run_uses_argv_and_names_a_dead_server(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="out\n", stderr="")

    monkeypatch.setattr(tmux.subprocess, "run", fake_run)
    assert tmux.run("send-keys", "-t", "w", "-l", "a; b") == "out"
    assert seen["cmd"] == ["tmux", "send-keys", "-t", "w", "-l", "a; b"]

    def dead(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no server running on /tmp/tmux-501/default")

    monkeypatch.setattr(tmux.subprocess, "run", dead)
    with pytest.raises(tmux.TmuxError, match="no tmux server is running"):
        tmux.run("list-panes")


def test_mac_worker_serves_tmux_under_its_own_prefix():
    fake = FakeTmux()
    mac.init(config=MacConfig(["Notes"], {}, "Reminders", tmux_sessions=["work"]), tmux_run=fake)
    assert mac.mac_tmux_list.name == "mac_tmux_list"
    assert "work:0.0*" in mac.mac_tmux_list.handler()
    assert mac.mac_tmux_read.handler("work") == "$ make test\nok\n$"
    mac.init(config=MacConfig(["Notes"], {}, "Reminders"), tmux_run=fake)
    assert mac.mac_tmux_read.handler("work").startswith("Refused: tmux access on mac is off")


def test_host_worker_serves_its_hosts_tools(monkeypatch):
    monkeypatch.setenv("NORNS_API_KEY", "k")
    monkeypatch.setenv("FULLA_HOST", "box")
    monkeypatch.setenv("FULLA_TMUX_SESSIONS", "work")
    config = load_config()
    assert config.host == HostConfig(name="box", tmux_sessions=["work"])
    agent = carrier(config)
    assert agent.name == "fulla-box"
    join = ToolWorker("http://unused")._join_payload(agent, agent.name)
    assert join["capabilities"] == ["tools"] and "agents" not in join
    assert [t["name"] for t in join["tools"]] == tmux.tool_names("box") + shell.tool_names("box")
    monkeypatch.setenv("FULLA_HOST", "mac")
    with pytest.raises(SystemExit):
        carrier(load_config())
    monkeypatch.delenv("FULLA_HOST")
    with pytest.raises(SystemExit):
        carrier(load_config())
    monkeypatch.setenv("FULLA_HOST", "My Box")
    with pytest.raises(ValueError):
        load_config()


def test_manager_allowlists_every_known_machines_tmux_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    config = load_config()
    hosts = HostStore(tmp_path / "hosts.json")
    for name in ("box", "pi"):
        hosts.register(parse_announcement({"name": name, "platform": "linux", "sessions": ["work"]}))
    manager = build_agents(config, DirectiveStore(tmp_path / "d.json"), hosts)[0]
    for name in ("mac_tmux_send", "box_tmux_send", "pi_tmux_list", "box_tmux_read"):
        assert name in manager.allowed_tools
    assert set(host_tool_names(hosts)) == {t.name for t in mac.mac_tools if t.name != "mac_imessage_deliver"} | set(
        tmux.tool_names("box") + shell.tool_names("box") + tmux.tool_names("pi") + shell.tool_names("pi"))
    assert "`box_` is box, a linux machine" in manager.system_prompt and "`pi_`" in manager.system_prompt
    assert "# Machines" in manager.system_prompt
    # No registry: the Mac alone.
    bare = build_agents(config, DirectiveStore(tmp_path / "d.json"))[0]
    assert "mac_tmux_read" in bare.allowed_tools and "box_tmux_read" not in bare.allowed_tools
    assert "not announced yet" in bare.system_prompt


def test_turn_note_reports_each_host():
    note = turn_note(["now", "mac_notify", "box_tmux_read", "box_tmux_send"], ["box", "pi"])
    assert "The `mac_` tools in your tool list right now: mac_notify. The Mac is connected" in note
    assert "The `box_` tools in your tool list right now: box_tmux_read, box_tmux_send. The machine box is connected" in note
    assert "No `pi_` tool is in your tool list right now: the machine pi is off" in note
    assert turn_note(["now"]) == turn_note(["now"], [])
    assert "pi" not in turn_note(["now"])


def test_capture_marks_dimmed_text_and_drops_other_escapes(box):
    fake, _, tools = box
    fake.screen = (
        "\x1b[1m\x1b[38;5;174m❯\x1b[0m \x1b[2mTry \"fix lint errors\"\x1b[0m   \n"
        "\x1b[38;2;2;2;2mnot faint\x1b[39m \x1b[0;2mhint\x1b[22m done\n"
        "\x1b]8;;http://x\x07link\x1b]8;;\x07 \x1b[2mtrails off\n"
        "❯ typed"
    )
    assert tools["box_tmux_read"].handler("work") == (
        "❯ «Try \"fix lint errors\"»\n"
        "not faint «hint» done\n"
        "link «trails off»\n"
        "❯ typed"
    )


def test_plain_handles_sgr_forms():
    assert tmux.plain("a\x1b[2m b \x1b[mc") == "a «b» c"
    assert tmux.plain("\x1b[2;1mx\x1b[1;22my") == "«x»y"
    assert tmux.plain("\x1b[48;5;2mx\x1b[2m\x1b[0m") == "x"
    assert tmux.plain("\x1b[?25l\x1b[2Kplain\x1b(B") == "plain"


def test_new_creates_a_detached_allowed_session(box, tmp_path):
    fake, allow, tools = box
    new = tools["box_tmux_new"].handler
    assert new("work") == "Created tmux session 'work' on box, detached, running its shell."
    assert fake.calls[-1] == ("new-session", "-d", "-s", "work")
    new("work", str(tmp_path))
    assert fake.calls[-1] == ("new-session", "-d", "-s", "work", "-c", str(tmp_path))
    fake.calls.clear()
    assert new("work", str(tmp_path / "nope")).startswith("No directory")
    assert new("scratch") == "Refused: tmux session 'scratch' is not one Fulla may use on box. Allowed: work."
    for name in ("", "a:b", "a.b", " work", "=work"):
        assert new(name).startswith("Session names are plain words")
    assert fake.calls == []
    allow[:] = ["*"]
    new("scratch; rm -rf ~")
    assert fake.calls[-1] == ("new-session", "-d", "-s", "scratch; rm -rf ~")
    allow[:] = []
    assert new("work").startswith("Refused: tmux access on box is off")
    allow[:] = ["work"]
    fake.error = "duplicate session: work"
    assert new("work") == "Could not create tmux session 'work' on box: duplicate session: work"


def test_rename_needs_both_names_allowed(box):
    fake, allow, tools = box
    rename = tools["box_tmux_rename"].handler
    allow[:] = ["work", "lab"]
    assert rename("work:1.0", "lab") == "Renamed tmux session 'work' to 'lab' on box."
    assert fake.calls[-1] == ("rename-session", "-t", "=work", "lab")
    fake.calls.clear()
    assert rename("secrets", "lab").startswith("Refused: tmux session 'secrets'")
    assert rename("work", "other").startswith("Refused: tmux session 'other'")
    assert rename("work", "a.b").startswith("Session names are plain words")
    assert all(c[0] == "display-message" for c in fake.calls)
