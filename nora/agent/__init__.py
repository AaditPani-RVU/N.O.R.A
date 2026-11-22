"""NORA's device agent for the travel laptop (distributed plan Phase 8e).

The home core stays home; this runs on the laptop the user carries. It
pairs like the phone (its own P-256 key, a one-use code, approval on the
core), keeps a connection open over the tailnet, and offers a short list of
`laptop.*` capabilities (`capabilities.py`). The core's notifications —
reminders, finished jobs, leave-by alerts — show up as desktop
notifications here, and `chat` is a terminal conversation with NORA.

Setup on the travel laptop, from a checkout of this repo:

    pip install websockets cryptography jsonschema
    python -m nora.hub pair                          # on the core
    python -m nora.agent pair wss://<core>.ts.net/v1/device ABCD-EFGH
    python -m nora.hub approve <device_id>           # on the core
    python -m nora.agent run                         # or: chat

Only `nora.hub.protocol` and this package are imported, so none of the
core's own dependencies are needed here.
"""
