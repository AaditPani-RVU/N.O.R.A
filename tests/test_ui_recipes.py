"""Phase 9: the accessibility recipes reach the model only while the
phone's service is on, and always behind a yes on the phone."""
from __future__ import annotations

from nora import command_engine
from nora.hub import protocol

try:
    from tests.test_hub import HubTestCase
except ImportError:          # run from inside tests/
    from test_hub import HubTestCase

RECIPE = {"name": "phone.ui.maps_start", "version": 1, "tier": 2, "available": False,
          "description": "Press Start on the directions Google Maps is showing",
          "params_schema": {"type": "object", "properties": {}}}


class RecipesFollowTheService(HubTestCase):
    async def test_off_means_not_offered_and_on_means_offered_with_a_yes(self) -> None:
        dev = await self.paired(capabilities=[RECIPE])
        self.assertIsNone(command_engine.get_action_meta("phone.ui.maps_start"))

        # The user switches the service on: the phone resends its manifest.
        dev.capabilities = [{**RECIPE, "available": True}]
        dev.tiers = {RECIPE["name"]: 2}
        await dev._send("manifest", {"capabilities": dev.capabilities})
        await self.until(lambda: command_engine.get_action_meta("phone.ui.maps_start") is not None)
        meta = command_engine.get_action_meta("phone.ui.maps_start")
        self.assertIn("asks on the device first", meta.description)

    async def test_a_recipe_declared_tier_one_is_still_asked_about(self) -> None:
        self.assertGreaterEqual(self.hub._effective_tier("phone.ui.maps_start", 1, "live_user"),
                                protocol.TIER_CONFIRM)
