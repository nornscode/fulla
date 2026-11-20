# Fulla

Fulla is a private assistant you talk to over Telegram, Signal, or SMS. It remembers what you tell it, follows up on reminders until you mark them done, and can send you a morning brief based on your calendar.

## What you can do

- Set reminders and check-ins, connect read-only calendars, and give Fulla standing instructions. [Using Fulla](docs/using.md)
- Ask Fulla to look something up. It uses a separate agent that can search and read web pages. [Web access](docs/using.md#the-web)
- Connect your Mac to use Things, Apple Reminders, and Shortcuts, or to send iMessages you approve. You can also give Fulla access to tmux, a shell, and files on machines you choose. [Mac setup](docs/mac.md) · [Other machines](docs/machines.md)
- Let friends and groups talk to Fulla in separate chats without giving them access to your personal data. You choose which groups get GitHub, Linear, or coding tools. [Friends and groups](docs/friends-and-groups.md)

## Run locally

Use a separate Telegram bot for development. Create it with @BotFather and get your numeric Telegram user ID from @userinfobot. You'll also need a running Norns instance.

```bash
cp .env.example .env
uv sync
```

In `.env`, set `FULLA_TELEGRAM_BOT_TOKEN`, `FULLA_TELEGRAM_USER_ID`, `NORNS_URL`, `NORNS_API_KEY`, and `ANTHROPIC_API_KEY`.

Register the run webhook so Norns can send Fulla updates. If both run directly on the same machine, the default address is `http://localhost:9000`. Otherwise, use an address that Norns can reach.

```bash
uv run --env-file .env fulla-register-channel --url http://localhost:9000
```

Save the webhook secret in `.env` as `NORNS_WEBHOOK_SECRET`, then start Fulla.

```bash
uv run --env-file .env fulla
```

Send the bot `/start`, then try “Remind me to feed the starter at 8 pm.” To set up a morning brief, say “Send me a morning brief every day at 8 am.” [Timers](docs/using.md#timers)

## How it works

Fulla uses Norns to manage conversations, timers, and reminders. Each reminder runs separately, so you can keep chatting while it's pending. In production, Fulla runs on Fly and stops when idle. Incoming messages and Norns requests wake it. [Norns](https://github.com/nornscode/norns) · [Architecture](docs/architecture.md)

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pyright
uv run pytest
```

Use `nornsctl runs list` to find a run and `nornsctl runs events <id>` to see what happened. Changes go through pull requests. Merges to `main` deploy to Fly when CI passes and deployment is configured.

## More docs

- [Messenger setup](docs/messengers.md)
- [Deployment](docs/deploying.md)
- [Model evals](docs/evals.md)
- [Repository guidelines](AGENTS.md)

## License

[MIT](LICENSE)
