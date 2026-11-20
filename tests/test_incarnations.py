"""The incarnation loop builds the agent definitions afresh each time, so a
soul or directive change made during one incarnation is what the next one
joins with, not the prompt as it was at process start."""

import threading

from fulla.wake import Incarnations, ServingNorns, run_incarnations


def test_each_incarnation_joins_with_the_current_definitions(monkeypatch):
    joined = []
    soul = {"text": "I am brief."}

    def fake_run(self, agents, **kwargs):
        joined.append(agents[0]["system_prompt"])

    monkeypatch.setattr(ServingNorns, "run", fake_run)
    incarnations = Incarnations()
    threading.Thread(
        target=run_incarnations,
        kwargs={"incarnations": incarnations, "url": "http://unused", "api_key": "k",
                "agents": lambda: [{"system_prompt": soul["text"]}], "worker_id": "fulla",
                "idle": 60, "max_lifetime": 900},
        daemon=True,
    ).start()

    incarnations.wait_for_end_of_current()
    soul["text"] = "I am warm and I call Robin boss."  # as write_soul would, between wakes
    incarnations.wait_for_end_of_current()
    assert joined == ["I am brief.", "I am warm and I call Robin boss."]
