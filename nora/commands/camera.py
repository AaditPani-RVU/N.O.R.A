"""Looking through the phone's camera (distributed plan Phase 7, spec example 3).

"What is this?" → the phone takes one photo (`phone.take_photo`, live user
only: never from a job or a rule), it comes over the socket, and the vision
model answers about it. The photo stays in memory for a few minutes for a
follow-up, then it's gone (`nora.photos`); "save that photo" keeps it.
"""
from __future__ import annotations

import asyncio
import logging

from nora import channel as _channel
from nora import photos
from nora.command_engine import register
from nora.hub import protocol
from nora.schemas import StepResult

logger = logging.getLogger("nora.commands.camera")

CAPABILITY = "phone.take_photo"


def _camera_device() -> str:
    """The device to take it with: the one the turn came from if it has a
    camera, else any connected one that does."""
    from nora.hub import server
    hub = server.get()
    if hub is None:
        return ""
    ch = _channel.current()
    if ch is not None and hub.offers(ch.device_id, CAPABILITY):
        return ch.device_id
    return next((d for d in hub.sessions() if hub.offers(d, CAPABILITY)), "")


async def _answer(action: str, photo: photos.Photo, question: str) -> StepResult:
    try:
        text = await asyncio.get_running_loop().run_in_executor(None, photos.describe, photo, question)
    except Exception as e:
        logger.warning("vision on a photo failed: %s", e)
        return StepResult(action=action, success=False,
                          message="I got the photo but couldn't make it out; the vision model didn't answer.")
    # What's in a picture is someone else's words as much as a notification is.
    return StepResult(action=action, success=True, message=text, untrusted=True,
                      data={"photo_id": photo.id})


@register(
    "look_with_phone",
    sig="look_with_phone(question='')",
    description="Take a photo with the user's phone camera and answer about it "
                "(\"what is this\", \"read this label\", \"what plant is this\")",
    category="vision",
)
async def look_with_phone(question: str = "") -> StepResult:
    from nora.hub import server
    device = _camera_device()
    hub = server.get()
    if not device or hub is None:
        return StepResult(action="look_with_phone", success=False, error_code=protocol.DEVICE_OFFLINE,
                          message="Your phone isn't connected, so I can't look through its camera.")
    res = await hub.call(device, CAPABILITY, {"prompt": question.strip()[:80]},
                         channel=_channel.current())
    if not res.success:
        return StepResult(action="look_with_phone", success=False, error_code=res.error_code,
                          withheld=res.withheld, message=res.message)
    photo = photos.get(str(res.data.get("blob_id", "")))
    if photo is None:
        return StepResult(action="look_with_phone", success=False,
                          message="The phone took the photo but it didn't arrive. Try again?")
    return await _answer("look_with_phone", photo, question)


@register(
    "ask_about_photo",
    sig="ask_about_photo(question)",
    description="Another question about the photo the phone just took (\"what colour is it\", \"how much does it say\")",
    category="vision",
)
async def ask_about_photo(question: str = "") -> StepResult:
    photo = photos.latest()
    if photo is None:
        return StepResult(action="ask_about_photo", success=False,
                          message="I don't have a recent photo. Ask me to look at something first.")
    return await _answer("ask_about_photo", photo, question)


@register(
    "save_photo",
    sig="save_photo()",
    description="Keep the photo the phone just took for NORA (otherwise it's dropped after a few minutes)",
    category="vision",
)
def save_photo() -> str:
    photo = photos.latest()
    if photo is None:
        return "There's no recent photo to save."
    if photo.saved:
        return f"Already saved, at {photo.saved}."
    path = photos.save(photo)
    return f"Saved it to {path}."
