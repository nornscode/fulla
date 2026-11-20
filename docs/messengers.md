# Messengers

Fulla listens on every messenger configured and answers on the one you used.

## Telegram

Make a bot with @BotFather and get your numeric id from @userinfobot; only that id is honoured. Set `FULLA_TELEGRAM_BOT_TOKEN` and `FULLA_TELEGRAM_USER_ID`.

Fulla never knows which channel it's on. The channel module watches Norns for runs parked on a question and posts them with buttons; a tap or a reply to that message answers the run. Anything else you type goes to the manager; while Fulla works on it the chat shows "Working…" with a Cancel button and the typing indicator, both gone when its reply lands. Scheduled briefs and finished reminders are announced the same way. Swapping Telegram for something else is a change to one file.

Reactions work both ways. On a reminder, 👍 or 👌 is done, 😴 snoozes an hour and 👎 drops it; on any other question 👍 is yes and 👎 is no. A reaction on anything else Fulla said reaches it as a short reply quoting the message, which it usually lets pass. When a single emoji is all its answer needs, it shows as its reaction on your message. Telegram only sends reactions to a webhook that asked for them, so the first deploy with this re-sets the webhook at startup, as every boot does.

## Signal

Fulla can be a Signal contact instead of a Telegram bot: its own number, on a second Fly app, `fulla-signal`, that runs signal-cli all day (the Fulla app sleeps, and signal-cli must stay connected). You need a number that can take one SMS or voice call, kept paid for; most VoIP numbers are refused, a prepaid SIM is not.

```bash
cd deploy/signal
fly apps create fulla-signal && fly volumes create fulla_signal -a fulla-signal -r yyz -s 1
fly deploy -c fly.toml --ha=false
fly proxy 8080:8080 -a fulla-signal           # in another terminal, for the next two
./register.sh +1FULLA signalcaptcha://…       # captcha from signalcaptchas.org/registration/generate
./register.sh +1FULLA verify 123456           # the code; add --voice to the first call if no SMS comes
curl -X POST -H 'Content-Type: application/json' localhost:8080/v1/accounts/+1FULLA/pin -d '{"pin":"…"}'
curl -X PUT -H 'Content-Type: application/json' localhost:8080/v1/profiles/+1FULLA -d '{"name":"Fulla"}'
fly secrets set -a fulla-signal SIGNAL_NUMBER=+1FULLA FULLA_SIGNAL_SECRET=$(uv run fulla-signal-secret)
fly secrets set -a fulla FULLA_CHANNEL=signal FULLA_SIGNAL_NUMBER=+1FULLA FULLA_SIGNAL_OWNER=+1YOU
```

`fulla-signal-secret` needs `NORNS_WEBHOOK_SECRET` in the environment; it derives the forwarder's secret from it, so the Signal machine never holds the webhook's own. Set a registration lock PIN before anything else: it is what stops someone who later gets the number from taking the account. Snapshot the volume once registered; losing it means a new registration and a new safety number.

Both messengers can stay configured. Fulla answers each message on the app it came from, and `FULLA_CHANNEL` names the primary: the one that gets the morning brief, reminders and anything else nobody asked for from a particular app. Changing the primary starts a new standing conversation (`signal:<your number>`), so the chat history starts over; facts, standing instructions, the soul and reminders are in their own stores and carry over. The triggers (see [Timers](using.md#timers)) carry the old key: its listing marks each as not in your conversation, and setting one again ("brief at 8") moves it, or re-create them with `--conversation-key 'signal:<your number>'`; otherwise the morning brief lands where nobody is listening. Questions arrive with a "Reply to this with one of" line in place of buttons; swipe to reply with one of them, or react (👍 is done or yes, 👎 is drop or no, 😴 snoozes an hour). There is no Working… message on Signal, only the typing indicator; while it is working, a message that is just "wait", "cancel", "stop" or "nvm" cancels the run (that works on Telegram too). Messages are never lost: the forwarder keeps each one on the volume until Fulla has taken it, so a slow start, a deploy or a wrong secret only delays delivery. Logs: `fly logs -a fulla-signal`; the forwarder's lines start with `forward`, and `forward-spool/` under the volume is what is still waiting.

Deploys of `deploy/signal/` go through `.github/workflows/signal.yml` on merge, with a `FLY_API_TOKEN_SIGNAL` repository secret from `fly tokens create deploy -a fulla-signal`. The same secret lets `signal-watch.yml` check every half hour that the machine is running, start it if not, and fail so GitHub mails you: the machine has no public service, so nothing else would notice, and Fly's restart policy is set to never give up on it. Bump the pinned image monthly; old signal-cli releases stop working when Signal changes its servers.

## Text messages

Fulla can also be a number you text, through Twilio. If its Signal number is a Twilio number it is the same one: Signal used it once, for its registration code, and a text to it reaches Twilio, not Signal. Check in the Twilio console that the number has SMS on it; a voice-only number will not do.

```bash
fly secrets set -a fulla TWILIO_ACCOUNT_SID=AC… TWILIO_AUTH_TOKEN=… FULLA_SMS_NUMBER=+1FULLA FULLA_SMS_OWNER=+1YOU
```

On start Fulla points the number's webhook, and its fallback, at `https://fulla.fly.dev/sms`; there is nothing to set in the console. Twilio signs each POST with the auth token, and only your number is heard. Everything the other messengers do with a message after sending it is missing here: no buttons, no edits, no typing indicator, no reactions, no quoting. A question comes with a "Reply with one of: Done, Snooze 1h, Snooze 1d, Drop it" line, and a reply that is just one of those labels answers it, the newest question that offers it (the same shortcut works on Signal, where you may not have swiped). A long answer arrives as several texts, split at paragraphs. Markdown is flattened to plain text and the typographic punctuation a model likes (the ellipsis, curly quotes, dashes, bullets) is swapped for plain ASCII, since one such character drops a whole message from 160 characters a segment to 70; emoji are kept. Each segment is billed, so a long answer costs a few cents. Friends and groups are not a thing here: SMS carries your own chat with the manager and nothing else.

Twilio gives the webhook fifteen seconds and does not retry on its own; the fallback is the same route, so a message that arrived while the machine was cold or Norns was unreachable gets one more try. `FULLA_CHANNEL=sms` makes it the primary. Local dev polls Twilio's message list every few seconds, so no tunnel is needed.
