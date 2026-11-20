"""Declare Fulla as a serverless worker, once.

The declaration is what Norns advertises while the worker is asleep, so it
must match what the SDK registers on join. It is generated from the same
Agent definitions the worker runs, through the SDK's own join payload, never
hand-written.

Usage:
  fulla-register --wake-url https://fulla.fly.dev/norns/wake [--fly-app fulla]

Needs NORNS_URL and NORNS_API_KEY in the environment. Idempotent: an existing
declaration for the worker id is updated in place and its secret is kept. Pass
--recreate to delete and re-create it, which issues a new secret. With
--fly-app the new secret is stored as NORNS_WAKE_SECRET on that app; without
it, the secret is printed once.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import httpx
from norns import Norns

from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.hosts import HostStore
from fulla.soul import Soul
from fulla.worker import build_agents

WORKER_ID = "fulla"


def declaration(worker_id: str = WORKER_ID, wake_url: str = "") -> dict:
    """Tools and capabilities exactly as the SDK would send them on join."""
    config = load_config()
    data_dir = config.fulla.data_dir
    agents = build_agents(config, DirectiveStore(data_dir / "directives.json"),
                          HostStore(data_dir / "hosts.json"), Soul(data_dir / "soul.md"))
    join = Norns("http://unused")._join_payload(agents, worker_id)
    return {
        "worker_id": worker_id,
        "wake_url": wake_url,
        "tools": join["tools"],
        "capabilities": join["capabilities"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wake-url", required=True)
    parser.add_argument("--worker-id", default=WORKER_ID)
    parser.add_argument("--fly-app", help="store the secret as NORNS_WAKE_SECRET on this Fly app")
    parser.add_argument("--recreate", action="store_true", help="delete and re-create (new secret)")
    args = parser.parse_args(argv)

    url = os.environ["NORNS_URL"].rstrip("/")
    http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {os.environ['NORNS_API_KEY']}"}, timeout=30)

    decl = declaration(args.worker_id, args.wake_url)
    existing = next(
        (w for w in http.get("/api/v1/serverless_workers").json()["data"] if w["worker_id"] == args.worker_id),
        None,
    )

    if existing and args.recreate:
        http.delete(f"/api/v1/serverless_workers/{existing['id']}").raise_for_status()
        existing = None

    if existing:
        resp = http.patch(f"/api/v1/serverless_workers/{existing['id']}", json=decl)
        resp.raise_for_status()
        data = resp.json()["data"]
        print(f"Updated serverless worker {data['id']} ({data['worker_id']}): "
              f"{len(data['tools'])} tools, wake_url {data['wake_url']}. Secret unchanged.")
        return 0

    resp = http.post("/api/v1/serverless_workers", json=decl)
    if resp.status_code != 201:
        print(f"Registration failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1
    data = resp.json()["data"]
    print(f"Created serverless worker {data['id']} ({data['worker_id']}): "
          f"{len(data['tools'])} tools, wake_url {data['wake_url']}.")

    secret = data["secret"]
    if args.fly_app:
        subprocess.run(
            ["fly", "secrets", "set", "-a", args.fly_app, f"NORNS_WAKE_SECRET={secret}"],
            check=True,
        )
        print(f"Stored NORNS_WAKE_SECRET on Fly app {args.fly_app}.")
    else:
        print("Secret (shown once, store it as NORNS_WAKE_SECRET):")
        print(secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())
