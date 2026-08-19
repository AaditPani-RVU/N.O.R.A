"""Vision & camera voice commands — Phase 1 of the vision module.

    open_camera()        — start capture + perception loop
    close_camera()       — release the device; off means off
    what_do_you_see()    — describe the current frame (locally, no upload)
    who_is_there()       — report recognized people
    learn_face(name)     — enroll, with a spoken countdown
    forget_face(name)    — delete one person from face memory
    forget_all_faces()   — wipe face memory entirely
    list_known_faces()   — everyone NORA has learned

Privacy contract enforced here (see NORA-vision-plan.md):
  * enroll() is reachable only through learn_face — deliberate, spoken, named
  * unknown faces are matched and discarded; nothing about them hits disk
  * no frame is ever written to disk or transmitted by any command in this file
  * forget_face / forget_all_faces genuinely rewrite ~/.nora/faces.json
"""
from __future__ import annotations

import logging
import time

from nora.command_engine import register
from nora.vision import camera, faces, perception
import nora.speaker as speaker

logger = logging.getLogger("nora.commands.vision_commands")

_ENROLL_SAMPLES = 5          # embeddings averaged over for a robust enrollment
# Budget against timeouts.command_sec (15s): ~1s model load if cold in-process,
# ~3s spoken countdown, then this. Five samples need only ~1s, so 6s is slack
# for the person to settle, not a target.
_ENROLL_TIMEOUT = 6.0


def _unavailable() -> str | None:
    """Human-readable reason vision can't run right now, or None if it can."""
    try:
        import cv2  # noqa: F401
    except Exception:
        return "OpenCV isn't installed, so I can't use the camera."
    if not faces.is_available():
        return "My face recognition model isn't loaded, so I can't see faces right now."
    return None


def _describe(names: list[str], unknown: int) -> str:
    if not names and not unknown:
        return "Nobody."
    parts = []
    if names:
        parts.append(", ".join(faces.display_name(n) for n in names))
    if unknown == 1:
        parts.append("one person I don't recognize")
    elif unknown > 1:
        parts.append(f"{unknown} people I don't recognize")
    return " and ".join(parts)


# ── Camera control ───────────────────────────────────────────────────────────

@register(
    "open_camera",
    sig="open_camera()",
    description="Turn the camera on and start watching for faces",
    risk="low",
    category="vision",
)
def open_camera() -> str:
    reason = _unavailable()
    if reason:
        return reason

    camera.resume()          # clear any explicit-off latch
    if not camera.acquire("voice"):
        return "I couldn't open the camera — it may be missing or in use by another app."

    perception.start()
    return "Camera on. I'm watching."


@register(
    "close_camera",
    sig="close_camera()",
    description="Turn the camera off and release the device",
    risk="low",
    category="vision",
)
def close_camera() -> str:
    # Explicit off overrides the refcount: the perception loop is stopped and
    # the device forcibly released, so "camera off" is never a polite fiction
    # told while the LED is still on.
    perception.suspend_for_explicit_off()
    camera.force_off()

    from nora import context
    context.update_vision(present=[], unknown_count=0, camera_active=camera.is_active())

    if camera.is_active():
        return "I asked the camera to close but the device is still held. Something's wrong."
    return "Camera off."


# ── Seeing ───────────────────────────────────────────────────────────────────

@register(
    "what_do_you_see",
    sig="what_do_you_see()",
    description="Describe who and what is in front of the camera right now",
    risk="low",
    category="vision",
)
def what_do_you_see() -> str:
    reason = _unavailable()
    if reason:
        return reason
    if camera.is_suspended():
        return "The camera is off. Say 'open camera' first."

    frame = camera.grab("what_do_you_see")
    if frame is None:
        return "I can't get a picture from the camera right now."

    # Detection and matching happen entirely on this machine. No frame leaves
    # the device on this path — that is Phase 4's ask_about_view, by request only.
    detections = faces.detect(frame)
    if not detections:
        return "I don't see anyone in front of the camera."

    known: list[str] = []
    unknown = 0
    for det in detections:
        name, _ = faces.identify(faces.embed(frame, det))
        if name:
            known.append(name)
        else:
            unknown += 1

    if not known:
        count = "one face" if unknown == 1 else f"{unknown} faces"
        return f"I see {count}, but nobody I recognize."
    return f"I see {_describe(sorted(set(known)), unknown)}."


@register(
    "who_is_there",
    sig="who_is_there()",
    description="Report which people NORA currently recognizes on camera",
    risk="low",
    category="vision",
)
def who_is_there() -> str:
    from nora import context

    state = context.get_vision()
    if state["present"] or state["unknown_count"]:
        return f"I can see {_describe(state['present'], state['unknown_count'])}."

    if not state["camera_active"]:
        # Perception isn't running — take a one-shot look rather than say "nobody".
        return what_do_you_see()
    return "I don't see anyone right now."


# ── Face memory ──────────────────────────────────────────────────────────────

@register(
    "learn_face",
    sig="learn_face(name)",
    description="Learn a person's face so NORA recognizes them later",
    risk="medium",
    category="vision",
)
def learn_face(name: str) -> str:
    if not name or not name.strip():
        return "I need a name to go with the face."

    reason = _unavailable()
    if reason:
        return reason

    camera.resume()
    if not camera.acquire("learn_face"):
        return "I couldn't open the camera to learn a face."

    try:
        speaker.speak(f"Look at the camera, {name.strip()}. Three, two, one.")

        embeddings = []
        deadline = time.time() + _ENROLL_TIMEOUT
        while len(embeddings) < _ENROLL_SAMPLES and time.time() < deadline:
            frame = camera.latest_frame()
            if frame is None:
                time.sleep(0.15)
                continue

            detections = faces.detect(frame)
            if len(detections) != 1:
                # Enrolling with two faces in frame risks binding the wrong one.
                time.sleep(0.2)
                continue

            embedding = faces.embed(frame, detections[0])
            if embedding is not None:
                embeddings.append(embedding)
            time.sleep(0.2)

        if not embeddings:
            return "I couldn't get a clear look at a single face. Try again facing the camera."

        total = faces.enroll(name, embeddings)
        logger.info("Enrolled %s from %d samples (%d stored)", name, len(embeddings), total)
        return f"Got it. I'll recognize {name.strip()} from now on."
    finally:
        camera.release("learn_face")


@register(
    "forget_face",
    sig="forget_face(name)",
    description="Permanently delete a person from NORA's face memory",
    risk="high",
    requires_confirmation=True,
    category="vision",
)
def forget_face(name: str) -> str:
    if not name or not name.strip():
        return "Tell me whose face to forget."
    if faces.forget(name):
        return f"I've forgotten {name.strip()}'s face."
    return f"I don't have a face stored for {name.strip()}."


@register(
    "forget_all_faces",
    sig="forget_all_faces()",
    description="Permanently delete every face NORA has learned",
    risk="high",
    requires_confirmation=True,
    category="vision",
)
def forget_all_faces() -> str:
    removed = faces.forget_all()
    if not removed:
        return "There were no faces to forget."
    people = "person" if removed == 1 else "people"
    return f"Face memory wiped. I've forgotten {removed} {people}."


@register(
    "list_known_faces",
    sig="list_known_faces()",
    description="List everyone whose face NORA has learned",
    risk="low",
    category="vision",
)
def list_known_faces() -> str:
    people = faces.list_people()
    if not people:
        return "I haven't learned anyone's face yet. Say 'learn my face' to start."
    names = ", ".join(p["name"] for p in people)
    if len(people) == 1:
        return f"I know one face: {names}."
    return f"I know {len(people)} faces: {names}."
