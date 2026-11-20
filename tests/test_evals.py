"""The eval harness itself, with a fake model. The evals need a key and are
not run here; `fulla-eval` runs them."""

from pathlib import Path

import pytest

from fulla import evals


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("FULLA_TIMEZONE", "America/Toronto")
    monkeypatch.setenv("FULLA_OWNER_NAME", evals.OWNER)
    return evals.load_config()


def fake(*replies):
    """A model that answers each call with the next reply, and records the tasks."""
    queue = list(replies)
    seen = []

    def call(task):
        seen.append(task)
        r = queue.pop(0)
        if isinstance(r, str):
            return {"status": "ok", "content": r, "tool_calls": []}
        return {"status": "ok", "content": r.get("content", ""), "tool_calls": [
            {"id": f"t{len(seen)}_{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(r["calls"])]}

    call.seen = seen
    return call


def case(checks, messages=None, **kw):
    return evals.parse_case({"messages": messages or [{"user": "hi"}], "checks": checks, **kw}, name="t")


def test_every_case_file_loads():
    cases = evals.load_cases()
    assert {"photo", "photo-offer", "timezone-pt", "tmux-placeholder", "web-search"} <= {c.name for c in cases}


def test_shorthand_becomes_neutral_messages():
    c = case([{"no_tools": True}], messages=[
        {"user": "what's on screen"},
        {"assistant": "", "calls": [{"name": "mac_tmux_read", "args": {"target": "work"}}]},
        {"tool": "$ make"},
    ])
    assert c.messages == [
        {"role": "user", "content": "what's on screen"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "name": "mac_tmux_read", "arguments": {"target": "work"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "$ make"},
    ]


@pytest.mark.parametrize("data, error", [
    ({"messages": [{"user": "hi"}], "checks": []}, "at least one check"),
    ({"messages": [{"user": "hi"}], "checks": [{"reply": "(", }]}, "bad regex"),
    ({"messages": [{"user": "hi"}], "checks": [{"reply": "a", "not_reply": "b"}]}, "exactly one"),
    ({"messages": [{"tool": "x"}], "checks": [{"no_tools": True}]}, "no call to answer"),
    ({"messages": [{"user": "hi"}, {"assistant": "yo"}], "checks": [{"no_tools": True}]}, "must end"),
])
def test_bad_cases_are_refused(data, error):
    with pytest.raises(evals.CaseError, match=error):
        evals.parse_case(data, name="t")


def test_bad_case_file_names_the_file(tmp_path: Path):
    (tmp_path / "broken.yaml").write_text("messages: [{user: hi}]\nchecks: []\n")
    with pytest.raises(evals.CaseError, match="broken.yaml"):
        evals.load_cases(tmp_path)


def test_task_is_the_manager_turn(config):
    model = fake("ok")
    c = case([{"no_tools": True}])
    evals.converse(c, model, config, "claude-x")
    task = model.seen[0]
    names = [t["name"] for t in task["tools"]]
    assert task["model"] == "claude-x"
    assert "schedule_reminder" in names and "launch_agent" in names
    assert not any(n.startswith("mac_") for n in names)
    assert "No `mac_` tool is in your tool list right now" in task["system_prompt"]
    assert "I am Fulla, a personal manager" in task["system_prompt"]
    assert task["messages"] == [{"role": "user", "content": "hi"}]


def test_connected_machines_bring_their_tools(config):
    model = fake("ok")
    c = case([{"no_tools": True}], connected=["mac", "box"], known={"mac": ["work"]})
    evals.converse(c, model, config, "m")
    task = model.seen[0]
    names = [t["name"] for t in task["tools"]]
    assert "mac_tmux_read" in names and "box_tmux_send" in names and "mac_things_add" in names
    assert "The Mac is connected on this turn" in task["system_prompt"]
    assert "The machine box is connected" in task["system_prompt"]
    assert "tmux sessions allowed: work" in task["system_prompt"]


def test_tool_calls_are_answered_from_stubs_until_a_reply(config):
    model = fake({"calls": [("now", {"tz": "America/Los_Angeles"})]},
                 {"calls": [("calendar", {})], "content": "Checking."},
                 "Done.")
    c = case([{"no_tools": True}], stubs={"calendar": "Dentist 3pm"})
    t = evals.converse(c, model, config, "m")
    assert [k["name"] for k in t.calls] == ["now", "calendar"]
    assert t.reply == "Checking.\nDone."
    third = model.seen[2]["messages"]
    assert third[2]["content"].startswith("2026-10-19T09:30:00-04:00 (Monday, America/Toronto); "
                                          "in America/Los_Angeles: 2026-10-19T06:30:00-07:00")
    assert third[4] == {"role": "tool", "tool_call_id": "t2_0", "content": "Dentist 3pm"}


def test_steps_are_capped(config):
    model = fake(*[{"calls": [("recall", {})]}] * 3)
    t = evals.converse(case([{"no_tools": True}], max_steps=3), model, config, "m")
    assert len(t.calls) == 3 and len(model.seen) == 3


def test_model_error_fails_the_case(config):
    c = case([{"no_tools": True}])
    t = evals.converse(c, lambda task: {"status": "error", "error": "rate limited"}, config, "m")
    assert evals.check(c, t) == ["model error: rate limited"]


def transcript(calls=(), reply=""):
    return evals.Transcript(calls=[{"name": n, "arguments": a} for n, a in calls], texts=[reply])


def test_called_matches_args_by_regex():
    c = case([{"called": "schedule_reminder", "args": {"due_at": "T11:00", "tz": "^America/Los_Angeles$"}}])
    good = transcript([("now", {}), ("schedule_reminder", {"due_at": "2026-10-20T11:00", "tz": "America/Los_Angeles"})])
    assert evals.check(c, good) == []
    converted = transcript([("schedule_reminder", {"due_at": "2026-10-20T14:00"})])
    assert evals.check(c, converted) == [
        "expected a call to schedule_reminder with {'due_at': 'T11:00', 'tz': '^America/Los_Angeles$'}"]


def test_first_and_not_called():
    c = case([{"first_call": "launch_agent", "args": {"agent_name": "^fulla-reader$"}},
              {"not_called": "mac_tmux_send"}])
    assert evals.check(c, transcript([("launch_agent", {"agent_name": "fulla-reader", "message": "q"})])) == []
    failures = evals.check(c, transcript([("now", {}), ("launch_agent", {"agent_name": "fulla-reader"}),
                                          ("mac_tmux_send", {"text": "ls"})]))
    assert failures == ["first call should be launch_agent with {'agent_name': '^fulla-reader$'}, was now",
                        "called mac_tmux_send, which it must not"]


def test_non_string_args_match_as_json():
    c = case([{"called": "mac_tmux_read", "args": {"lines": "^50$"}}])
    assert evals.check(c, transcript([("mac_tmux_read", {"lines": 50})])) == []


def test_reply_checks():
    c = case([{"reply": "(?i)can'?t see"}, {"not_reply": "(?i)^yes"}, {"no_tools": True}])
    assert evals.check(c, transcript(reply="I can't see photos.")) == []
    failures = evals.check(c, transcript([("note_feedback", {})], reply="Yes, send it."))
    assert failures == ["reply should match /(?i)can'?t see/",
                        "reply must not match /(?i)^yes/ (matched 'Yes')", "expected no tool calls"]


def test_judge_asks_the_model_and_reads_yes_or_no():
    c = case([{"judge": "Does it avoid claiming to see the photo?"}])
    yes, no = fake("YES\nIt says it can't see it."), fake("NO\nIt describes the photo.")
    assert evals.check(c, transcript(reply="I can't see it."), yes, "m") == []
    assert evals.check(c, transcript(reply="Nice cookies."), no, "m") == [
        "judge said no to 'Does it avoid claiming to see the photo?': NO\nIt describes the photo."]
    asked = yes.seen[0]
    assert asked["tools"] == [] and "said: I can't see it." in asked["messages"][0]["content"]
    assert "Does it avoid claiming to see the photo?" in asked["messages"][0]["content"]


def test_repeat_counts_passes():
    c = case([{"reply": "ok"}])
    model = fake("ok", "nope", "ok")
    o = evals.run_case(c, model, evals.load_config(), "m", repeat=3)
    assert o.passed == 2 and len(o.runs) == 3


def test_skips_without_a_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("FULLA_MODEL", "claude-sonnet-5")
    assert evals.main([]) == 0
    assert "ANTHROPIC_API_KEY is not set; skipping" in capsys.readouterr().out


def test_missing_key_follows_the_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert evals.missing_key("claude-opus-5") is None
    assert evals.missing_key("openai/gpt-5") == "OPENAI_API_KEY"
    assert evals.missing_key("ollama/llama3") is None


def test_judge_is_its_own_model():
    c = case([{"judge": "Is it fine?"}])
    model, judge_model = fake("hello"), fake("YES fine")
    o = evals.run_case(c, model, evals.load_config(), "under-test", judge_model=judge_model,
                       judge_name="grader")
    assert o.passed == 1
    assert [t["model"] for t in model.seen] == ["under-test"]
    assert [t["model"] for t in judge_model.seen] == ["grader"]


def test_social_case_runs_the_friends_agent(config):
    model = fake("ok")
    c = case([{"no_tools": True}], messages=[{"user": "remind Robin", "from": "Sam"}], agent="social")
    evals.converse(c, model, config, "m")
    task = model.seen[0]
    names = [t["name"] for t in task["tools"]]
    assert "now" in names and "launch_agent" in names
    assert not {"schedule_reminder", "calendar", "recall", "remember"} & set(names)
    assert "side role" in task["system_prompt"] and "# This turn" not in task["system_prompt"]
    content = task["messages"][0]["content"]
    assert content.startswith("[Untrusted message from Sam, who is not the owner.") and "Sam: remind Robin" in content
    owner = case([{"no_tools": True}], messages=[{"user": "hi", "from": "Robin"}], agent="social")
    assert owner.messages[0]["content"] == "Robin (the owner): hi"


def test_social_tools_match_production(config):
    from fulla.tools import social_tools
    from fulla.wake import WITHHELD
    from fulla.worker import SOCIAL_AGENT

    c = case([{"no_tools": True}], messages=[{"user": "hi", "from": "Sam"}], agent="social")
    names = {t["name"] for t in evals.tool_list(c)}
    offered = {t.name for t in social_tools} | {t["name"] for t in evals.BUILTINS}
    assert names == offered - WITHHELD[SOCIAL_AGENT]
    assert not {"wait", "ask_human", "list_agents"} & names


def test_manager_is_not_offered_wait():
    c = case([{"no_tools": True}])
    assert "wait" not in {t["name"] for t in evals.tool_list(c)}


def test_manager_is_not_offered_ask_human_on_a_scheduled_turn():
    asked = case([{"no_tools": True}], messages=[{"user": "book the dentist"}])
    timed = case([{"no_tools": True}], messages=[{"user": "[scheduled] check-in"}])
    assert "ask_human" in {t["name"] for t in evals.tool_list(asked)}
    assert "ask_human" not in {t["name"] for t in evals.tool_list(timed)}


def test_unknown_agent_is_refused():
    with pytest.raises(evals.CaseError, match="agent"):
        case([{"no_tools": True}], agent="reader")
