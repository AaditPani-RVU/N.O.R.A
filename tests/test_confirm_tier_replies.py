"""Phase 8d: what reaches other people needs a yes on the phone, whatever
the phone's build declares (the core's tier floors in config.yaml)."""
from __future__ import annotations

import pytest

from nora.config import get_config
from nora.hub import protocol
from nora.hub.server import Hub
from nora import channel

FLOORS = {"phone.reply_notification": 2, "phone.send_whatsapp": 2,
          "phone.send_sms": 3, "phone.dnd": 2}


def test_the_shipped_config_keeps_replies_behind_a_yes():
    tiers = (get_config().get("hub", {}) or {}).get("capability_tiers") or {}
    for name, floor in FLOORS.items():
        assert int(tiers.get(name, 0)) >= floor, name


@pytest.mark.parametrize("name", sorted(FLOORS))
def test_a_device_declaring_tier_zero_still_has_to_ask(name):
    tier = Hub._effective_tier(Hub.__new__(Hub), name, 0, channel.ORIGIN_LIVE_USER)
    assert tier >= protocol.TIER_CONFIRM


def test_a_text_asked_for_by_a_job_is_past_what_the_phone_will_run():
    # SMS is tier 3 live; from a job it's 4: not run at all.
    assert Hub._effective_tier(Hub.__new__(Hub), "phone.send_sms", 3, channel.ORIGIN_JOB) \
        >= protocol.TIER_FORBIDDEN
