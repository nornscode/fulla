"""Create the run webhook that tells the channel about run state changes.

Usage:
  fulla-register-channel --url https://fulla.fly.dev [--fly-app fulla]

Needs NORNS_URL and NORNS_API_KEY in the environment. Idempotent by URL: an
existing webhook at <url>/norns/events is kept and its secret left alone;
--recreate deletes and re-creates it, issuing a new secret. With --fly-app
the secret is stored on that app as NORNS_WEBHOOK_SECRET; otherwise it is
printed once. The Telegram side needs nothing here: the app sets its own
webhook at startup, with a token derived from this secret.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="the app's public base URL")
    parser.add_argument("--fly-app")
    parser.add_argument("--recreate", action="store_true")
    args = parser.parse_args(argv)

    events_url = f"{args.url.rstrip('/')}/norns/events"
    norns = httpx.Client(
        base_url=os.environ["NORNS_URL"].rstrip("/"),
        headers={"Authorization": f"Bearer {os.environ['NORNS_API_KEY']}"},
        timeout=30,
    )

    existing = next((w for w in norns.get("/api/v1/run_webhooks").json()["data"] if w["url"] == events_url), None)
    if existing and args.recreate:
        norns.delete(f"/api/v1/run_webhooks/{existing['id']}").raise_for_status()
        existing = None
    if existing:
        print(f"Run webhook {existing['id']} already points at {events_url}; secret unchanged.")
        return 0

    resp = norns.post("/api/v1/run_webhooks", json={"url": events_url})
    if resp.status_code != 201:
        print(f"Creating the run webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1
    data = resp.json()["data"]
    print(f"Created run webhook {data['id']} -> {events_url} for {data['events']}.")

    if args.fly_app:
        subprocess.run(["fly", "secrets", "set", "-a", args.fly_app, f"NORNS_WEBHOOK_SECRET={data['secret']}"], check=True)
        print(f"Stored NORNS_WEBHOOK_SECRET on Fly app {args.fly_app}.")
    else:
        print("Secret (shown once, store it as NORNS_WEBHOOK_SECRET):")
        print(data["secret"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
