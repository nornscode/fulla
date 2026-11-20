"""Declaring one agent between wakes goes through the SDK's own upsert,
on the class that has it. Live, the push used the REST client, which has
none; the error was swallowed and the group waited for the next wake."""

from norns import Agent, Norns

from fulla import wake


def test_declare_agent_uses_the_sdk_upsert(monkeypatch):
    declared = []
    monkeypatch.setattr(Norns, "_ensure_agent", lambda self, agent: declared.append((self.url, agent.name)))
    wake.declare_agent("http://norns.test", "k", Agent(name="fulla-group-ab12", system_prompt="hi"))
    assert declared == [("http://norns.test", "fulla-group-ab12")]
