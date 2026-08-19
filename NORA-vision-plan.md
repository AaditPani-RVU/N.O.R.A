# NORA — Vision Module (LLM + Computer Vision)

## Context

NORA is a local voice assistant (`AaditPani-RVU/N.O.R.A`, branch `linux`) that pipes voice → LLM intent parsing → guarded action execution. It has no real computer vision today: `nora/commands/screen_intelligence.py` only ships screenshots to Groq's hosted `llama-4-scout` vision model. There is no camera, no local inference, no persistent visual identity.

The goal is a genuine CV component so the project demonstrates **LLM + CV fusion**, not just LLM-with-an-image-API. The anchor feature: NORA sees a face, recognizes it against a persistent store, and greets that person **once per session** — "Hey Aadit." From there, who NORA is looking at becomes live context the LLM can reason over, and a gate on what it's willing to say out loud.

**Build order: core first.** Phase 1 ships a working camera + face memory. Phases 2–4 are scoped here but built after Phase 1 runs.

## What the codebase gives us for free

These existing patterns should be reused rather than reinvented:

- **`nora/ambient.py`** — the template for the whole thing. A daemon thread that samples a sensor in a loop, filters low-signal frames, and writes to a JSON store, started from `pipeline.run()` and gated by a `config.yaml` `enabled` flag. The camera loop is this with `cv2.VideoCapture` in place of `sounddevice`. Also exposes `log_entry()` for writing into the knowledge base.
- **`nora/context.py`** — thread-safe shared runtime state (`_lock` + module-level getters/setters, e.g. `MusicState` / `update_music()` / `get_music()`). Vision state belongs here so anything can read "who is present" without coupling.
- **`nora/command_engine.py`** — `@register(action_name, sig=, description=, risk=, requires_confirmation=, category=)`. `discover_commands()` auto-imports every module in `nora/commands/`, so a new file there is live with no wiring. `_OPTIONAL_CATEGORIES` controls the section header in the generated LLM prompt.
- **`nora/config.py`** — `get_config()` returns parsed `config.yaml`; follow the `ambient:` / `proactive:` block convention.
- **`nora/commands/screen_intelligence.py`** — reuse its Groq vision client (`_get_client()`, `_vision()`, `_VISION_MODEL`) for Phase 4 rather than opening a second client.

**Gotcha (verified):** handlers are invoked as `handler(**params)` at `command_engine.py:171`, so command functions take real keyword arguments (`def learn_face(name: str)`), **not** the `parameters: dict` shown in the README's outdated plugin example.

### Corrected — the manifest situation

An earlier draft of this plan claimed the repo has no `requirements.txt`. **It does** — ~25 deps at the repo root. It is invisible to git because `.gitignore:28` contains `/*.txt`, which over-matches and swallows it. That is the actual bug, and it is why a GitHub-API reading of this repo cannot see the file.

1. **Fix the ignore rule; do not create a second manifest.** Narrow `/*.txt` or add a `!requirements.txt` negation, then track the file.
2. **The existing manifest is stale for this branch.** It pins `pywin32`, `pyttsx3`, `keyboard`, `pyautogui` — Windows-era deps — while this is the `linux` branch. Reconcile it in the same pass that adds the vision deps.

**Trust note:** the original plan was written without a local clone (see Verification). Its claims about *tracked* source were spot-checked and held up; its claims about untracked or ignored files did not. Re-verify anything in that second category before acting on it.

---

## Phase 1 — Core: camera, face memory, greet-once

### New: `nora/vision/camera.py`

Single owner of the capture device — only one process can hold `/dev/video0`, and Phases 2–4 all need frames from the same stream.

- `start()` / `stop()`, `latest_frame()` returning the most recent BGR ndarray
- One `cv2.VideoCapture` in a daemon thread, downscaled (640×480) and throttled to the configured FPS
- Reference-counted open/close so `open_camera()` and the background perception loop can coexist
- **Explicit-off override.** Ref-counting alone is a privacy footgun: `close_camera()` must never report success while the perception loop still holds a ref and the device stays live. A user-issued close forces release and suspends the perception loop until an explicit reopen. Off means off, regardless of refcount.
- `is_active()` so camera state is always answerable — and never reports inactive while the device is held
- Fails soft: no camera present → log at debug and no-op, exactly like `ambient.start()` when disabled

### New: `nora/vision/faces.py`

Persistent face memory, using **insightface** (`buffalo_l`, ONNX runtime — pip-installable, no cmake/dlib toolchain).

- Store at `~/.nora/faces.json` — matches the `~/.nora/dbus_blessed.json` convention already used for learned D-Bus shortcuts
- Schema: `{"people": {"aadit": {"embeddings": [[512 floats], ...], "enrolled_at": ts, "trusted": true}}}`
- `enroll(name, frames)` — average several embeddings for robustness
- `identify(embedding) -> (name | None, score)` — cosine similarity, threshold from config (start ~0.5, tune on real captures)
- Same `_lock` + atomic-write discipline as `ambient.py`

### New: `nora/vision/perception.py`

The background loop — mirrors `_ambient_loop()`.

1. Pull a frame and detect faces at the configured FPS
2. **Embed only on demand.** Running the 512-d embedding on every face on every frame pins a GPU permanently in order to say "Hey Aadit" once. Detect at `fps`, but run the embed only for a face that is new or not yet confirmed, then hold identity by bounding-box continuity. Re-embed on track loss, not on a schedule.
3. Update `context` with who is currently present
4. **Greet on arrival, not once per process.** The original design used a module-level `_greeted_this_session: set[str]` that never resets while the process lives. That is wrong for a long-lived daemon: greeted at 09:00, step away, return at 18:00 → silence until restart. Instead greet on a *presence transition* — a person becomes eligible again once `last_seen[name]` is older than `regreet_after_seconds` (default 1800). `VisionState.last_seen` already carries the data, so this costs nothing extra. Setting `regreet_after_seconds: 0` restores strict once-per-process behavior if NORA turns out to be launched per-session rather than run as a daemon.
5. Debounce with N-consecutive-frame confirmation before declaring presence or absence, so a blink or a turned head doesn't retrigger greetings
6. Unknown face → stay silent, and **persist nothing** (see Privacy)

### Modified: `nora/context.py`

Add a `VisionState` alongside `MusicState`, same lock and getter/setter shape:

```python
@dataclass
class VisionState:
    present: list[str]        # recognized names currently visible
    unknown_count: int        # unrecognized faces in frame
    last_seen: dict[str, float]
    camera_active: bool
```

Plus `update_vision(**kwargs)` / `get_vision()`. This is the fusion point — the LLM prompt can then include "currently visible: Aadit" as ambient context.

### New: `nora/commands/vision_commands.py`

Auto-discovered. Registered actions, all `category="vision"`:

| Action | Risk | Purpose |
|---|---|---|
| `open_camera()` | low | Start capture + perception loop |
| `close_camera()` | low | Stop and release the device |
| `what_do_you_see()` | low | Describe the current frame |
| `who_is_there()` | low | Report recognized people |
| `learn_face(name)` | medium | Enroll — captures frames, speaks countdown |
| `forget_face(name)` | **high**, `requires_confirmation=True` | Delete a person from face memory |
| `list_known_faces()` | low | Everyone NORA has learned |

### Modified: `nora/command_engine.py`

Add `("vision", "Vision & Camera:")` to `_OPTIONAL_CATEGORIES` so the actions get their own section in the generated system prompt.

### Modified: `config.yaml`

```yaml
vision:
  enabled: false            # opt-in, like ambient
  camera_index: 0
  fps: 5                    # detection rate; embedding is event-driven, not per-frame
  face:
    enabled: true
    match_threshold: 0.5
    greet_on_first_sight: true
    regreet_after_seconds: 1800     # re-greet once absent this long; 0 = once per process
    offer_enrollment_for_unknown: false
    persist_unknown_embeddings: false   # keep false — see Privacy
  detector_device: "cuda"   # "cuda" | "cpu" — matches transcriber/embedder convention
```

Also add `forget_face` to `security.destructive_actions`.

### Modified: `nora/pipeline.py`

Add `vision.start()` next to the existing `ambient.start()` call at `pipeline.py:75`. No-ops when `vision.enabled` is false. Mirror the existing `ambient.stop()` teardown at `pipeline.py:247` and `pipeline.py:517` so the camera is released on shutdown paths, not just on the happy path.

### Privacy — build this in Phase 1, not later

A persistent store of face embeddings for anyone who walks past a webcam is the most sensitive thing this project will hold. Phase 2's guest mode gates what NORA *says*; nothing in the original plan gated what it *records*. Four rules, cheap now and painful to retrofit:

1. **Enrollment is always deliberate.** Embeddings are written only by an explicit `learn_face(name)`. `offer_enrollment_for_unknown` may prompt, but never enrolls without a spoken yes.
2. **Unknown faces leave no trace.** Match-and-discard. Nothing about an unrecognized person reaches disk — no embedding, no thumbnail, no `ambient.log_entry()` record. `persist_unknown_embeddings: false` is the default and there is no reason to flip it.
3. **Frames are never persisted.** The perception loop holds frames in memory only; only `ask_about_view()` (Phase 4) transmits an image anywhere, and only on an explicit request.
4. **Deletion is complete and reachable by voice.** `forget_face(name)` removes one person; add `forget_all_faces()` at the same **high / `requires_confirmation=True`** tier for a full wipe. Both must actually rewrite `faces.json`, not tombstone.

Add both `forget_face` and `forget_all_faces` to `security.destructive_actions` in `config.yaml:186` — `security.py:17` reads that list.

---

## Phase 2 — Per-person profiles + guest mode

Builds on the Phase 1 identity signal. When the visible person is not the enrolled owner (`trusted: true`), NORA enters guest mode: memory recall, email, and calendar actions decline to read private content aloud. Enforce in `nora/security.py` alongside the existing `is_blocked()` check, so it's one guard in the action path rather than scattered per-command checks — consistent with the NeuroSym action-guard design already in the project. Per-person greeting text and persona preference live in the `faces.json` record.

**Keep the asymmetry explicit.** A visible face is defeated by a photograph, so it is a valid signal for *restricting* behavior and never for *granting* it:

- **Allowed:** guest present → decline to read private content aloud, suppress notification previews, fall back to a neutral persona.
- **Forbidden:** owner visible → unlock anything, skip a confirmation, raise a risk ceiling, or authenticate.

Write this into the guard as a comment, not just into this document. The failure mode is someone later adding "auto-unlock when Aadit is seen" because nothing said not to — at which point a printed photo held to the webcam is a credential.

## Phase 3 — Presence-aware automation + gestures

**Presence:** absence beyond a configured threshold → lock screen, pause media via the existing MPRIS/D-Bus wrapper (`media_play_pause`), mute notifications. Return → restore, and report time away. Reuses F2/F5 command surface, adds no new system integration.

**Gestures:** mediapipe hand landmarks in the same perception loop. Thumbs-up/down becomes a silent second channel for `confirmation_flow()` (`pipeline.py:50`), which today only accepts voice. Open palm maps to the existing `STOP_PHRASES` behavior (`pipeline.py:27`); wave triggers wake without `Ctrl+\``.

**Gate gesture-confirm by risk.** The original plan pitched gesture confirmation as most valuable exactly where destructive actions demand confirmation. Invert that: a thumbs-up is a single noisy classifier output with no semantic content, and a false positive on a `requires_confirmation=True` action is unrecoverable. Accept gestures as confirmation for **low and medium risk only**; high-risk and destructive actions continue to require voice, which at least carries meaning that can be checked. A gesture may still *cancel* anything at any risk level — failing toward "didn't happen" is safe.

## Phase 4 — Show-and-tell

`ask_about_view(question)` — grab frame, optionally crop to the salient object, hand to the Groq vision model **through `screen_intelligence`'s existing `_vision()` helper** (`screen_intelligence.py:82`, client at `:42`, model at `:34`). This is the explicit local-CV → cloud-LLM handoff: local detection decides *what* to look at, the LLM decides *what it means*.

This is also the only path in the whole feature that transmits an image off the machine, so it stays explicitly user-initiated — never called from the perception loop, and never on a frame containing an unenrolled person.

---

## Dependencies

`opencv-python`, `insightface`, `onnxruntime` (or `onnxruntime-gpu`), `numpy` (already used). `mediapipe` only when Phase 3 lands. These go into the **existing** `requirements.txt` once the `/*.txt` ignore rule is fixed — see the manifest note above.

First run downloads the insightface `buffalo_l` pack (~300 MB) — warm it at startup like `cognitive_memory.warm_up()` does, so the first greeting isn't stalled by a model download.

**Verify the install story before committing to insightface.** The plan picked it to avoid the cmake/dlib toolchain, which is the right motivation, but `insightface` ships a C extension and its wheel coverage is inconsistent — on some Python versions pip falls back to a source build that needs a compiler anyway. Confirm a clean `pip install insightface` in a fresh venv on the target Python *before* the rest of Phase 1 is written around it. If it needs a build chain, the fallback worth pricing is running an ArcFace ONNX model directly on `onnxruntime` with a separate lightweight detector, which drops the dependency without changing the embedding math or any of the design above.

## Verification

This needs the repo cloned locally — it cannot be validated through the GitHub API. (The structural claims in this plan have since been spot-checked against a local clone; what remains below is runtime verification, which still requires real hardware.)

0. **Dependency smoke test first:** fresh venv, `pip install insightface onnxruntime opencv-python`. If this needs a compiler, resolve that before writing Phase 1 around it.
1. Clone the `linux` branch, install the vision deps into a venv.
2. **Camera standalone:** small script driving `nora/vision/camera.py` directly — confirm frames arrive and the device releases cleanly on `stop()`.
3. **Enrollment:** run `learn_face("aadit")` against a real webcam; confirm `~/.nora/faces.json` is written with the expected shape.
4. **Recognition:** restart the process, verify identification from the persisted store — this proves memory survives a session, the core requirement.
5. **Greeting cadence:** confirm the greeting fires once on arrival and does *not* repeat while you stay in frame or step out briefly. Then set `regreet_after_seconds` low (say 30), leave frame past the window, return, and confirm it *does* greet again. Restart and confirm it greets again.
6. **Negative cases:** no camera attached, camera busy in another app, unknown face in frame, two faces at once. None should crash the loop or block the voice pipeline.
7. **Privacy assertions:** with an unenrolled person in frame, confirm `faces.json` is unchanged and no frame or log entry hits disk. Run `forget_face` and `forget_all_faces` and confirm the file is actually rewritten.
8. **Camera off means off:** call `open_camera()`, let the perception loop run, then `close_camera()` — confirm the device is released (`fuser /dev/video0` or the camera LED) and `is_active()` reports false.
9. **Full pipeline:** `python main.py` with `vision.enabled: true`, then voice-drive `"open camera"` / `"who do you see"` / `"learn my face"` to confirm LLM intent parsing routes to the new actions.
10. Confirm the voice path still works normally with `vision.enabled: false` — no regression to existing behavior.

Nothing gets pushed until the webcam path runs end-to-end locally.
