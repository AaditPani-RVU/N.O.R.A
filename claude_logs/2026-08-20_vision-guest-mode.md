# Vision Guest-Mode — Development Log

- **Written:** 2026-08-20 11:16:24
- **Source:** ask_claude:sonnet
- **Request:** Create a log for the vision guest-mode change (guest-mode privacy guard, presence module, security/pipeline/web_search updates).

---
**Date:** 2026-08-20
**Area:** `nora/vision/*`, `nora/security.py`, `nora/command_engine.py`, `nora/pipeline.py`, `nora/proactive.py`, `nora/conversation.py`, `nora/intent_parser.py`, `config.yaml`
**Status:** Uncommitted working-tree changes (Phase 2 of the vision plan)

## Summary

Phase 2 of NORA's vision system turns raw face presence into a privacy guard: when someone who isn't the owner is in front of the camera, actions that would read private content aloud (memory recall, email, calendar, screen contents) decline instead of running. A new module, `nora/vision/presence.py`, is the single place that interprets "who's in frame" into "is a guest here," and `nora/security.py` wires that interpretation into the action-execution path.

## New module: `nora/vision/presence.py`

Sits between the perception loop (which only tracks bounding boxes and identities) and everything that needs to act on presence. Its job is split cleanly from `nora/security.py`: presence.py *derives* the answer (cheap, side-effect free), security.py *enforces* it.

Public surface:
- `enabled()` — guest mode only runs when `vision.enabled` and `vision.guest_mode.enabled` are both true.
- `snapshot()` — resolves everything once: who's present, unknown-face count, the owner key, `owner_present`, `known_guests`, `guest_present`, `camera_active`.
- `guest_present()` / `guest_mode_active()` — the two questions callers actually ask; both fail closed (`False`) if vision throws, so a broken camera never blocks a command it shouldn't.
- `describe_guests()` — turns the snapshot into a spoken phrase ("Sam, and someone I don't recognize") for decline messages.
- `format_for_prompt()` — the vision → LLM handoff. Injects a `PRESENCE` block into prompts naming who's visible, tagging the owner, surfacing per-person style hints (only when exactly one *recognized* person is alone in frame — a second face makes "whose preference applies" ambiguous), and appending the `GUEST MODE` instruction block when a non-owner is present.

**The core design rule, stated explicitly in the module docstring and mirrored in `security.py`:** a face is a weak signal (defeated by a printed photo), so it may only ever *restrict* behavior, never grant it.
```
allowed:   guest present  → decline to read private things aloud
forbidden: owner present  → unlock, authenticate, skip a confirmation
```
Nothing anywhere returns "owner recognized, therefore proceed." The only branch that exists is "someone else is here, therefore be quieter."

**No-owner edge case:** if nobody has been designated owner (`vision.face.owner` empty, nobody marked trusted), only *unrecognized* faces count as guests. Enrolling a face is not the same as declaring an owner — otherwise a fresh enrollment would immediately lock the enroller out of their own memory recall. `set_owner` (voice: "you're the owner") or `vision.face.owner` in config is what actually turns on the your-face-vs-everyone-else distinction.

## `nora/vision/faces.py` — ownership and profiles

Added the identity layer presence.py depends on:
- `owner()` — resolves the owner key, with `vision.face.owner` in config outranking the stored `trusted` flag (a config value only the user edits is a stronger statement than one set by voice, and survives a face-store wipe).
- `is_trusted(key)` — `True` iff `key == owner()`. Docstring explicitly forbids using this to *unlock* anything.
- `set_owner(name)` — exclusive: promoting one person demotes any previously trusted record in the same write, so `owner()` never has two answers.
- `get_profile` / `set_profile` — per-person greeting text and persona hints (tone/verbosity/style). `set_profile` deliberately has no `trusted` parameter — ownership only changes through `set_owner`, keeping exclusivity enforcement in one place.

## `nora/security.py` — the enforcement guard

New "Guest mode (vision Phase 2)" section, placed next to the existing `is_blocked()` check:
- `_DEFAULT_GUEST_RESTRICTED` — fallback action list used when `vision.guest_mode.restricted_actions` is absent from config: `recall`, `semantic_recall`, `show_patterns`, `memory_status`, `inject_knowledge`, `check_email`, `send_email`, `check_calendar`, `add_calendar_event`, `delete_calendar_event`, `read_screen`, `set_owner`.
- `is_guest_restricted(action)` — checks the explicit action list, then falls back to `vision.guest_mode.restricted_categories` matched against `command_engine.get_action_meta(action).category`, so a whole command category (e.g. `memory`) can be withheld without enumerating every action.
- `guest_mode_active()` — thin wrapper over `presence.guest_mode_active()`; catches exceptions so vision being unavailable never turns into a hard failure elsewhere.
- `guest_blocks(action)` = `is_guest_restricted(action) and guest_mode_active()` — the actual guard predicate.
- `guest_decline_message(action)` — reads `vision.guest_mode.decline` from config (or the default line), substitutes `{who}` with `presence.describe_guests()` if the template uses it.

A long comment block above `_DEFAULT_GUEST_RESTRICTED` states the asymmetry rule again as a warning to future editors: never add "auto-unlock when the owner is seen" here — that would turn a photo held to the webcam into a credential.

## `nora/command_engine.py` — wiring into execution

`execute()` now checks `guest_blocks(action)` immediately after the existing `is_blocked(action)` check, before dispatching to the handler. A blocked step never reaches its handler — no private data is even computed before being withheld. On a hit it appends a `StepResult(success=False, withheld=True, message=<decline>)` and stops the step list (same short-circuit behavior as a hard block).

`StepResult` gained a `withheld: bool = False` field (`nora/schemas.py`) to distinguish "NORA chose not to run this" from an actual failure.

## `nora/pipeline.py` — speaking a decline correctly

`summarize_results()` now treats `r.withheld` the same as `r.success` when deciding whether to speak the result as a plain message vs. prefix it with "Failed:". Without this, a deliberate guest-mode refusal would have been read aloud as an error.

## `nora/proactive.py` — suggestions go quiet too

`_can_suggest()` now checks `security.guest_mode_active()` first and returns `False` if a guest is present — proactive suggestions narrate the owner's habits out loud, which is exactly the kind of thing guest mode exists to suppress, even though "suggest something" isn't a discrete restrictable action.

## `nora/conversation.py` / `nora/intent_parser.py` — prompt fusion

Both now call `presence.format_for_prompt()` and append the returned block to the LLM prompt when non-empty (`conversation.py:_presence_block()`, `intent_parser.py` inline). This is how the model itself learns to talk around a guest ("I'll keep that private") rather than relying solely on the hard action-level block — the guard in `security.py` is the backstop; the prompt block is what makes the refusal sound natural instead of like a permission error.

## `nora/commands/vision_commands.py` — user-facing status

New `guest_mode_status()` command (category `vision`, low risk) answers "am I in guest mode right now" — three branches: guest mode off entirely, guest mode on and active (names who triggered it via `describe_guests()`), or on standby with no owner set / owner alone.

## `config.yaml` — new `vision.guest_mode` block

```yaml
vision:
  guest_mode:
    enabled: true
    decline: "Someone I don't recognize is here, so I'll keep that private. Ask me again when you're alone."
    restricted_actions: [recall, semantic_recall, show_patterns, memory_status,
                          inject_knowledge, check_email, send_email, check_calendar,
                          add_calendar_event, delete_calendar_event, read_screen, set_owner]
    restricted_categories: []
```
Heavily commented in place, restating the asymmetry rule a third time (module docstring, security.py comment, config comment) — deliberate, since this is the property most likely to get silently broken by a future edit.

## Tests: `tests/test_vision.py`

Substantial new coverage, all stdlib `unittest`, no camera/ONNX required (detection/embedding stubbed):
- **`ProfileTest`** — owner exclusivity (promoting demotes the prior owner), rejecting an unenrolled name, config-owner outranking the stored flag, greeting/persona defaults and overrides, profiles surviving re-enrollment but not `forget()`, `set_profile` rejecting a `trusted` kwarg (`TypeError`).
- **`GuestPresenceTest`** — unknown face is a guest even with no owner set; an enrolled-but-not-designated person is *not* a guest until an owner exists; non-owner becomes a guest once an owner is designated; owner alone is never a guest; empty frame isn't a guest; `vision.enabled=false` never restricts regardless of who's in frame; `guest_mode.enabled=false` still resolves presence but doesn't activate the guard.
- **`GuestGuardTest`** — private actions withheld with a guest present, nothing withheld for the owner alone, harmless actions (`get_time`, `play_music`) unaffected; confirms guest mode never loosens an existing guard (`needs_confirmation`, `is_blocked` unaffected by owner presence); `restricted_categories` extends the block list by category; `{who}` substitution in the decline message; end-to-end `command_engine.execute()` test proving the handler never runs and no private data leaks into the returned message when withheld; matching test that the owner's step *does* run and succeed.
- **`PresencePromptTest`** — empty block when nobody's visible; owner tagged `(owner)`; `GUEST MODE` instruction present/absent correctly; persona hint suppressed the moment a second face (known or unknown) enters frame.

## `nora/commands/web_search.py` + `tests/test_web_search.py` — unrelated, bundled in the same diff

Worth flagging as a separate concern: `web_search.py` has no guest-mode hooks and none of its actions (`web_search`, `open_url`, `tell_me_about`) appear in `guest_mode.restricted_actions`. Its changes are a distinct tiered-research feature shipped in the same working tree:
- Three-tier fallback in `tell_me_about()`: live search (groq/compound-mini role that does its own web search) → Brave/DuckDuckGo snippet summarization → model-only with an explicit "I don't have real-time data" admission.
- `_is_fresh_query()` regex gates a Brave `freshness` param so time-sensitive queries ("current fuel prices") don't return stale top results.
- Bug fix: Brave results with empty `extra_snippets: []` used to raise `IndexError` and silently drop the result — now falls back to `description`.
- Every prompt tier explicitly forbids emitting URLs/domains/markdown, since results are spoken, not displayed; source attribution is phrased for TTS ("according to the Guardian").
- Test file covers freshness detection, snippet parsing/dedup, source extraction from `executed_tools`, and the tier fall-through order — all stubbed, no network calls.

## Net effect

A visible non-owner face now causes: private commands to decline before their handler runs, proactive suggestions to pause, and the LLM's own prompt to carry an explicit instruction to stay neutral — three independent layers enforcing the same one-directional rule, with the "never grants, only restricts" invariant documented at every layer to keep it that way under future edits.
