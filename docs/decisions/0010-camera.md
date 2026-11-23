# ADR-0010: Looking through the phone's camera (Phase 7)

- **Date:** 2026-11-23
- **Status:** implemented; **not yet verified on the Pixel**
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** core `nora/photos.py`, `nora/commands/camera.py` (new), `nora/commands/screen_intelligence.py`, `nora/hub/{server,protocol}.py`, `nora/fast_tables.py`, `config.yaml`; Android `phone/Camera.kt` (new), `link/DeviceLink.kt`, `phone/NoraApp.kt`, manifest, `res/xml/photo_paths.xml`; tests

## Context

Spec example 3: point the phone at something and ask "what is this?". Phase 7
lists a capture activity, blob upload, vision analysis and a retention policy.

## Decision

- **Capture with the phone's own camera app**, through an invisible activity
  and `ActivityResultContracts.TakePicture` into a FileProvider file in the
  app's cache. No CameraX, no CAMERA permission (declaring it would make the
  system camera intent refuse without it), and the user lines up the shot in
  the camera they know.
- **Live user only** (`requires_live_user`): no job, rule or notification can
  take a photo. `phone.take_photo` is kept out of the model's tools; the way in
  is `look_with_phone(question)`, which also asks the vision model, and
  `ask_about_photo` / `save_photo` for follow-ups. "What is this", "what's this
  plant", "read this label", "take a photo and tell me…" are fast-path, and only
  when said **to the phone** (said to the laptop's microphone there's no camera
  in hand). Bare "what's that" is left alone: it's too often about something
  just said.
- Android opens the camera only from the screen, so with NORA not visible the
  phone says so (`BACKGROUND_RESTRICTED`) instead of failing silently.
- **Transport: `blob.chunk` frames on the authenticated socket**, ahead of the
  result that names the blob, rather than the plan's `PUT /v1/blobs`: the hub is
  a websockets server with no HTTP body handling, and one socket already has
  the device's authentication and ordering. Chunks are 96 KB decoded, a blob at
  most 8 MB, at most four in flight per session; out of order, oversized or not
  an image type drops it. The phone shrinks the photo to 1600 px JPEG first
  (ImageDecoder applies the EXIF rotation) and deletes its copy.
- **Vision:** Groq's `qwen/qwen3.8-27b` (`qwen3.6-27b` until Groq retired it on 2026-11-23), the model `read_screen` already uses,
  with a camera prompt. Its answer is marked **untrusted**: a sign, a letter or
  a screen in the picture can say anything.
- **Retention:** in memory for 10 minutes for follow-ups, at most ten photos,
  then dropped. Only `save_photo` writes one to disk, under `~/Pictures/NORA`.

## On-phone checklist

1. Install the APK. Open NORA, say or type "what is this?" with something in
   front of the phone: the camera opens, take the shot, NORA answers.
2. "What colour is it?" answers from the same photo; "save that photo" puts it
   in `~/Pictures/NORA` on the core.
3. With NORA in the background, a typed "what is this" from the laptop says
   the camera needs NORA on screen.
