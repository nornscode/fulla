"""Fulla's hands on the owner's Mac.

A second, tools-only Norns worker runs on the Mac under launchd and serves
the `mac_*` tools: Things, Apple Reminders, iMessage, notifications, and AppleScript
against a short list of apps. The manager on Fly allowlists these tools but
never serves them; Norns dispatches a call by name to whichever connected
worker holds it, so the tools are offered exactly while the Mac is up.

Everything here is channel-blind and Norns-blind except `worker.py`.
"""
