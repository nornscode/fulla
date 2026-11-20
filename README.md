# Fulla

A private personal manager. You tell her things, she keeps them. You ask to be reminded, she asks you when it's due and keeps asking until you say it's done. Every morning she tells you what's coming. She talks to you on Telegram, Signal or SMS, and to nobody else unless you say so.

Built on [Norns](https://github.com/nornscode/norns). Third reference agent after [Mimir](https://github.com/nornscode/norns-mimir-agent) and [Hermod](https://github.com/nornscode/hermod), and the first one that's for a person rather than a team. It leans on the parts of durable execution that a daemon on a laptop can't give you: a commitment that waits for days at no cost, a question that parks until you answer it, and a log that says what happened overnight.

Named for Frigg's attendant, who carried her casket and kept her secrets.

## What she does

- **Remembers and reminds.** Facts in your words, reminders that chase you until done, a morning brief, quiet check-ins, read-only calendars. [Using Fulla](docs/using.md)
- **Learns how you want her.** A soul she writes with you, standing instructions, feedback, and a list of what she can't do yet. [Using Fulla](docs/using.md)
- **Has hands on your machines.** Things, Reminders, iMessage drafts you approve, and your home on a [Mac](docs/mac.md); tmux, a shell and files on [any machine](docs/machines.md) you allow.
- **Reads the web** through a separate agent that can't act. [Using Fulla](docs/using.md#the-web)
- **Talks to friends and groups** you let in, as a different Fulla with none of your data, plus GitHub, Linear and coding tools you enable per group. [Friends and groups](docs/friends-and-groups.md)

## How it works

```
Telegram / Signal / SMS ◄──► fulla (one process on Fly, asleep when idle)
                               ├── channel: your messages in, her answers and questions out
                               └── Norns worker: serves the agents' tools and LLM calls
                                        │
                                        ▼
                                   Norns runtime
                                        ├── fulla            the manager, one standing conversation
                                        ├── fulla-reminder   one run per commitment
                                        └── small agents for follow-ups, the web, the Mac queue, friends and groups
```

The manager chats; anything that waits is its own run. A parked run treats the next message as its answer, so the thing that chats must not be the thing that sleeps. Norns is the only always-on process: a message, a due reminder or a timer wakes Fulla, and she stops again when idle. More in [architecture](docs/architecture.md).

## Running it

Make a Telegram bot with @BotFather and get your numeric id from @userinfobot. Norns must be reachable at `NORNS_URL`.

```bash
cp .env.example .env    # bot token, your id, Norns URL and key, ANTHROPIC_API_KEY
uv sync
uv run fulla
```

Say `/start` to the bot, then try "remind me to feed the starter at 8pm". Then set up her [timers](docs/using.md#timers) by telling her: "morning brief at 8".

## Docs

- [Using Fulla](docs/using.md): timers, calendar, check-ins, the web, soul, standing instructions, feedback, memory, `/reset`
- [Messengers](docs/messengers.md): Telegram, Signal, text messages
- [Your Mac](docs/mac.md) and [other machines](docs/machines.md)
- [Friends and groups](docs/friends-and-groups.md)
- [Deploying](docs/deploying.md): Fly, registering with Norns, how she sleeps
- [Evals](docs/evals.md)
- [Architecture](docs/architecture.md); [AGENTS.md](AGENTS.md) for the rules the code holds to

## Development

```bash
uv run pytest
uv run ruff check .
uv run pyright
```

Changes land through pull requests; a merge to `main` deploys. `nornsctl runs list` shows every run, and `nornsctl runs events <id>` is the log of what one did and why.

## License

MIT
