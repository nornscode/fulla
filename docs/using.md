# Using Fulla

What to say to her, and what she keeps.

## Timers

Give her her mornings by telling her: "set up the morning brief at 8", "check in every two hours from 9 to 7", "nightly review at midnight", "shipping check at half past", and, if you want her to get to know you, "ask me a personal question Tuesday and Friday evenings". Each is a trigger in Norns she makes with the key she holds, in your own conversation so she has the same memory as your chat, and in your own time zone; she converts to the UTC cron Norns runs. "When do your timers run" lists them; "brief at 7:30 on weekdays", "skip the check-ins" and "drop the shipping check" change them. Anything else you want on a timer is the same ask with what she should say: "every Friday at 4, ask me how the week went". It reaches her marked as scheduled, like the turns, so she writes to you rather than asking a question nobody is there to answer. Only your words set a timer; nothing she reads can. Nothing runs at start or in CI to make or move one, so `nornsctl triggers list` is always the truth, and a trigger made by hand is one she sees and can change:

```bash
nornsctl agents list                    # find fulla's id
nornsctl triggers create --agent <id> --name morning-brief \
  --cron '0 12 * * *' --message '[scheduled] morning brief' \
  --conversation-key 'telegram:<your telegram user id>'
```

A fixed UTC cron shifts by an hour at a clock change; her listing shows the local time, so you see it and say "move it back".

## Calendar

Fulla reads calendars from iCal feeds, read-only. In Google Calendar, open the calendar's settings and copy "Secret address in iCal format". Put one or more in `FULLA_CALENDARS` as `Name=url,Other=url`. She uses them for the brief, for scheduling questions, and for check-ins.

## Check-ins

A second trigger, every two hours during the day, lets her be useful without being asked: a free hour before an evening event is a gym window, a friend you mentioned hasn't come up in weeks, you said you wanted to walk more. She suggests one thing or stays silent, and silence is the default. A silent run ends with `[silent]`, which the channel doesn't relay, so the log still says she looked.

## The web

Ask her to look something up, or to read a page and learn from it ("read my homepage and remember what matters"). She doesn't read it herself: the manager holds the hands (your terminals, iMessage drafts), and a page can say anything. She hands the question to a fourth agent, `fulla-reader`, through Norns's `launch_agent`, and gets back a short answer with its sources. The reader has `web_search`, `fetch_url` and `now`, nothing that acts, and is told that what it reads is data, not instructions. She stores what a page said only when you asked her to learn from it.

`fetch_url` reads HTML, plain text and JSON from public addresses only: no Fly private network, no metadata service, no localhost, checked again on every redirect. Search uses the [Brave Search API](https://brave.com/search/api/); set `BRAVE_API_KEY` (a Fly secret in production) to turn it on. Without it, reading a page you link still works.

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

To keep that list honest she reads her own code. Set `FULLA_REPO` (default `nornscode/fulla`), connect that repository to "me" ("connect nornscode/fulla to me as Fulla") and store a GitHub token for "me" if the repository is private. Her prompt then names it as her own, with the commit she is running (CI bakes it in as `FULLA_COMMIT`), and a nightly shipping check (its own timer, see "Timers") reads the day's commits and notes which gaps and feedback items one looks to have shipped, five a night at most; the morning brief asks you, and a yes closes them. A commit is a claim, never a close. What she reads of her own code is untrusted text like any file: it describes her and cannot change her, and the capability map built from the running code stays the truth.

## Memory

Six stores. Standing instructions are how you told her to behave. Feedback is what you told her she got wrong, kept for review. Facts are what you told her about the world. Facts are what you told her, in your words, in a JSON file on the volume; `remember`, `recall` and `forget` are the whole interface. The standing conversation is working memory: briefs, check-ins and your chat all run in it, so she sees what she said earlier today. Calendars are read on demand. The Norns event log is the complete record and the answer to "why did you do that".

Nothing moves between them on its own except the nightly review: at midnight she reads back over the day's conversation and writes anything durable to facts before it scrolls out of the window, marking what she inferred rather than was told. It's silent unless something went wrong.

Say `/start` to the bot, then try "remind me to feed the starter at 8pm".

`/reset` starts the standing conversation over. Facts, standing instructions, feedback and reminders are separate stores and survive; only the chat window goes. It is for the day a run of wrong answers has become the context: a model that has said "the Mac is off" three times will say it a fourth, whatever the truth, and no prompt wording reliably argues it out of its own history. Fulla also tells the model the truth each turn (which `mac_` tools are in its list right now, appended to the prompt per call), which is what makes the Mac tools work after an outage without a reset. `/reset <group or person>`, in your own chat, does the same for a friend's or a group's conversation: what she said there is gone, with anything queued and the group's thread, while the people list, their reminders and the day's count stay. For the day she has told a group she can't do something she can.

## Follow-ups

Fulla's own check-backs ("look at that build again in twenty minutes") work like reminders, with an agent of their own, `fulla-followup`: `schedule_followup` starts a run that sleeps until due, posts `[scheduled] follow-up: <what>` into the manager's conversation, and finishes. The manager looks, and tells you only if it is worth saying; otherwise `[silent]`. The follow-up run itself is never shown to you. A coding sandbox is watched instead (`watch_environment`): `fulla-watch` polls it on a small model and wakes the manager once, when the job ends.

## Settings

Set `FULLA_OWNER_NAME` to what she should call you in her prompts and in a group's thread; unset, you are "the owner".

Only the manager runs on `FULLA_MODEL` (Opus by default). The reader, the Mac queue, friends and groups run on `FULLA_MID_MODEL` (Sonnet), and reminders, follow-ups and the watcher on `FULLA_LIGHT_MODEL` (Haiku).
