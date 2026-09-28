"""Device Hub — phones and laptops as NORA's bodies (NORA_DISTRIBUTED_PLAN.md §2, §5).

  protocol     wire format, ids, keys, manifest checks (pure)
  registry     paired devices and pairing codes, in the core store
  server       the WebSocket endpoint, sessions, invoke/confirm
  fake_device  a protocol v1 client for tests and manual checks

`python -m nora.hub` manages pairing from a terminal on the core.
"""
