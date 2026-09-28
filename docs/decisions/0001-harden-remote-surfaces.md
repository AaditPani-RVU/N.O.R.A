# ADR-0001: Harden NORA's remote surfaces (Phase 0)

- **Date:** 2026-09-17
- **Status:** implemented (uncommitted, branch `fix/phase0-remote-auth`)
- **Author:** Aadit Pani (with Warden / Claude Code)
- **Scope:** `nora/security.py`, `nora/ui_server.py`, `nora/remote_mic.py`, `nora/static/index.html`, `nora_remote.py`, `nora/autonomy.py`, `nora/commands/notifications.py`, `nora/commands/google_services.py`, `config.yaml`, `README.md`, `tests/test_remote_security.py`

## Context

The audit for `NORA_DISTRIBUTED_PLAN.md` found six holes in the servers that let
other devices reach NORA. None were known to be exploited, and NORA was not
running when they were fixed.

| # | Before |
|---|---|
| S1 | `remote_mic` (enabled, `0.0.0.0:8767`) turned `POST /audio` into commands with no authentication |
| S2 | `send_whatsapp`, `send_email`, `delete_calendar_event` ran without confirmation (default `risk="low"`) |
| S3 | An unset `NORA_API_TOKEN` passed every token check, while the servers listened on `0.0.0.0` |
| S4 | Tokens compared with `==` / `!=` |
| S5 | Any LAN peer could connect to all three servers |
| S6 | On the dashboard, only `/desktop` and `/desktop_act` checked the token. `/type_command`, `/proc_kill`, `/ptt`, `/music_ctl`, `/interrupt` and the data GETs (`/state`, `/history`, `/processes`, `/memory_graph`, …) were open, and `Access-Control-Allow-Origin: *` plus an allow-all preflight let any web page in a local browser call them |

## Decision

- **Network filter:** new `security.peer_allowed()` with a configurable
  `security.remote_networks` (default: loopback, `100.64.0.0/10`,
  `fd7a:115c:a1e0::/48`). Applied first on all three servers; others get `403`,
  or the WebSocket is dropped.
- **Token everywhere:** every dashboard route except `/`, `/index.html` and the
  three static scripts needs the token (query or bearer). `remote_mic` needs a
  bearer token on both routes.
- **Fail closed:** `check_api_token(provided, peer=, proxied=)`. With no token
  set, only a direct loopback connection passes. Requests carrying proxy headers
  (`X-Forwarded-For`, `Forwarded`, `Tailscale-User-Login`) never get loopback
  trust, so a future `tailscale serve` can't open it up.
- **Constant-time comparison** with `hmac.compare_digest`. Non-string WebSocket
  tokens are rejected instead of raising.
- **No CORS:** all `Access-Control-Allow-*` headers are removed. The dashboard is same-origin.
- **Dashboard:** one `fetch` wrapper adds `Authorization: Bearer` to same-origin calls.
- **remote_mic body cap:** 60 s of audio (3.84 MB); larger claims get `413` unread.
- **Confirmation:** the three outbound/destructive actions are `risk="high"`,
  `requires_confirmation=True`, in `security.destructive_actions`, and in
  `autonomy._HARD_CONFIRM` (learned consent can never skip them).
- **Client:** `nora_remote.py` sends `NORA_API_TOKEN` from its own `.env`.

## Alternatives considered

- **Bind to `127.0.0.1` and publish via `tailscale serve`** (the plan's original
  S5). This is the end state, but it would break the phone's current
  `http://<tailscale-ip>:8766` URL now, and it needs HTTPS certs enabled in the
  Tailscale admin console. Deferred to Phase 2, where the device protocol needs
  TLS anyway.
- **Bind to the Tailscale IP only.** Fails if tailscaled comes up after NORA at
  boot (EADDRNOTAVAIL), so NORA would need retry logic. Filtering by peer address
  has no boot-order dependency.
- **Disable `remote_mic`.** It's slated for replacement by the Phase 6 voice
  stream, but it's documented as working today. Securing it keeps the workflow
  while closing the hole.
- **A host firewall (nftables).** Stronger, but it needs root and lives outside
  the repo. Worth adding with Phase 0b; the in-app filter protects even without it.

## Blast radius & reversibility

- Any client that relied on unauthenticated access now gets `401`/`403`. Known clients:
  - The dashboard: fixed via the wrapper; the launch URL already carries the token.
  - `nora_remote.py`: sends the token now.
  - The phone browser: works if opened with `?token=` once, as the README already said.
- LAN access without Tailscale stops working. To allow it, add the subnet to
  `security.remote_networks`.
- Telegram requests for the three actions are now refused with "confirm in the
  room". This is intended until on-device confirmation exists (Phase 2).
- Fully reversible by reverting the branch. No data or state migration.

## Security & data impact

Closes remote command injection (S1, S6), remote process kill (S6), leakage of
memory/history/process data (S6), drive-by requests from web pages (S6), and
messages sent in the user's name without confirmation (S2). No secrets were
added. The token still appears in dashboard URLs (`?token=`), as before.

## Verification

- `tests/test_remote_security.py`: 31 tests over real sockets for the dashboard
  routes, `remote_mic` and the WebSocket, plus unit tests for the token and peer
  checks and the confirmation metadata.
- Run against the pre-fix code (source changes stashed): **26 failed, 5 passed**.
  The 5 cover behaviour that should hold both before and after.
- Full suite with the fix: **639 passed** (608 before + 31 new).
- Dashboard JS: `node --check` on the inline scripts passed. The fetch wrapper
  was run in Node: bearer added to `/state` and `/type_command`, existing
  `Content-Type` kept, absolute and `//` URLs untouched, no header without a token.
- **Not verified:** a live NORA run with the dashboard open on the phone over
  Tailscale. Do this after restarting NORA: open the phone URL, send a typed
  command, check that music controls and the process constellation work.

## Consequences & follow-ups

- Phase 2: move to `127.0.0.1` + `tailscale serve` (TLS) and per-device keys; revisit `?token=` in URLs.
- Phase 0b: consider an nftables rule limiting 8765–8767 to `tailscale0` + `lo`.
- `/desktop_act` still checks the token itself after the global check. That's
  redundant but harmless, so it was left as-is.
