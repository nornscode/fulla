from norns import Norns

from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.register import declaration
from fulla.worker import build_agents


def test_declaration_matches_sdk_join_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    decl = declaration("fulla", "https://fulla.fly.dev/norns/wake")

    agents = build_agents(load_config(), DirectiveStore(tmp_path / "d.json"))
    join = Norns("http://unused")._join_payload(agents, "fulla")
    assert decl["tools"] == join["tools"]
    assert decl["capabilities"] == join["capabilities"]
    assert decl["worker_id"] == "fulla"

    names = {t["name"] for t in decl["tools"]}
    assert {"now", "calendar", "remember", "schedule_reminder", "set_directive", "note_feedback", "note_gap"} <= names
    for t in decl["tools"]:
        assert set(t) == {"name", "description", "input_schema", "side_effect"}
