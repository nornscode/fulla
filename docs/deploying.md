# Deploying

One Fly app, `fulla`, and it sleeps. Changes land through pull requests; GitHub Actions lints and tests each one, and a merge to `main` that passes is deployed from the committed tree. The deploy step needs a `FLY_API_TOKEN` repository secret, made once with `fly tokens create deploy -a fulla`, and does nothing until it exists. For a deploy by hand, from a clean checkout of `main`:

```bash
fly deploy
```

Its volume at `/data` holds facts, calendars, the reminder index, and the channel's record of what it has posted.

## Nothing runs while nothing happens

Norns is the only always-on process. Fulla is one HTTP receiver on Fly with `min_machines_running = 0`: a request starts the machine, and a few idle minutes after the last one closes, Fly stops it. Three things send requests.

**Norns wakes the worker.** Fulla is registered as a serverless worker: her tools are declared up front, Norns advertises them while nothing is connected, and when a run calls one it queues the task and POSTs a signed wake to `/norns/wake`. The handler checks the signature, brings the SDK worker up as `fulla`, serves until it has had no task for `WAKE_IDLE` seconds, drains, and only then answers 200. The open request is what keeps the machine alive. A sleeping reminder costs nothing until it's due; `wait` and `ask_human` are handled inside Norns and wake nobody.

**Norns tells the channel.** A run webhook POSTs to `/norns/events` when a run parks on a question, completes, or fails, signed the same way. The channel posts questions with buttons and relays outputs. On a cold start it asks Norns for any question still parked, in case a delivery came while nothing was listening.

**Telegram delivers your messages.** The app sets the bot's webhook to `/telegram` at startup, with a secret token derived from the run webhook's secret, so there is one secret to store and nothing to rotate by hand. Nothing polls.

**Or the Signal machine does.** signal-cli must stay connected, so it runs on its own always-on machine, and a forwarder beside it POSTs each message to `/signal`, signed with a secret derived the same way. Fulla still sleeps; the POST is what wakes her.

**A box running egdir says when Claude needs you.** The box's Claude Code hook (`egdir-notify`) POSTs its events to `/egdir/hook` with `Authorization: Bearer $EGDIR_NOTIFY_SECRET`, the same value the box holds in `~/.config/egdir/env`. A question or permission prompt becomes one line to you ("norns on box is waiting: Claude needs your permission to use Bash") and starts no run; answering is a message to her, and she types it into the window with that machine's tmux tools. A finished turn is not relayed, since the hook fires for every Claude session in tmux on the box, yours included. `fly secrets set -a fulla EGDIR_NOTIFY_SECRET=…` turns the route on.

Register both once, after the first deploy, with the hosted `NORNS_URL` and `NORNS_API_KEY` in the environment:

```bash
uv run fulla-register --wake-url https://fulla.fly.dev/norns/wake --fly-app fulla
uv run fulla-register-channel --url https://fulla.fly.dev --fly-app fulla
```

The first generates the tool declarations from the agent definitions through the SDK's own join payload and stores the wake secret. The second creates the run webhook and stores its secret. Re-run the first after adding a tool; it updates in place.

To test: `GET /api/v1/serverless_workers` shows `fulla` with `awake: false` and `GET /api/v1/tools` still lists its tools. `POST /api/v1/run_webhooks/<id>/test` delivers a sample event and reports what the app answered. Send the bot a message and `fly logs` shows the update arrive, the wake if a tool is needed, the run event, and the drain a minute later.

Locally, `uv run fulla` runs the worker all the time and long-polls Telegram, with run events arriving over HTTP from the local Norns. Use a second bot for local dev: Telegram allows one of webhook or polling per bot, and production holds the webhook on the real one. `uv run fulla` deletes the webhook on whichever bot its token names, so never point it at the production token.
