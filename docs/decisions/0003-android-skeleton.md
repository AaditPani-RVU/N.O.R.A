# ADR-0003: Android skeleton — pairing, link, kill switch, first capabilities (Phase 3)

- **Date:** 2026-09-30
- **Status:** implemented, verified on the Pixel 10 (Android 17, SDK 37)
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** new `android/`; changed `nora/hub/__main__.py`, `nora/fast_path.py`, `nora/dialogue.py`, `nora/commands/system_info.py`, `config.yaml`, `requirements.txt`, tests

## Context

Phase 3 of `NORA_DISTRIBUTED_PLAN.md`: the first real device. The exit
criteria were that "NORA, what's my phone battery?" works from the laptop, the
kill switch stops it, and the phone reconnects after airplane mode.

## Decision

- **App** (`android/`, package `com.aaditpani.nora`): Kotlin, Compose,
  `minSdk 35`, `targetSdk 37`. Sideloaded over adb.
  - `link/` holds protocol v1 (`Protocol`, `DeviceLink`, `InvocationGate`,
    `Capability`). It uses only OkHttp and `org.json`, with no Android APIs,
    so the JVM tests run it unchanged.
  - `phone/` holds the Android parts: `KeystoreSigner`, `PhoneDb` (audit log
    and outbox), `LinkService`, `KillTileService`, `BootReceiver`, and the
    capabilities.
- **Identity**: a P-256 key generated in Android Keystore, in StrongBox when
  the phone has it. It is never exported. Unpairing deletes the key, and
  pairing again creates a new identity.
- **Pairing**: `python -m nora.hub pair` prints a QR code of
  `{"nora":1,"url":…,"code":…,"exp":…}`, with the URL taken from the new
  `hub.public_url` setting. The app scans it with Google's code scanner, which
  needs no camera permission, or takes the URL and code typed in. The app
  accepts only `wss://` URLs, or plain `ws://` to loopback for tests.
- **Link**: a foreground service of type `specialUse` keeps the socket open.
  - Reconnects use exponential backoff from 1 s to 60 s with full jitter, and
    never wait less than 0.5 s.
  - When a network comes up, the service reconnects immediately instead of
    waiting out the backoff.
  - OkHttp sends WebSocket pings every 25 s.
  - After every handshake the app re-sends the kill state, because the core
    keeps its kill flag per session.
- **Device-side policy** (`InvocationGate`) makes the same checks as the fake
  device, in the same order:
  - a replayed invocation id returns the stored result (kept 10 minutes)
  - the kill switch refuses everything
  - a capability that isn't compiled in, or is switched off, is unavailable
  - past its deadline → expired
  - a live-user-only capability refuses any other origin
  - tier ≥ 2 needs this device's approval of this exact step
  - params are checked against the capability's schema
- **Confirmations are declined** in this version. No capability here is
  tier 2, and typed turns, which can ask to confirm a plan, arrive with the
  Phase 5 chat screen. A job-origin call (tier + 1) is therefore refused with
  `USER_DECLINED`, and that refusal is tested.
- **Kill switch** in three places: the notification action, a quick-settings
  tile, and a switch in the app. It takes effect on the phone first, then
  tells the core, which withdraws the capabilities.
- **Events**: `device.status` is sent when the charger is plugged in or
  unplugged. Events wait in the outbox (at most 500, kept for 24 h) until the
  core acks them.
- **Capabilities**:
  - `device.status` (tier 0): battery, charging, charge source, time to full,
    battery saver, network, ringer.
  - `phone.notify` (tier 0): posts a notification on the phone.
- **Routing on the core**: questions about the phone's battery, charge or
  ringer resolve to `device.status` in the fast path, but only while a
  connected device offers it. `get_system_info` now says it describes this
  laptop. Those phrasings take the action path. "jarvis" is stripped as a
  leading address, like "nora".

## Deviations from the plan

| Plan | Here | Why |
|---|---|---|
| Room for the outbox and audit log | `SQLiteOpenHelper` | Two small tables don't justify KSP and its version-matching with Kotlin |
| QR on the dashboard | QR from `python -m nora.hub pair` (terminal or image) | The dashboard has no pairing view yet. The store is shared, so one can be added without touching the hub |
| Confirmation UI in `policy/` | Confirmations are declined | Nothing in Phase 3 needs one; it comes with the first tier-2 capability or the chat screen |
| OkHttp (version unspecified) | OkHttp 4.12 | 5.x resolves to an Android variant that the JVM tests can't load |
| Voice session FGS type | `specialUse` link service | The link isn't audio. No FGS type fits "stay reachable by my own server" |

## Findings

- **Android 17's local-network permission doesn't apply** to the core's
  address. The phone reaches `*.ts.net` through the Tailscale VPN without
  `ACCESS_LOCAL_NETWORK` (the open question from plan §8).
- **The model preferred the laptop.** Asked "what's my phone battery looking
  like", it called `get_system_info`: the command had no description, and its
  output includes a battery line. Asked "how much charge does my phone have",
  the chat path answered "57 percent" without asking the phone. Both are
  fixed, and both have tests.
- **The wake word ended up in the transcript** ("Hey Jarvis What's my phone
  battery…"), which kept every rule from matching until "jarvis" was stripped.
- `nvidia/nemotron-3-nano-30b-a3b` was retired on 2026-09-01 and returns 410.
  It is still in the chat and reasoning fallback chains (not fixed here).

## Blast radius & reversibility

- Core changes are additive: a CLI output, a config key, one fast-path rule, a
  command description, and two regexes. To revert, check out the commit before
  this one.
- Revoking the phone takes effect live:
  `python -m nora.hub revoke d_a17c4584fea8`. Unpairing in the app deletes its
  key.

## Security & data impact

- The phone holds no shared secret. Its private key never leaves Keystore, and
  the core stores only the public key.
- The app won't pair over plain `ws://` except to loopback.
- The phone logs every invocation locally, independently of the core's
  `invocations` table.
- New outbound data from the phone: battery, charging state, network type and
  ringer mode, and only when asked or when the charger is plugged or unplugged.

## Verification

- **JVM, 15 tests**: protocol and ULIDs, the full `InvocationGate` table, and
  `HubIntegrationTest` against the real Python hub. That test covers pairing,
  staying pending until approved, the signed handshake, an invoke round trip
  with its audit row, a job-origin confirmation declined, the kill switch on
  both sides, the outbox acked after approval, and revocation.
- **Python**: 742 passed, 4 skipped.
- **On the Pixel 10, against the live core, 2026-09-29/30:**
  - paired over `wss://…ts.net:8443`, stayed pending, then connected on
    approval;
  - spoken "what's my phone battery looking like?" → "Phone battery is at 19%,
    battery saver on. On Wi-Fi. Ringer vibrate." (`device.status`, tier 0, ok);
  - kill switch ON from 00:07:16 to 00:09:39: nothing got through, and a
    request succeeded 8 s after it was turned off;
  - airplane mode: disconnected at 00:11:11, reconnected at 00:11:16;
  - after two core restarts, the phone reconnected within about 10 s each time.

## Consequences & follow-ups

1. Build environment: JDK 21, Android SDK 37 and Gradle 9.8 live under
   `~/Android` (no sudo). Build with
   `JAVA_HOME=~/Android/jdk ./gradlew :app:assembleDebug` from `android/`.
2. A pairing view on the dashboard (QR, pending devices, approve and revoke).
3. The Phase 2 test device `d_aa6832a4aa40` ("fake") is still approved on the
   live core.
4. Replace the retired NVIDIA fallback model.
5. The confirmation UI, when the first tier-2 capability arrives (Phase 4 or 8).
