"""What a Norns app shares: clients for the services an app reads on a
project's behalf (GitHub, Linear), written so that scope is the caller's
business. Nothing here knows which project or group it serves, holds a
token of its own, or decides what a model may name; the app that calls it
does (Fulla through `groupctx`, Mimir through its project). A directory in
Fulla's repository for now, to be extracted when Mimir takes it too.
"""
