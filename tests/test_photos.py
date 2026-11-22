"""Phase 7: a photo from the phone, over the socket, to the vision model."""
from __future__ import annotations

import asyncio
import base64
import time
from unittest import mock

from nora import channel, photos
from nora.commands import camera  # noqa: F401 - registers look_with_phone
from nora.hub import protocol
from nora.hub import server as hub_server

try:
    from tests.test_hub import HubTestCase
except ImportError:          # run from inside tests/
    from test_hub import HubTestCase

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 900       # ~230 KB, three chunks

CAMERA = {"name": "phone.take_photo", "version": 1, "tier": 1, "requires_live_user": True,
          "description": "Take one photo with the phone's camera",
          "params_schema": {"type": "object", "properties": {"prompt": {"type": "string", "maxLength": 80}}}}


def chunks(blob_id: str, data: bytes, mime: str = "image/jpeg") -> list[dict]:
    parts = [data[i:i + protocol.BLOB_CHUNK] for i in range(0, len(data), protocol.BLOB_CHUNK)]
    return [{"blob_id": blob_id, "seq": i, "mime": mime, "last": i == len(parts) - 1,
             "data": base64.b64encode(p).decode()} for i, p in enumerate(parts)]


class PhotoOverTheWire(HubTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        photos.reset_for_tests()
        self.addCleanup(photos.reset_for_tests)
        patch = mock.patch.object(hub_server, "_hub", self.hub)
        patch.start()
        self.addCleanup(patch.stop)

    async def camera_phone(self, data: bytes = JPEG):
        dev = await self.paired(capabilities=[CAMERA])

        def take(params: dict) -> dict:
            # The phone sends the photo ahead of the result that names it.
            blob_id = protocol.new_id()
            for body in chunks(blob_id, data):
                asyncio.get_running_loop().create_task(dev._send("blob.chunk", body))
            return {"success": True, "result": {"blob_id": blob_id, "message": "Photo taken."}}

        dev.replies["phone.take_photo"] = take
        return dev

    async def test_what_is_this_takes_a_photo_and_answers_untrusted(self) -> None:
        dev = await self.camera_phone()
        from nora.commands import camera
        token = channel.bind(channel.Channel(dev.device_id, "voice", print))
        try:
            with mock.patch.object(photos, "describe", return_value="A monstera plant.") as see:
                res = await camera.look_with_phone("what plant is this")
        finally:
            channel.unbind(token)
        self.assertTrue(res.success, res.message)
        self.assertEqual(res.message, "A monstera plant.")
        self.assertTrue(res.untrusted)
        photo = see.call_args.args[0]
        self.assertEqual(photo.data, JPEG)
        self.assertEqual(photo.device, dev.device_id)
        self.assertEqual(see.call_args.args[1], "what plant is this")

    async def test_a_job_cant_take_a_photo(self) -> None:
        dev = await self.camera_phone()
        res = await self.hub.call(dev.device_id, "phone.take_photo", {})
        self.assertEqual(res.error_code, protocol.POLICY_BLOCKED)
        self.assertEqual(dev.executed, [])

    async def test_the_camera_isnt_offered_to_the_model_directly(self) -> None:
        from nora import command_engine
        dev = await self.camera_phone()
        self.assertTrue(self.hub.offers(dev.device_id, "phone.take_photo"))
        self.assertIsNone(command_engine.get_action_meta("phone.take_photo"))
        self.assertIsNotNone(command_engine.get_action_meta("look_with_phone"))

    async def test_chunks_out_of_order_or_too_big_are_dropped(self) -> None:
        dev = await self.paired()
        bad = chunks("blob-0001", JPEG)
        await dev._send("blob.chunk", bad[1])                       # seq 1 first
        await dev._send("blob.chunk", {**chunks("blob-0002", b"x")[0], "mime": "text/html"})
        await asyncio.sleep(0.2)
        self.assertIsNone(photos.get("blob-0001"))
        self.assertIsNone(photos.get("blob-0002"))


def test_photos_are_dropped_after_keep_minutes():
    photos.reset_for_tests()
    p = photos.put("p1", "d", b"\xff\xd8x", "image/jpeg")
    assert photos.latest() is p
    with mock.patch("time.time", return_value=time.time() + 11 * 60):
        assert photos.latest() is None


def test_saving_writes_under_save_dir(tmp_path):
    photos.reset_for_tests()
    p = photos.put("p2", "d", b"\xff\xd8x", "image/jpeg")
    with mock.patch.object(photos, "_cfg", return_value={"keep_minutes": 10, "save_dir": str(tmp_path)}):
        path = photos.save(p)
    assert path.parent == tmp_path and path.read_bytes() == b"\xff\xd8x" and path.suffix == ".jpg"


def test_only_image_types_are_kept():
    import pytest
    with pytest.raises(ValueError):
        photos.put("p3", "d", b"<html>", "text/html")
