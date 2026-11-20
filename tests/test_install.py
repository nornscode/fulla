"""`fulla-mac install` and `fulla-host install`: what they write and what they run."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from fulla import install

REPO = Path(__file__).resolve().parents[1]
VALUES = install.Values(norns_url="https://norns.example", norns_api_key="nrn_secret",
                        fulla_url="https://fulla.example", tmux_sessions="work,scratch")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(install.os, "getuid", lambda: 501)
    return tmp_path


def runner(fail_bootstrap_times: int = 0):
    """A subprocess.run that records every command. The first
    `fail_bootstrap_times` launchctl bootstraps fail with error 5."""
    calls: list[list[str]] = []
    state = {"left": fail_bootstrap_times}

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "bootstrap"] and state["left"] > 0:
            state["left"] -= 1
            return subprocess.CompletedProcess(cmd, 5, stdout=b"", stderr=b"Bootstrap failed: 5: Input/output error")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    run.calls = calls
    return run


def test_parse_env_reads_what_uv_would(tmp_path):
    f = tmp_path / "x.env"
    f.write_text('# a comment\n\nexport A=1\nB="two words"  \nC=three # trailing\nD=\'x\'\nnot a pair\n')
    assert install.parse_env(f) == {"A": "1", "B": "two words", "C": "three", "D": "x"}


def test_env_file_holds_the_values_and_names_the_rest():
    mac = install.render_env("mac", VALUES)
    assert "NORNS_URL=https://norns.example\n" in mac and "NORNS_API_KEY=nrn_secret\n" in mac
    assert "FULLA_URL=https://fulla.example\n" in mac and "FULLA_TMUX_SESSIONS=work,scratch\n" in mac
    assert "FULLA_HOST" not in mac
    assert "# FULLA_IMESSAGE_ALLOW=" in mac and "# FULLA_MAC_HOME_FOLDER=" in mac
    host = install.render_env("host", install.Values("https://n", "k", "https://f", "", host="box"))
    assert "FULLA_HOST=box\n" in host and "FULLA_TMUX_SESSIONS=\n" in host and "FULLA_SHELL=\n" in host
    assert "FULLA_IMESSAGE_ALLOW" not in host
    assert all(line.split("=", 1)[0].isupper() for line in host.splitlines() if line and not line.startswith("#"))


def test_everything_else_in_the_old_env_file_is_carried_over(home, tmp_path):
    # The checkout's .env.mac is the default on a first run: the allow list and the
    # app list are not asked for, and must not quietly become the defaults.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env.mac").write_text(
        "NORNS_URL=https://n\nNORNS_API_KEY=k\nFULLA_URL=https://f\nFULLA_TMUX_SESSIONS=a,b\n"
        "FULLA_MAC_APPS=Reminders,Notes,Calendar,Messages\nFULLA_IMESSAGE_ALLOW=Alex=+16045550100,Robin=me@icloud.com\n"
        'FULLA_MAC_HOME_FOLDER="Fulla Home"\nNORNS_WORKER_ID=fulla-mac-2 # a comment\n')
    args = install.parser("mac").parse_args([])
    v = install.gather("mac", args, repo=repo, interactive=False)
    assert v.tmux_sessions == "a,b"
    assert v.extras == {"FULLA_MAC_APPS": "Reminders,Notes,Calendar,Messages",
                        "FULLA_IMESSAGE_ALLOW": "Alex=+16045550100,Robin=me@icloud.com",
                        "FULLA_MAC_HOME_FOLDER": "Fulla Home", "NORNS_WORKER_ID": "fulla-mac-2"}
    text = install.render_env("mac", v)
    assert "\nFULLA_IMESSAGE_ALLOW=Alex=+16045550100,Robin=me@icloud.com\n" in text
    assert "\nFULLA_MAC_APPS=Reminders,Notes,Calendar,Messages\n" in text
    assert '\nFULLA_MAC_HOME_FOLDER="Fulla Home"\n' in text  # a space is quoted, so it reads back whole
    assert "# FULLA_REMINDERS_LIST=Reminders" in text  # unset stays a documented default
    assert "carried over" in text and "\nNORNS_WORKER_ID=fulla-mac-2\n" in text
    out = tmp_path / "round.env"
    out.write_text(text)
    assert {k: val for k, val in install.parse_env(out).items() if k not in install.ASKED} == v.extras


def test_mac_install_builds_the_python_writes_both_plists_and_loads_them(home):
    run = runner()
    said = []
    env_file = install.install_mac(VALUES, repo=REPO, run=run, out=said.append, sleep=lambda s: None)

    assert env_file == home / ".config" / "fulla" / "mac.env"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "NORNS_API_KEY=nrn_secret" in env_file.read_text()

    worker = (home / "Library" / "LaunchAgents" / "com.fulla.mac.plist").read_text()
    assert f"<string>{home}/Library/Application Support/Fulla/venv/bin/python</string>" in worker
    assert f"<string>{env_file}</string>" in worker
    assert f"<string>{REPO}</string>" in worker
    assert "ENV_FILE" not in worker and "REPO" not in worker and "FULLA_HOME" not in worker
    update = (home / "Library" / "LaunchAgents" / "com.fulla.mac.update.plist").read_text()
    assert f"<string>{REPO}/launchd/mac-update.sh</string>" in update

    assert run.calls[0] == ["sh", str(REPO / "launchd" / "mac-python.sh")]
    assert run.calls[1:] == [
        ["launchctl", "bootout", "gui/501/com.fulla.mac"],
        ["launchctl", "bootstrap", "gui/501", str(home / "Library/LaunchAgents/com.fulla.mac.plist")],
        ["launchctl", "bootout", "gui/501/com.fulla.mac.update"],
        ["launchctl", "bootstrap", "gui/501", str(home / "Library/LaunchAgents/com.fulla.mac.update.plist")],
    ]
    # The grant is the owner's; the last words name the one binary and the restart.
    text = "\n".join(said)
    assert f"{home}/Library/Application Support/Fulla/python/bin/python3.13" in text
    assert "Full Disk Access and Accessibility" in text and "kickstart -k gui/501/com.fulla.mac" in text


def test_mac_install_retries_bootstrap_on_the_heels_of_bootout(home):
    run = runner(fail_bootstrap_times=2)
    naps = []
    install.install_mac(VALUES, repo=REPO, run=run, out=lambda s: None, sleep=naps.append)
    bootstraps = [c for c in run.calls if c[:2] == ["launchctl", "bootstrap"]]
    assert len(bootstraps) == 4 and naps == [2, 2]  # two retries for the first label, none for the second

    run = runner(fail_bootstrap_times=5)
    with pytest.raises(SystemExit, match="Input/output error"):
        install.install_mac(VALUES, repo=REPO, run=run, out=lambda s: None, sleep=lambda s: None)


def test_host_install_writes_the_units_and_enables_them(home):
    run = runner()
    said = []
    v = install.Values("https://norns.example", "k", "https://fulla.example", "", host="box")
    env_file = install.install_host(v, repo=REPO, run=run, out=said.append, uv="/usr/local/bin/uv")

    assert env_file == home / ".config" / "fulla" / "host.env"
    assert "FULLA_HOST=box\n" in env_file.read_text()
    units = home / ".config" / "systemd" / "user"
    service = (units / "fulla-host.service").read_text()
    assert f"ExecStart=/usr/local/bin/uv run --env-file {env_file} fulla-host" in service
    assert f"WorkingDirectory={REPO}" in service
    assert "UV" not in service.replace("fulla-host", "") and "ENV_FILE" not in service
    assert f"ExecStart=/bin/sh {REPO}/systemd/host-update.sh" in (units / "fulla-host-update.service").read_text()
    assert "OnUnitActiveSec=1h" in (units / "fulla-host-update.timer").read_text()
    assert run.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "fulla-host"],
        ["systemctl", "--user", "restart", "fulla-host"],
        ["systemctl", "--user", "enable", "--now", "fulla-host-update.timer"],
    ]
    assert any("loginctl enable-linger" in s for s in said)


def test_flags_need_no_terminal_and_earlier_values_are_the_defaults(home, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted"))
    monkeypatch.setattr(install.getpass, "getpass", lambda *a: pytest.fail("prompted"))
    args = install.parser("host").parse_args(["--norns-url", "https://n", "--norns-api-key", "k",
                                              "--fulla-url", "https://f", "--host", "box", "--tmux-sessions", ""])
    v = install.gather("host", args, repo=REPO, interactive=False)
    assert v == install.Values("https://n", "k", "https://f", "", host="box", machine_id=v.machine_id)
    assert len(v.machine_id) == 16  # made on the first run, kept after

    # A second run: what the first wrote is the default, so only the key can be left out of the flags.
    install.install_host(v, repo=REPO, run=runner(), out=lambda s: None, uv="uv")
    args = install.parser("host").parse_args(["--tmux-sessions", "work"])
    v2 = install.gather("host", args, repo=REPO, interactive=False)
    assert v2 == install.Values("https://n", "k", "https://f", "work", host="box", machine_id=v.machine_id)

    # Interactive: an empty answer keeps the default (what the install wrote); the key is asked for hidden.
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "")
    monkeypatch.setattr(install.getpass, "getpass", lambda prompt: asked.append(prompt) or "")
    v3 = install.gather("host", install.parser("host").parse_args([]), repo=REPO, interactive=True)
    assert v3 == v
    assert [a.split(" [")[0].rstrip(": ") for a in asked] == [
        "tmux sessions she may reach (comma-separated, empty for none)",
        "give her a shell and the files on this machine, as this user? (y/n)", "Norns URL", "Norns API key",
        "Fulla's URL", "this machine's name (lowercase letters and digits)"]
    assert "Norns API key [keep the current one]: " in asked  # the key itself is never shown


def test_bad_values_are_refused(home):
    def ns(**kw):
        base = {"norns_url": "https://n", "norns_api_key": "k", "fulla_url": "https://f",
                "tmux_sessions": "", "shell": None, "host": "box"}
        base.update(kw)
        return argparse.Namespace(**base)

    with pytest.raises(SystemExit, match="not a host name"):
        install.gather("host", ns(host="My Box"), repo=REPO, interactive=False)
    with pytest.raises(SystemExit, match="Mac worker's name"):
        install.gather("host", ns(host="mac"), repo=REPO, interactive=False)
    with pytest.raises(SystemExit, match="http"):
        install.gather("host", ns(norns_url="norns.example"), repo=REPO, interactive=False)
    with pytest.raises(SystemExit, match="no terminal"):
        install.gather("mac", ns(norns_api_key=None), repo=REPO, interactive=False)


def test_the_entrypoints_dispatch_install(monkeypatch):
    seen = []
    monkeypatch.setattr(install, "main", lambda kind, argv: seen.append((kind, argv)) or 0)
    from fulla import host
    from fulla.mac import worker

    monkeypatch.setattr(host.sys, "argv", ["fulla-host", "install", "--host", "box"])
    with pytest.raises(SystemExit) as e:
        host.main()
    assert e.value.code == 0
    monkeypatch.setattr(worker.sys, "argv", ["fulla-mac", "install"])
    with pytest.raises(SystemExit):
        worker.main()
    assert seen == [("host", ["--host", "box"]), ("mac", [])]
