from fulla.directives import DirectiveStore
from fulla.tools import directives as tool_mod
from fulla.tools.directives import drop_directive, list_directives, set_directive


def test_store_persists_and_renders(tmp_path):
    store = DirectiveStore(tmp_path / "d.json")
    assert store.render() == "(none yet)"
    a = store.add("Act like my manager. Give me hard feedback.")
    b = store.add("Never suggest the gym before 9am.")
    assert DirectiveStore(tmp_path / "d.json").render() == f"{a.id}. {a.text}\n{b.id}. {b.text}"
    assert store.remove(a.id) and not store.remove(a.id)
    assert store.render() == f"{b.id}. {b.text}"


def test_tools_push_the_prompt_on_change(tmp_path):
    store = DirectiveStore(tmp_path / "d.json")
    pushes = []
    tool_mod.init(store=store, push_prompt=lambda: pushes.append(store.render()))

    assert set_directive.handler("Give me hard feedback.").startswith("Standing instruction #1 set")
    assert pushes == ["1. Give me hard feedback."]
    assert list_directives.handler() == "1. Give me hard feedback."
    assert drop_directive.handler(1) == "Dropped standing instruction #1."
    assert pushes[-1] == "(none yet)"
    assert drop_directive.handler(1) == "No standing instruction #1."
    assert len(pushes) == 2


def test_push_failure_is_reported_not_raised(tmp_path):
    store = DirectiveStore(tmp_path / "d.json")

    def boom():
        raise RuntimeError("norns down")

    tool_mod.init(store=store, push_prompt=boom)
    out = set_directive.handler("Be brief.")
    assert "saved, but the prompt update failed" in out
    assert store.render() == "1. Be brief."


def test_directives_are_rendered_into_the_manager_prompt(tmp_path, monkeypatch):
    from fulla.config import load_config
    from fulla.worker import build_agents, manager_prompt

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    config = load_config()
    store = DirectiveStore(tmp_path / "d.json")
    store.add("Give me hard feedback.")
    assert "1. Give me hard feedback." in manager_prompt(config, store)
    manager = build_agents(config, store)[0]
    assert manager.system_prompt == manager_prompt(config, store)
    assert "set_directive" in manager.allowed_tools
