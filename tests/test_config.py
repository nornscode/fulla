import pytest

from fulla.config import _interpolate


def test_env_var(monkeypatch):
    monkeypatch.setenv("X_TOKEN", "abc")
    assert _interpolate("${X_TOKEN}") == "abc"


def test_default_used_when_unset(monkeypatch):
    monkeypatch.delenv("X_URL", raising=False)
    assert _interpolate("${X_URL:-http://localhost:4001}") == "http://localhost:4001"


def test_default_used_when_empty(monkeypatch):
    monkeypatch.setenv("X_URL", "")
    assert _interpolate("${X_URL:-fallback}") == "fallback"


def test_env_beats_default(monkeypatch):
    monkeypatch.setenv("X_URL", "http://real")
    assert _interpolate("${X_URL:-fallback}") == "http://real"


def test_missing_without_default_raises(monkeypatch):
    monkeypatch.delenv("X_MISSING", raising=False)
    with pytest.raises(ValueError):
        _interpolate("${X_MISSING}")


def test_load_config_parses_calendars(monkeypatch, tmp_path):
    from fulla.config import load_config

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    monkeypatch.setenv("FULLA_CALENDARS", "Personal=https://a/b.ics")
    cfg = load_config("config.yaml")
    assert cfg.fulla.calendars == {"Personal": "https://a/b.ics"}

    monkeypatch.delenv("FULLA_CALENDARS")
    assert load_config("config.yaml").fulla.calendars == {}


def test_every_configured_messenger_is_an_owner_address_and_the_primary_names_the_conversation(monkeypatch):
    import pytest

    from fulla.config import load_config

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    cfg = load_config("config.yaml")
    assert cfg.owners == {"telegram": 7}
    assert cfg.channel.primary == "telegram" and cfg.conversation_key == "telegram:7"
    assert cfg.channel.group_context == 8

    monkeypatch.setenv("FULLA_SIGNAL_NUMBER", "+16045550199")
    monkeypatch.setenv("FULLA_SIGNAL_OWNER", "u-1,+16045550100")
    cfg = load_config("config.yaml")
    assert cfg.owners == {"telegram": 7, "signal": "+16045550100"}
    assert cfg.conversation_key == "telegram:7"  # the primary did not change, so neither did the key

    monkeypatch.setenv("FULLA_CHANNEL", "signal")
    cfg = load_config("config.yaml")
    assert cfg.primary_address == "signal:+16045550100" and cfg.conversation_key == "signal:+16045550100"

    monkeypatch.delenv("FULLA_SIGNAL_NUMBER")
    with pytest.raises(ValueError):
        _ = load_config("config.yaml").primary_address  # primary is signal, but signal is not configured


def test_repo_and_commit(monkeypatch):
    from fulla.config import load_config
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    monkeypatch.delenv("FULLA_REPO", raising=False)
    monkeypatch.setenv("FULLA_COMMIT", "")
    cfg = load_config()
    assert cfg.fulla.repo == "nornscode/fulla" and cfg.fulla.commit == ""
    monkeypatch.setenv("FULLA_REPO", " other/her ")
    monkeypatch.setenv("FULLA_COMMIT", "abc1234def")
    cfg = load_config()
    assert cfg.fulla.repo == "other/her" and cfg.fulla.commit == "abc1234def"
