"""Gateways — NORA reachable from somewhere other than the microphone.

NORA currently exists only where the mic is. Everything it can do is gated on
being in the room: `listener` opens a local input device, `pipeline.run` is a
loop around that device, and walking away ends the session.

A gateway is the same assistant addressed over a message. `core.handle_text`
is a headless turn — no audio in, text out — and each transport module drives
it. Telegram is the one implemented; the shape is deliberately transport-shaped
so a second one is a file, not a refactor.
"""
