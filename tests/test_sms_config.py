import pytest

from fulla.config import load_config


def test_sms_is_an_owner_address_once_all_four_twilio_values_are_set(monkeypatch):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("FULLA_SMS_NUMBER", "+16045550199")
    cfg = load_config("config.yaml")
    assert "sms" not in cfg.owners  # no owner number yet

    monkeypatch.setenv("FULLA_SMS_OWNER", "+16475550100")
    cfg = load_config("config.yaml")
    assert cfg.owners == {"telegram": 7, "sms": "+16475550100"}
    assert cfg.owner_ids["sms"] == {"+16475550100"}
    assert cfg.conversation_key == "telegram:7"

    monkeypatch.setenv("FULLA_CHANNEL", "sms")
    cfg = load_config("config.yaml")
    assert cfg.primary_address == "sms:+16475550100" and cfg.conversation_key == "sms:+16475550100"

    monkeypatch.setenv("FULLA_CHANNEL", "pigeon")
    with pytest.raises(ValueError):
        load_config("config.yaml")
