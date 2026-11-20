from fulla.soul import MAX_CHARS, Soul, main
from fulla.tools import soul as tool_mod
from fulla.tools.soul import show_soul, write_soul


def test_store_starts_unwritten_and_persists(tmp_path):
    soul = Soul(tmp_path / "soul.md")
    assert not soul.written and soul.text == ""
    soul.write("  I am Fulla. I am dry and short, and I call Robin boss.\n\n")
    assert Soul(tmp_path / "soul.md").text == "I am Fulla. I am dry and short, and I call Robin boss."
    assert soul.written
    assert soul.clear() and not soul.clear()
    assert not soul.written


def test_store_refuses_empty_and_overlong(tmp_path):
    soul = Soul(tmp_path / "soul.md")
    for bad in ("", "   \n", "x" * (MAX_CHARS + 1)):
        try:
            soul.write(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad[:10]!r}")
    assert not soul.written


def test_render_asks_when_unwritten_and_holds_when_written(tmp_path):
    soul = Soul(tmp_path / "soul.md")
    unwritten = soul.render("Robin")
    assert "You are brief." in unwritten
    assert "Your soul is unwritten" in unwritten and "`write_soul`" in unwritten
    assert "A scheduled message never asks this" in unwritten

    soul.write("I am Fulla. I am warm and I never hurry Robin.")
    written = soul.render("Robin")
    assert written.startswith("I am Fulla. I am warm and I never hurry Robin.\n")
    assert "Your soul is unwritten" not in written and "You are brief." not in written
    assert "Hold to it in every reply, scheduled ones included." in written


def test_tools_write_show_and_push(tmp_path):
    soul = Soul(tmp_path / "soul.md")
    pushes = []
    tool_mod.init(soul=soul, push_prompt=lambda: pushes.append(soul.text))

    assert show_soul.handler() == "(unwritten)"
    assert write_soul.handler("I am Fulla. Blunt, brief, on your side.").startswith("Written.")
    assert pushes == ["I am Fulla. Blunt, brief, on your side."]
    assert show_soul.handler() == "I am Fulla. Blunt, brief, on your side."
    # A refusal is reported, not raised, and nothing is pushed.
    assert write_soul.handler("x" * (MAX_CHARS + 1)).startswith("Not written:")
    assert len(pushes) == 1 and soul.text == "I am Fulla. Blunt, brief, on your side."


def test_push_failure_is_reported_not_raised(tmp_path):
    soul = Soul(tmp_path / "soul.md")

    def boom():
        raise RuntimeError("norns down")

    tool_mod.init(soul=soul, push_prompt=boom)
    assert "saved, but the prompt update failed" in write_soul.handler("I am Fulla.")
    assert soul.text == "I am Fulla."


def test_soul_is_rendered_into_the_manager_prompt(tmp_path, monkeypatch):
    from fulla.config import load_config
    from fulla.directives import DirectiveStore
    from fulla.worker import build_agents, manager_prompt

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    config = load_config()
    directives = DirectiveStore(tmp_path / "d.json")
    soul = Soul(tmp_path / "soul.md")

    # Unwritten: the default voice and the instruction to ask, under its heading.
    bare = manager_prompt(config, directives, soul=soul)
    assert "# Who you are\nYou are brief." in bare and "Your soul is unwritten" in bare
    assert manager_prompt(config, directives) == bare  # no soul given is the same as unwritten

    soul.write("I am Fulla. I call Robin boss and I keep it to one line.")
    prompt = manager_prompt(config, directives, soul=soul)
    assert "# Who you are\nI am Fulla. I call Robin boss" in prompt
    assert "Your soul is unwritten" not in prompt
    # The nightly review knows to revise it, and the directive section defers to it.
    from fulla.worker import scheduled_playbook
    review = scheduled_playbook("[scheduled] nightly review", "Robin")
    assert "`show_soul`" in review and "(learned <date>)" in review
    assert "changes with `write_soul` instead" in prompt
    manager = build_agents(config, directives, soul=soul)[0]
    assert manager.system_prompt == prompt
    assert "write_soul" in manager.allowed_tools and "show_soul" in manager.allowed_tools


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FULLA_DATA_DIR", str(tmp_path))
    assert main(["show"]) == 0 and capsys.readouterr().out.strip() == "(unwritten)"
    page = tmp_path / "page.md"
    page.write_text("I am Fulla. Short and kind.\n")
    assert main(["set", str(page)]) == 0
    assert main(["show"]) == 0
    assert "I am Fulla. Short and kind." in capsys.readouterr().out
    page.write_text("x" * (MAX_CHARS + 1))
    assert main(["set", str(page)]) == 1
    assert "at most" in capsys.readouterr().err
    assert main(["clear"]) == 0 and main(["clear"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Cleared.", "Nothing to clear."]


def test_manager_prompt_says_attachments_are_unseen(tmp_path, monkeypatch):
    from fulla.config import load_config
    from fulla.directives import DirectiveStore
    from fulla.worker import manager_prompt

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    prompt = manager_prompt(load_config(), DirectiveStore(tmp_path / "d.json"))
    assert "You cannot see photos" in prompt and "never offer to look at one" in prompt
