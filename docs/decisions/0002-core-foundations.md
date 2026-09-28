# ADR-0002: Core foundations — store, channels, delivery, Device Hub (Phase 2)

- **Date:** 2026-09-28
- **Status:** implemented (uncommitted, branch `phase2/core-foundations`)
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** new `nora/store.py`, `nora/channel.py`, `nora/delivery.py`, `nora/hub/`; changed `nora/jobs.py`, `nora/scheduler.py`, `nora/task_ledger.py`, `nora/pipeline.py`, `nora/gateway/core.py`, `nora/planner.py`, `nora/wiring.py`, `nora/command_engine.py`, `nora/audit_log.py`, `nora/schemas.py`, `config.yaml`, `requirements.txt`, tests

## Context

Phase 2 of `NORA_DISTRIBUTED_PLAN.md`: give the core what devices need before
the Android app exists. The exit criteria were that a fake device pairs,
advertises `test.echo`, the LLM can call it, confirmation round-trips, the
audit log shows device and origin, and desktop behaviour is unchanged.

## Decision

- **Store** (`nora/store.py`): one SQLite file, `nora_core.db`, in WAL mode
  with one connection per thread. The schema is versioned by `PRAGMA
  user_version`, and migrations are append-only. Jobs, schedules and tasks
  moved in with their public functions unchanged. The legacy JSON files are
  imported once and renamed `*.json.migrated`; an unreadable file is left in
  place. `NORA_STORE_PATH` overrides the location, and the test suite sets it.
  - Jobs and tasks read from the store on every call. The scheduler keeps an
    in-memory map that writes through, because `add()` has always returned
    the live object the ticker fires, and callers rely on that.
- **Channels** (`nora/channel.py`): `pipeline.handle_turn(text, deps,
  channel=None)`. A channel is the turn's return address: a device id, a
  `speak`, an optional `confirm`, an origin, and a turn id. With no channel the
  turn is the local microphone's, exactly as before. The current channel sits
  in a context variable, and the `run_in_executor` calls copy it, so the
  audit log and job submission can attribute work without new parameters.
  - `gateway/core.handle_text` is now `handle_turn` on a text channel with no
    `confirm`. The old headless turn skipped both NeuroSym guards and the
    risk, autonomy and confidence ladder, so Telegram now gets the same checks
    as the microphone. A channel with no `confirm` refuses anything that needs
    confirming, as before.
  - Only the local channel can end the process by saying "goodbye".
  - The planner takes the channel's `speak` and `confirm`. With no confirm, a
    multi-step plan is refused at pre-flight.
- **Delivery** (`nora/delivery.py`): a job or schedule records the device
  that asked for it. A device's answer goes to that device if it is connected.
  Otherwise it is held (`delivered = 0`) and flushed when the device
  reconnects, for up to 12 hours. Local work keeps the focus-gated local
  speaker.
- **Device Hub** (`nora/hub/`): protocol v1 from plan §5.
  - Pairing uses a single-use code, valid 5 minutes and stored only as a
    SHA-256 hash. Five wrong guesses void every outstanding code. A newly
    paired device is *pending* until the user runs `python -m nora.hub
    approve`.
  - Each connection authenticates by challenge and response: ECDSA P-256 over
    `nonce ‖ device_id ‖ "nora-v1"`. Unknown, pending and revoked devices all
    get the same refusal. Revocation from the CLI disconnects the device live,
    within about 2 seconds.
  - Capabilities are registered as ordinary commands (category `device`) while
    the device is connected and removed when it disconnects. A device cannot
    take a name that belongs to a local command or to another device.
  - Params are validated against the manifest's JSON Schema, with
    `additionalProperties: false` added when the schema doesn't say.
  - Effective tier is `max(core floor, device tier)`, plus one when the origin
    is not `live_user`. Tier 4 is never registered.
  - At tier 2 or above, the executing device is asked to confirm. If the turn
    already came from that device and its user approved these steps, that
    approval counts, so the user isn't asked twice on the same screen. The
    device still checks that the invocation matches a step it approved under
    that id.
  - Every invocation gets a row in `invocations`. Audit rows now carry
    `device`, `origin`, `turn_id`, `executed_on` and `confirmed_by`.
  - The hub binds loopback only and refuses any other `hub.host`. It is off by
    default (`hub.enabled: false`).
- **Fake device** (`nora/hub/fake_device.py`): a real protocol v1 client with
  its own key, its own tier table, deadline checks, replay deduplication for
  10 minutes, and a kill switch. It serves as the test double and as a manual
  CLI.

## Deviations from the plan

| Plan | Here | Why |
|---|---|---|
| `POST /v1/pair` | a `pair` message as the first frame on `/v1/device` | The `websockets` server can't read a POST body. This keeps one endpoint and one listener; the security properties are the same |
| QR code on the dashboard | `python -m nora.hub pair` prints the code | The QR flow belongs with the Android scanner in Phase 3. The store is shared, so the dashboard can add it without touching the hub |
| Delivery "picks the most recently active device" | Delivers to the device that asked, and holds the answer if it is offline | Answering on a different device than the one that asked surprised more than it helped. `note_active` is recorded for when a real fallback exists (phone notification, Phase 5) |
| `proactive`, `anomaly_watchdog` and `terminal_monitor` use the router | They stay on the local speaker | They are about this laptop. Moving them has no benefit until a phone can receive them |
| `memory` JSON facts in the store | Not moved | Phase 2 scoped the store to jobs, schedules and tasks. Memory goes in with the next migration |
| `resume_from_seq` and event outbox replay | `welcome` always sends 0, and events are acked and logged | Event handling is Phase 8. Replay is safe anyway because devices deduplicate invocation ids |

## Blast radius & reversibility

- **On first boot after merge**, the store imports `nora_jobs.json` and
  `nora_tasks.json` and renames them. This was tested against copies of the
  live files: 2 tasks and 0 jobs imported, both files renamed. To roll back,
  check out the previous commit, rename the `.migrated` files back, and delete
  `nora_core.db*`.
- The hub is off by default. With it off, the only runtime changes are the
  store and the channel plumbing. The local-mic path is covered unchanged by
  `test_pipeline_smoke`.

## Security & data impact

- New surface: a WebSocket listener on 127.0.0.1:8770, only when enabled. It
  is never on the LAN, and the code refuses a non-loopback host.
- No shared secrets on devices. The core stores public keys only. Pairing
  codes are hashed, single-use, short-lived, and have a guess budget.
- Tiers are enforced on both sides. The fake device's refusal of an
  unconfirmed tier-2 call is tested with a deliberately confused core.
- The backup (`deploy/backup_state.py`) already picks up `nora_*.db` through
  the SQLite online-backup API, so `nora_core.db` is covered.

## Verification

- 739 tests pass, 3 runs in a row. There are also 4 skips, which are Chroma
  tests that skip in a checkout without `nora_cognitive_db`. The sum: 690
  before this work, plus 50 new (`test_store` 14, `test_hub` 36), minus 1
  JSON temp-file test that no longer applies.
- Tests no longer write the live store, audit log, session index or
  model-router log (`tests/conftest.py`).
- A live CLI run against a standalone hub: pair → pending, refused → approve
  → "what time is it" answered on the device → revoke → refused.

## Consequences & follow-ups

1. **`tailscale serve`** (not done; it needs the user's go-ahead on the
   tailnet): `tailscale serve --bg --https=8443 http://127.0.0.1:8770`, then
   set `hub.enabled: true`. Until then the hub is reachable only from this
   laptop.
2. Dashboard: pairing QR, list of pending devices, approve and revoke buttons
   (Phase 3).
3. Move `memory` facts into the store (migration 3).
4. Device turns run on the hub's event loop concurrently with the voice loop.
   Global turn state (`context` cancel flag, dashboard stage indicator) is
   shared, as it already was with Telegram.
5. A pre-existing test still creates `nora_cognitive_db/` in the repo root
   (not introduced here).
