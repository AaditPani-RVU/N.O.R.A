# ADR-0011: The optional accessibility service, named recipes only (Phase 9)

- **Date:** 2026-11-23
- **Status:** implemented; **not yet verified on the Pixel**
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** Android `phone/UiAutomation.kt` (new), `link/{PhoneLogic,Capability,Chat}.kt`, `phone/{NoraApp,Capabilities}.kt`, `ui/MainActivity.kt`, manifest, `res/xml/accessibility.xml`, `res/values/strings.xml`; core `config.yaml`, `nora/evals/`; tests

## Context

Phase 9: a disabled-by-default accessibility service, allowlisted apps, named
recipes only, tier ≥ 2. Exit: three recipes work and the service can't run
anything outside them.

## Decision

Three recipes, each its own capability so each has its own tier:

| Capability | Does | Tier |
|---|---|---|
| `phone.ui.whatsapp_send` | Opens the chat with the message (`wa.me`) and presses Send | 3, live user |
| `phone.ui.whatsapp_call` | Opens the chat and presses Voice call (and "Start voice call?" if shown) | 2, live user |
| `phone.ui.maps_start` | Presses Start on the directions Maps shows after `navigate_to` | 2 |

What keeps it to those:

- The service's config lists **only WhatsApp and Maps** (`packageNames`), so
  the system shows it no other app's windows. It asks for no gestures
  (`canPerformGestures="false"`) and ignores every event.
- A press is a match against the recipe's own target (resource ids, content
  descriptions, labels) in the recipe's own package; `UiMatch` skips nodes of
  any other package, even nested inside an allowed window. There is no tap(x, y),
  no "press whatever says X" capability, and nothing read off the screen goes
  back to the core.
- A recipe that can't find its button says so; it never falls back to
  something else.
- Recipes report `available: false` while the service is off, so the core
  leaves them out of the model's tools; the phone resends its manifest when the
  service connects or disconnects. The core floors their tiers in config.

## Consequences

- An update to WhatsApp or Maps can move a button. The recipe then fails with
  a sentence, and the target lists in `UiRecipes` are where to fix it.
- Advanced Protection Mode would revoke the service (plan §8); nothing else
  depends on it.

## On-phone checklist

1. NORA → System → "PRESS BUTTONS IN WHATSAPP + MAPS" → switch on "NORA buttons".
2. "Send a WhatsApp to <name> saying testing, and send it" → fingerprint → sent.
3. "WhatsApp call <name>" → approve → it rings.
4. "Directions to college", then "start navigation" → approve → Maps starts.
5. Switch the service off: the recipes leave NORA's list (asking for one says
   it isn't available).
