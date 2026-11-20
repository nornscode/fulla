# Evals

`fulla-eval` is the manager's regression set. Each case in `evals/cases/*.yaml` is a short scripted conversation sent straight to the model with the real manager prompt and tool schemas, built the way the worker builds them, and checked on what it says and which tool it calls. Nothing is executed: a tool call gets a stubbed result back. No Norns, no messenger. It needs `ANTHROPIC_API_KEY` and skips cleanly without one, so CI does not run it.

```bash
uv run --env-file .env fulla-eval                 # every case, against fulla.model
uv run --env-file .env fulla-eval --repeat 5      # see which cases are flaky
uv run --env-file .env fulla-eval --model claude-haiku-4-5 -k timezone
```

Every fixed bug adds a case. Write the owner's message as it reached it, the tool results that led it astray, and a check that fails on the old behaviour:

```yaml
why: Told "11am PT" it converted the time itself (run 220).
connected: [mac]          # machines whose tools are in the list this turn; default none
now: "2026-10-19T09:30"   # what `now` answers, in the owner's zone
stubs: {recall: "#3 Bank closes at 4pm Pacific"}   # other tools get a plain empty answer
messages:
  - user: remind me at 11am PT tomorrow to call the bank
checks:
  - called: schedule_reminder              # also not_called, first_call, no_tools; args are regexes
    args: {due_at: "^2026-10-20T11:00", tz: "^America/Los_Angeles$"}
  - not_reply: "(?i)eastern"               # reply / not_reply: a regex over everything it said
  - judge: Does it confirm 11am Pacific?  # the model grades it, YES passes; last resort
```

A case with `agent: social` runs `fulla-social` instead, the agent friends and groups talk to; give each `user:` a `from:` name and it is marked as the channel marks it.

Run the new case against the old prompt first and see it fail. Prefer a tool check or a regex; a judge question is slower and noisier.
