# Fulla

A private personal manager. You tell her things, she keeps them. You ask to be reminded, she asks you when it's due and keeps asking until you say it's done. Every morning she tells you what's coming. She talks to you on Telegram and to nobody else.

Built on [Norns](https://github.com/nornscode/norns). Third reference agent after [Mimir](https://github.com/nornscode/norns-mimir-agent) and [Hermod](https://github.com/nornscode/hermod), and the first one that's for a person rather than a team. It leans on the parts of durable execution that a daemon on a laptop can't give you: a commitment that waits for days at no cost, a question that parks until you answer it, and a log that says what happened overnight.

Named for Frigg's attendant, who carried her casket and kept her secrets.

## How it works

```
Telegram ◄──► fulla (one process)
               ├── channel: long-polls Telegram, posts questions with buttons
               └── Norns worker: serves two agents
                        │
                        ▼
                     Norns runtime
                        ├── fulla           the manager, one standing conversation
                        └── fulla-reminder  one run per commitment
```

Two agent definitions, on purpose. The manager is a chat front with a single standing conversation keyed to you. Each commitment is its own run of the reminder agent: it sleeps until due, asks once, and closes when you say so. They have to be separate, because a parked run treats the next message as its answer, so the thing that chats must not be the thing that sleeps.

Fulla's own check-backs ("look at that build again in twenty minutes") work the same way with a fourth agent, `fulla-followup`: `schedule_followup` starts a run that sleeps until due, posts `[scheduled] follow-up: <what>` into the manager's conversation, and finishes. The manager looks, and tells you only if it is worth saying; otherwise `[silent]`. The follow-up run itself is never shown to you. A coding sandbox is watched instead (`watch_environment`): `fulla-watch` polls it on a small model and wakes the manager once, when the job ends.

Only the manager runs on `FULLA_MODEL` (Opus by default). The reader, the Mac queue, friends and groups run on `FULLA_MID_MODEL` (Sonnet), and reminders, follow-ups and the watcher on `FULLA_LIGHT_MODEL` (Haiku).

Fulla never knows which channel she's on. The channel module watches Norns for runs parked on a question and posts them with buttons; a tap or a reply to that message answers the run. Anything else you type goes to the manager; while she works on it the chat shows "Working…" with a Cancel button and the typing indicator, both gone when her reply lands. Scheduled briefs and finished reminders are announced the same way. Swapping Telegram for something else is a change to one file.

Reactions work both ways. On a reminder, 👍 or 👌 is done, 😴 snoozes an hour and 👎 drops it; on any other question 👍 is yes and 👎 is no. A reaction on anything else she said reaches her as a short reply quoting the message, which she usually lets pass. When a single emoji is all her answer needs, it shows as her reaction on your message. Telegram only sends reactions to a webhook that asked for them, so the first deploy with this re-sets the webhook at startup, as every boot does.

## Calendar

Fulla reads calendars from iCal feeds, read-only. In Google Calendar, open the calendar's settings and copy "Secret address in iCal format". Put one or more in `FULLA_CALENDARS` as `Name=url,Other=url`. She uses them for the brief, for scheduling questions, and for check-ins.

## Check-ins

A second trigger, every two hours during the day, lets her be useful without being asked: a free hour before an evening event is a gym window, a friend you mentioned hasn't come up in weeks, you said you wanted to walk more. She suggests one thing or stays silent, and silence is the default. A silent run ends with `[silent]`, which the channel doesn't relay, so the log still says she looked.

## Your Mac

Fulla can have hands on your Mac: Things, Apple Reminders, iMessage, a notification on your screen, your home, your tmux sessions (see below), and AppleScript against a short list of apps. The Mac runs a second, tools-only worker, `fulla-mac`, kept alive by launchd. Norns dispatches a tool call to whichever connected worker holds the tool, so the `mac_*` tools are offered to Fulla exactly while the Mac is up and refused, not queued forever, when it is not: she says the Mac is offline and offers one of her own reminders instead.

What she may do is a list, and it starts short. `FULLA_MAC_APPS` names the apps AppleScript may address (default Things3, Reminders, Notes, Calendar; `do shell script` is refused, and so is an app named by anything but a plain string). "Add a task" or anything naming a project or area goes to Things; "Apple reminder" goes to Reminders; "remind me" stays a Fulla reminder she chases herself. `FULLA_IMESSAGE_ALLOW` names who she may iMessage, as `Name=handle` pairs; it is empty by default, which turns sending off. She never sends one herself: she drafts it, on your ask or her own initiative, and you get a card with the recipient, the exact text, and Send, Edit and Cancel (buttons on Telegram; on Signal, quote the card and reply with one; a reaction can cancel but never sends). Only Send sends it, exactly as shown, from the Mac (or when the Mac is next on). AppleScript never addresses Messages. Reading recent messages needs Full Disk Access for the worker's Python, granted once in System Settings. The same grant lets her read your Contacts, so a thread is named by who wrote it rather than by a number, and `mac_imessage_unanswered` lists who has waited a day or more for your reply; her check-ins look at it. Only people with a name in Contacts or the allow list are listed, so a short code's one-time code or a stranger's text never reaches her that way. A name she knows is not someone she may message: that stays `FULLA_IMESSAGE_ALLOW`. `mac_window` puts a running app's front window full screen, maximized, on half the screen, minimized or in front ("make Notes full screen"). It is one fixed script, not AppleScript against System Events, which stays off the list because it could type into anything; it never launches an app, and it needs Accessibility for the worker's Python.

The worker runs on a Python of its own. macOS grants Full Disk Access, Accessibility and Automation to the binary that asks, and uv's Python is shared by every `uv run` on the machine, so a grant there would reach all of them. `launchd/mac-python.sh` copies a uv-managed CPython into `~/Library/Application Support/Fulla/python`, gives the copy its own code signature, builds the worker's venv beside it, and prints the one binary to grant (`~/Library/Application Support/Fulla/python/bin/python3.13`). launchd runs that venv's Python directly, never through uv.

The home goes through Shortcuts, because the Home app cannot be scripted and Shortcuts can: make a folder in the Shortcuts app named `Fulla Home` (or whatever `FULLA_MAC_HOME_FOLDER` says) and put in it the shortcuts you want her to run, one per scene, light, lock or thermostat setting. "Goodnight", "Lights off", "Thermostat" with a number as its input. She sees that folder and nothing else: `mac_home_list` names what is in it and `mac_home_run` runs one by name, so what she can touch in the house is exactly the folder's contents, and adding a shortcut is the whole change. "Lights off at eleven" becomes a follow-up of her own whose note says to run the shortcut then; a reminder would only tell you.

```bash
cp .env.mac.example .env.mac     # hosted NORNS_URL, NORNS_API_KEY, the two allowlists
uv run --env-file .env.mac fulla-mac    # try it in the foreground first
uv run fulla-mac install         # then install it: asks for the values, builds Fulla's own Python, loads launchd
```

`fulla-mac install` asks for the Norns URL and key, Fulla's URL and the tmux sessions (flags `--norns-url`, `--norns-api-key`, `--fulla-url`, `--tmux-sessions` skip the questions; what an earlier install or `.env.mac` already says is the default), writes them to `~/.config/fulla/mac.env`, runs `launchd/mac-python.sh`, and writes and loads both plists from `launchd/`: the worker and the hourly update. Run it again to change a value or after moving the checkout. Then grant Full Disk Access and Accessibility (System Settings > Privacy & Security, "+", Cmd-Shift-G to paste the path) to the binary it names and nothing else, and `launchctl kickstart -k gui/$(id -u)/com.fulla.mac`. Never grant them to uv or to uv's shared Python under `~/.local/share/uv/python`; if you did before, remove those entries. When a permission is missing the tool names the binary, or, if the worker is somehow on a shared Python, tells you to run `mac-python.sh` instead. The first time she touches each app, macOS asks you to allow Python to control it (Automation); say yes once, for that binary. Logs are in `/tmp/fulla-mac.log`. Nothing to register in Norns: a worker that connects is its own declaration, and the manager's allowlist already names the tools.

When the lid is shut the Mac is off, and the `mac_*` tools are simply not in Fulla's list. Ask for something on the Mac anyway and she queues it: `queue_for_mac` starts a run of a third agent, `fulla-mac-task`, which parks on `ask_human` with the question "Waiting for the Mac." That question is not for you and is never posted; the Mac worker answers it when it next connects, the run wakes, finds the Mac tools in its list, does the task, and tells you. Nothing polls and nothing costs anything while the lid is shut: a reconnect wakes every waiting task exactly once. The task is a run, like a reminder; `list_mac_queue` shows what is waiting and "drop that" cancels one.

The Mac worker runs whatever code is in that checkout, so treat the checkout as the Mac's deploy location: keep it on `main` with no local edits, and do your work in a worktree or a clone. The second launchd job the installer loads pulls `main` every hour, syncs Fulla's venv and restarts the worker when it changed, which is the Mac's half of a merge-to-deploy; `sh launchd/mac-update.sh` does the same by hand, right after a merge.

## Your terminals

Fulla can look at your tmux sessions, type into them, and start or rename them: "what's the test run in my work session doing?", "type `make deploy` into the shell window on the box", "start a session called scratch in ~/proj". On the Mac this rides on the Mac worker; on any other machine, a Linux box say, a third tools-only worker, `fulla-host`, serves the same tools under that machine's name, so `FULLA_HOST=box` serves `box_tmux_list`, `box_tmux_read`, `box_tmux_send`, `box_tmux_new` and `box_tmux_rename`. A new session starts with its shell; there is no start command, since tmux would run it through the shell, and typing into it is how anything runs. Text that is dimmed on screen, like the placeholder in Claude Code's input box, reaches her marked as «…», so she does not take it for something typed.

She remembers every machine she has met. A worker announces itself to the Fly app when it starts (`FULLA_URL` on the machine, signed with the Norns key), and Fulla writes it into `hosts.json`: the name, the platform, which sessions it allows. From that memory come the machines she may call, the list of them in her prompt, and a note on every turn saying which are connected right now, so she routes "the box" to `box_tmux_read` and tells you when the box is off. Nothing to configure on Fly: start the worker once and she knows it.

Typing into a shell runs commands, so this is off until you allow it. On each machine, `FULLA_TMUX_SESSIONS` names the sessions she may read, type into, create and rename to (`*` for all of them); she sees nothing else, and the check is on the session a target resolves to, not on how it was spelled. The prompt tells her to type only what you asked for, as you gave it, never something of her own, and never from a scheduled message. The allowlist is the real guard; keep secrets out of the sessions on it.

Every machine has a name, and the name is the routing: `box_exec` goes to the one worker serving it. So a name belongs to one machine: the installer gives each an id of its own, and a second machine announcing a name already taken is refused: its worker waits and tries again hourly, its log says why, and she tells you once a day that a machine of that name is trying to join. A reinstall with the same hostname is the usual cause; "forget box" lets the new one in. Tell her what a machine is for ("the box is my dev server with the widgets checkout") and she keeps it with the machine, so "run the tests on the dev server" finds it; "what machines do you know" lists them, and "forget the old box" drops one. With more than one connected she names the machine when she says what she ran, and asks when an ask could mean two.

A machine can also give her a shell of her own and its files: `FULLA_SHELL=1` in that machine's env file (the installer asks) turns on `<name>_exec`, which runs a command and returns its exit code and output, and `<name>_read_file`, `_write_file`, `_edit_file`, `_grep` and `_glob`, which touch files without a shell. Off, the tools are still in her list and refuse with a sentence saying so. On, everything runs as the worker's own user on your own machine, so it is your hands: she is told to do what you asked for and what that plainly takes, to say what she ran when it matters, and to do nothing destructive you did not ask for by name. A command is killed past its timeout (120 seconds, 600 at most), output is capped, and the worker's Norns key is kept out of the command's environment. And only your word starts one: the worker asks Norns what started the run, and a command, a write or an edit on a run a timer started is refused, so her nightly review and check-ins can look at a machine but not change it. `mac_exec` on the Mac, `box_exec` on a box named box.

```bash
cp .env.host.example .env.host   # NORNS_URL, NORNS_API_KEY, FULLA_URL, FULLA_HOST, FULLA_TMUX_SESSIONS
uv run --env-file .env.host fulla-host   # try it in the foreground first
uv run fulla-host install        # then install it: asks for the values, enables the systemd user units
loginctl enable-linger $USER     # keep it up while you are logged out
```

`fulla-host install` asks for the five values (flags `--norns-url`, `--norns-api-key`, `--fulla-url`, `--host`, `--tmux-sessions`; an earlier install or `.env.host` is the default), writes them to `~/.config/fulla/host.env`, and writes and enables the units from `systemd/`: the worker, and the hourly update that pulls `main` and restarts it when it changed. The same merge-to-deploy as the Mac: keep the checkout on `main` with no local edits; `sh systemd/host-update.sh` is the update by hand after a merge.

## The web

Ask her to look something up, or to read a page and learn from it ("read my homepage and remember what matters"). She doesn't read it herself: the manager holds the hands (your terminals, iMessage drafts), and a page can say anything. She hands the question to a fourth agent, `fulla-reader`, through Norns's `launch_agent`, and gets back a short answer with its sources. The reader has `web_search`, `fetch_url` and `now`, nothing that acts, and is told that what it reads is data, not instructions. She stores what a page said only when you asked her to learn from it.

`fetch_url` reads HTML, plain text and JSON from public addresses only: no Fly private network, no metadata service, no localhost, checked again on every redirect. Search uses the [Brave Search API](https://brave.com/search/api/); set `BRAVE_API_KEY` (a Fly secret in production) to turn it on. Without it, reading a page you link still works.

## Running it

Telegram: make a bot with @BotFather, get your numeric id from @userinfobot. Only that id is honoured. Or Signal or text messages, below, or all of them: Fulla listens on every messenger configured and answers on the one you used.

```bash
cp .env.example .env    # fill in the four required values
uv sync
uv run fulla
```

Set `FULLA_OWNER_NAME` to what she should call you in her prompts and in a group's thread; unset, you are "the owner".

Then give her her mornings by telling her: "set up the morning brief at 8", "check in every two hours from 9 to 7", "nightly review at midnight", "shipping check at half past", and, if you want her to get to know you, "ask me a personal question Tuesday and Friday evenings". Each is a trigger in Norns she makes with the key she holds, in your own conversation so she has the same memory as your chat, and in your own time zone; she converts to the UTC cron Norns runs. "When do your timers run" lists them; "brief at 7:30 on weekdays", "skip the check-ins" and "drop the shipping check" change them. Anything else you want on a timer is the same ask with what she should say: "every Friday at 4, ask me how the week went". It reaches her marked as scheduled, like the turns, so she writes to you rather than asking a question nobody is there to answer. Only your words set a timer; nothing she reads can. Nothing runs at start or in CI to make or move one, so `nornsctl triggers list` is always the truth, and a trigger made by hand is one she sees and can change:

```bash
nornsctl agents list                    # find fulla's id
nornsctl triggers create --agent <id> --name morning-brief \
  --cron '0 12 * * *' --message '[scheduled] morning brief' \
  --conversation-key 'telegram:<your telegram user id>'
```

A fixed UTC cron shifts by an hour at a clock change; her listing shows the local time, so you see it and say "move it back".

## Who she is

Fulla has a soul: a page, in her own words, that says how she talks and carries herself. It starts unwritten, and on your first message she introduces herself and asks how you want her: what to call you, how blunt to be, how much to say, whether to speak up unprompted. Answer in one message and she writes the page, reads the gist back, and holds to it from then on, scheduled messages included. Tell her "you figure it out" and she starts from a plain default and learns.

It changes two ways. You ask ("be warmer", "stop with the jokes", "call me boss") and she rewrites the part it bears on. Or the nightly review notices something about how you like to be dealt with (what you brushed off, what you thanked her for, how you talk to her) and revises one line, marked as learned that night; one small change at most, never removing something you said yourself. "Show me your soul" shows the page; "start over" and she asks the first questions again. Rules about what to do ("never suggest the gym before nine") are standing instructions, below, not soul.

The page is `soul.md` in the data directory and survives restarts, deploys and `/reset`. To edit it by hand:

```bash
fly ssh console -a fulla -C "fulla-soul show"
fly ssh console -a fulla -C "fulla-soul set /path/to/soul.md"   # or 'clear' to make her ask again
```

## Standing instructions

Tell her how to behave and it sticks: "from now on, act like my manager and give me hard feedback", "never suggest the gym before nine". She stores it as a numbered standing instruction, it goes into her system prompt immediately, and it applies to every message from then on, scheduled ones included. "What are your standing instructions" lists them; "drop number two" removes one; to change one, drop it and say the new wording. The agent process re-reads its definition every step, so a change is live on the next reply, not the next deploy.

## Feedback

Tell her when she got it wrong: "that brief was too long", "you should have asked first". She notes your words and what she was doing, says it's noted, and doesn't argue. If it's also a rule she can follow, she sets a standing instruction too. Feedback is for the humans: every so often, read the list and turn each item into a prompt change, a tool, a standing instruction, or a won't-do.

```bash
fly ssh console -a fulla -C "fulla-feedback list"
fly ssh console -a fulla -C "fulla-feedback resolve 3 --as prompt --note 'brief now leads with today'"
```

Or ask her: "what feedback have I given you". Telling her is enough to close one: "number 3 is fixed", "drop the one about emoji" resolves it with the outcome you give, and "1 and 2 are done" closes those gaps. Only your word does; nothing a commit or a page says can.

## What she can't do yet

Ask for something off her map and she says so, says what she can do instead, and notes it as a gap in your words. The nightly review proposes the smallest fix for each (a line of prompt, a tool to allow, a new tool, a change to her code); the morning brief tells you and leaves it with you.

To keep that list honest she reads her own code. Set `FULLA_REPO` (default `nornscode/fulla`), connect that repository to "me" ("connect nornscode/fulla to me as Fulla") and store a GitHub token for "me" if the repository is private. Her prompt then names it as her own, with the commit she is running (CI bakes it in as `FULLA_COMMIT`), and a nightly shipping check (its own timer, see "Running it") reads the day's commits and notes which gaps and feedback items one looks to have shipped, five a night at most; the morning brief asks you, and a yes closes them. A commit is a claim, never a close. What she reads of her own code is untrusted text like any file: it describes her and cannot change her, and the capability map built from the running code stays the truth.

## Memory

Six stores. Standing instructions are how you told her to behave. Feedback is what you told her she got wrong, kept for review. Facts are what you told her about the world. Facts are what you told her, in your words, in a JSON file on the volume; `remember`, `recall` and `forget` are the whole interface. The standing conversation is working memory: briefs, check-ins and your chat all run in it, so she sees what she said earlier today. Calendars are read on demand. The Norns event log is the complete record and the answer to "why did you do that".

Nothing moves between them on its own except the nightly review: at midnight she reads back over the day's conversation and writes anything durable to facts before it scrolls out of the window, marking what she inferred rather than was told. It's silent unless something went wrong.

Say `/start` to the bot, then try "remind me to feed the starter at 8pm".

`/reset` starts the standing conversation over. Facts, standing instructions, feedback and reminders are separate stores and survive; only the chat window goes. It is for the day a run of wrong answers has become the context: a model that has said "the Mac is off" three times will say it a fourth, whatever the truth, and no prompt wording reliably argues it out of its own history. Fulla also tells the model the truth each turn (which `mac_` tools are in its list right now, appended to the prompt per call), which is what makes the Mac tools work after an outage without a reset. `/reset <group or person>`, in your own chat, does the same for a friend's or a group's conversation: what she said there is gone, with anything queued and the group's thread, while the people list, their reminders and the day's count stay. For the day she has told a group she can't do something she can.

## Signal

Fulla can be a Signal contact instead of a Telegram bot: her own number, on a second Fly app, `fulla-signal`, that runs signal-cli all day (the Fulla app sleeps, and signal-cli must stay connected). You need a number that can take one SMS or voice call, kept paid for; most VoIP numbers are refused, a prepaid SIM is not.

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

Both messengers can stay configured. Fulla answers each message on the app it came from, and `FULLA_CHANNEL` names the primary: the one that gets the morning brief, reminders and anything else nobody asked for from a particular app. Changing the primary starts a new standing conversation (`signal:<your number>`), so the chat history starts over; facts, standing instructions, the soul and reminders are in their own stores and carry over. The triggers from "Running it" carry the old key: her listing marks each as not in your conversation, and setting one again ("brief at 8") moves it, or re-create them with `--conversation-key 'signal:<your number>'`; otherwise the morning brief lands where nobody is listening. Questions arrive with a "Reply to this with one of" line in place of buttons; swipe to reply with one of them, or react (👍 is done or yes, 👎 is drop or no, 😴 snoozes an hour). There is no Working… message on Signal, only the typing indicator; while she is working, a message that is just "wait", "cancel", "stop" or "nvm" cancels the run (that works on Telegram too). Messages are never lost: the forwarder keeps each one on the volume until Fulla has taken it, so a slow start, a deploy or a wrong secret only delays delivery. Logs: `fly logs -a fulla-signal`; the forwarder's lines start with `forward`, and `forward-spool/` under the volume is what is still waiting.

Deploys of `deploy/signal/` go through `.github/workflows/signal.yml` on merge, with a `FLY_API_TOKEN_SIGNAL` repository secret from `fly tokens create deploy -a fulla-signal`. The same secret lets `signal-watch.yml` check every half hour that the machine is running, start it if not, and fail so GitHub mails you: the machine has no public service, so nothing else would notice, and Fly's restart policy is set to never give up on it. Bump the pinned image monthly; old signal-cli releases stop working when Signal changes its servers.

## Text messages

Fulla can also be a number you text, through Twilio. If her Signal number is a Twilio number it is the same one: Signal used it once, for its registration code, and a text to it reaches Twilio, not Signal. Check in the Twilio console that the number has SMS on it; a voice-only number will not do.

```bash
fly secrets set -a fulla TWILIO_ACCOUNT_SID=AC… TWILIO_AUTH_TOKEN=… FULLA_SMS_NUMBER=+1FULLA FULLA_SMS_OWNER=+1YOU
```

On start Fulla points the number's webhook, and its fallback, at `https://fulla.fly.dev/sms`; there is nothing to set in the console. Twilio signs each POST with the auth token, and only your number is heard. Everything the other messengers do with a message after sending it is missing here: no buttons, no edits, no typing indicator, no reactions, no quoting. A question comes with a "Reply with one of: Done, Snooze 1h, Snooze 1d, Drop it" line, and a reply that is just one of those labels answers it, the newest question that offers it (the same shortcut works on Signal, where you may not have swiped). A long answer arrives as several texts, split at paragraphs. Markdown is flattened to plain text and the typographic punctuation a model likes (the ellipsis, curly quotes, dashes, bullets) is swapped for plain ASCII, since one such character drops a whole message from 160 characters a segment to 70; emoji are kept. Each segment is billed, so a long answer costs a few cents. Friends and groups are not a thing here: SMS carries your own chat with the manager and nothing else.

Twilio gives the webhook fifteen seconds and does not retry on its own; the fallback is the same route, so a message that arrived while the machine was cold or Norns was unreachable gets one more try. `FULLA_CHANNEL=sms` makes it the primary. Local dev polls Twilio's message list every few seconds, so no tunnel is needed.

## Friends and groups

Fulla talks to you, and to nobody else unless you say so. Two lists, kept in `people.json` and changed from chat:

- People. "Let Sam talk to you, he's +1 604 555 0111" or "Sam's Telegram id is 12345" calls `allow_person`; "drop Sam" closes the door. A listed person can message Fulla directly on either app.
- Groups. Add Fulla to a group (her Signal number, or the Telegram bot) and say something there. She asks you privately, on your primary app, whether she may talk in it, with Yes and No; answer that message, or tell her "allow the friends group". In an allowed group she answers only when addressed: a message starting with "Fulla", a mention of the bot, or a reply to something she said. Only listed people are heard there, you included. When someone you have not listed addresses her in an allowed group, she asks you privately, once, with their name and id: yes lets them in, no ignores them for good (`list_people` shows both). Signal often shows a person by account id rather than number, so a friend allowed by number may arrive that way; the question carries the id, and `allow_person` takes it.

What they get is a different Fulla: the `fulla-social` agent one to one, and each allowed group its own agent, each with its own conversation, a tool list of the clock, the group's reminders, what you enabled for that group, and none of your facts, calendar, reminders, machines, standing instructions or soul. That tool list is the wall, not a promise in the prompt, and a group's tools reach only that group's sources whoever asks. Everyone's words but yours arrive inside the untrusted marker, so an instruction from a friend is data, not a request. In a group she also gets the thread: the last few messages before the one that addressed her (`FULLA_GROUP_CONTEXT`, 8, none older than two hours), everyone's including yours and her own, as untrusted text, so "Fulla, remind us about that on Wednesday" means something. On Telegram that needs the bot's privacy mode off in BotFather, or she never sees the unaddressed ones. Each conversation answers at most `FULLA_SOCIAL_DAILY_CAP` messages a day (50), then says so once and goes quiet until tomorrow; other people can run up your bill, and that is the ceiling. "Fulla, remind us in an hour to leave" works in a group: the reminder is a run of its own and is posted back into that chat when due, tagging whoever asked for it (so Dana's reminder reaches Dana, not you), or the person it is for when the ask names one ("Fulla, remind @Dana to bring the cake" tags Dana, as long as Dana has spoken in that group), and she stays free to talk meanwhile. Fulla tells the people in a group apart: everyone who speaks in an allowed group is remembered as one of its members, listed or not. `list_people` shows all of it, members included.

Each allowed group also gets a directory of its own, and you decide what goes in it from your own chat. `list_groups` shows them. "The friends group can use the web and memory" is `set_group_tools`, from a fixed catalogue (web, memory, github, linear, linear_edit, coding). "Connect nornscode/fulla to the friends group as Fulla" is `connect_group_source`; sources are GitHub repositories, pages, Linear teams and projects, and are the only things a group's tools will ever reach. A token for a group ("here's a GitHub token for the friends group") is `set_group_credential`: it is stored encrypted with `FULLA_SECRET`, never typed in a group, and never read back by anything; `list_groups` says only that it is set. Set `FULLA_SECRET` to a long random string before storing one. A fine-grained token wants, on the connected repositories, read on Contents, Pull requests, Issues, Commit statuses and Actions (CI comes from the Actions workflow runs, since GitHub offers no Checks permission on a fine-grained token), and write on Contents and Pull requests if you will merge or post reviews from a card; a CI source the token cannot read is left out of the answer, never a refusal. Each allowed group then talks to an agent of its own whose tool list is exactly what you enabled: "web" lets it read the web through the reader, and the group's `group_sources` tells it what is connected. A change takes effect on the group's next turn. "github" gives the group reads over its repositories (pull requests with CI and reviews, issues, commits, files, search), with everything strangers wrote marked as untrusted, and three asks of you: "Fulla, merge #12", "file an issue for this" or "review #12" in the group becomes a card in your own chat with Merge, File, Edit and Cancel, or Post, Edit and Cancel, done with that group's token only when you choose, the merge and the review pinned to the commit the card showed. "memory" gives it `group_remember` and `group_recall` over its own facts, screened for anything that reads like instructions. "linear" does the same for Linear: issues in the teams (by key) and projects (by slug) you connected, one issue in full, and "file this in Linear" as a File card in your chat, with the group's own Linear key. "The testing group can edit Linear" (`linear_edit`) lets that group change its own issues without a card: "Fulla, assign ENG-12 to Sam", "move it to Done", "make it urgent", "label it bug", "comment that it's blocked on the API", "file an issue for this" all happen at once with the group's key, inside the teams and projects you connected, and she says what she did. Assignees go by name as Linear knows them, Linear's own agents included; nothing a group can do deletes or archives, and merges stay a card for you. A group never reaches a repository, team or project you did not connect to it, whoever asks and however they phrase it. "The testing group can code" (`coding`) gives that group the Bubbo worker's coding sandboxes, for anyone in it: "Fulla, make the date parser accept PT in widgets" provisions a sandbox on one of the group's own `github_repo` sources (any other repository is refused, even one Bubbo itself would take), briefs a coding agent there, checks back on its own (`schedule_group_followup`, a check-back that comes back into the group's conversation rather than a `wait`), reports in a sentence or two and offers a pull request, which `open_pull_request` opens as a draft at once, with no card for you. A group drives only the sandboxes and jobs it created; neither another group nor you can drive one of its, and it cannot drive yours. The Bubbo worker must be connected, with the group's repositories on its enabled list and a token that can push to them.

The same tools work for you, over your own sources. "Connect nornscode/fulla for me as Fulla" and "here's a GitHub token for me" put them in a directory of your own, and `list_groups` shows them first; then "what's open on Fulla?" in your own chat reads pull requests, and "merge #12" or "approve #12, looks good" puts the same Merge or Post card in front of you. Your own Linear needs no switch: "assign FUL-12 to me", "mark it done" and "file an issue" happen at once in your chat. Your tools are fixed; what changes is what they can reach.

A pasted GitHub token expires. A GitHub App of your own does not: create one (Settings, Developer settings, GitHub Apps; no webhook, no callback URL), give it repository permissions of read on Metadata, Actions, Checks and Commit statuses and read and write on Contents, Pull requests and Issues, generate a private key, and install it on the repositories you connect. Then set its app id and the key file's text as Fly secrets:

```bash
fly secrets set FULLA_GITHUB_APP_ID=123456 FULLA_GITHUB_APP_KEY="$(cat fulla.private-key.pem)"
```

With both set, no group and not you need a GitHub token: for each call Fulla mints a token good for an hour, for that one repository, and only if it is among the sources of the group asking. `list_groups` shows github as "the app", and a GitHub token stored earlier is left unused. A connected repository the app is not installed on answers with a sentence saying so. What a card does (a merge, an issue, a review) is then the app's on GitHub, not yours: an approval is the app's approval, and a merge is by the app.

Telegram bots in groups receive only commands, replies to them and mentions unless privacy mode is off: in @BotFather, `/setprivacy`, Disable, then re-add the bot to the group, or address her with @mentions only.

## Deploying

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

## Evals

`fulla-eval` is the manager's regression set. Each case in `evals/cases/*.yaml` is a short scripted conversation sent straight to the model with the real manager prompt and tool schemas, built the way the worker builds them, and checked on what she says and which tool she calls. Nothing is executed: a tool call gets a stubbed result back. No Norns, no messenger. It needs `ANTHROPIC_API_KEY` and skips cleanly without one, so CI does not run it.

```bash
uv run --env-file .env fulla-eval                 # every case, against fulla.model
uv run --env-file .env fulla-eval --repeat 5      # see which cases are flaky
uv run --env-file .env fulla-eval --model claude-haiku-4-5 -k timezone
```

Every fixed bug adds a case. Write the owner's message as it reached her, the tool results that led her astray, and a check that fails on the old behaviour:

```yaml
why: Told "11am PT" she converted the time herself (run 220).
connected: [mac]          # machines whose tools are in the list this turn; default none
now: "2026-10-19T09:30"   # what `now` answers, in the owner's zone
stubs: {recall: "#3 Bank closes at 4pm Pacific"}   # other tools get a plain empty answer
messages:
  - user: remind me at 11am PT tomorrow to call the bank
checks:
  - called: schedule_reminder              # also not_called, first_call, no_tools; args are regexes
    args: {due_at: "^2026-10-20T11:00", tz: "^America/Los_Angeles$"}
  - not_reply: "(?i)eastern"               # reply / not_reply: a regex over everything she said
  - judge: Does she confirm 11am Pacific?  # the model grades it, YES passes; last resort
```

A case with `agent: social` runs `fulla-social` instead, the agent friends and groups talk to; give each `user:` a `from:` name and it is marked as the channel marks it.

Run the new case against the old prompt first and see it fail. Prefer a tool check or a regex; a judge question is slower and noisier.

## Debugging

`nornsctl runs list` shows every run, including reminders asleep in `waiting`. `nornsctl runs events <id>` is the log of what a run did and why. A reminder's whole life is three events long until it's due.

## License

MIT
