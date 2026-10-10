"""Channels — where a turn came from, and therefore where its answer goes.

`pipeline.handle_turn` used to answer through `TurnDeps.speak` and confirm
through `TurnDeps.confirm`, both process-wide: the laptop speaker and the
laptop microphone. A typed command from the dashboard, a Telegram message and
(soon) a phone all ended up speaking out of the laptop, and anything that
needed a "yes" could only get one from someone in the room.

A `Channel` is the turn's return address. It carries the device the request
came from, how to say something back to it, and — only if the device can
genuinely ask its user — how to ask for confirmation. A channel with no
`confirm` cannot approve anything; the turn refuses rather than guessing.

The *current* turn's channel is also published in a context variable, so code
far from the turn (the audit log inside `command_engine`, the delivery router)
can attribute and route without every signature in between growing a
parameter.
"""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from nora.schemas import ActionStep

LOCAL_DEVICE = "local"

# `origin` values (NORA_DISTRIBUTED_PLAN.md §5): who asked for this.
ORIGIN_LIVE_USER = "live_user"
ORIGIN_AUTOMATION = "automation"
ORIGIN_JOB = "job"


@dataclass
class ConfirmRequest:
    """What a channel is asked to confirm. Devices render their own prompt from
    `steps` rather than trusting `rendered` (plan §5, Confirmation)."""

    turn_id: str
    steps: list[ActionStep]
    rendered: str
    expires_in: float = 60.0


@dataclass
class Channel:
    device_id: str
    kind: str                                   # "voice" | "text" | "device"
    speak: Callable[..., None]
    confirm: Callable[[ConfirmRequest], Awaitable[bool]] | None = None
    session_id: str = ""
    origin: str = ORIGIN_LIVE_USER
    turn_id: str = field(default_factory=lambda: "t_" + uuid.uuid4().hex[:10])
    # Set by the turn once this channel's user approves; read by the audit log.
    confirmed_by: str = ""
    # Set once a step returns text someone other than the user wrote (a phone
    # notification). Every action decided after that point needs this
    # channel's yes (plan §7.5): see `command_engine.execute`.
    tainted: bool = False
    # A test turn runs normally but is not remembered (nora.dev).
    test: bool = False
    # Where on the device the turn came from, when the device has more than
    # one way in: "dashboard" or "keyboard" for text typed on the core.
    source: str = ""
    # Whether `speak` puts what was said into the transcript itself. Only the
    # laptop speaker does (it knows how much was said before an interruption);
    # for every other channel the turn records each line (Sharp F).
    records_itself: bool = False

    @property
    def label(self) -> str:
        """Who spoke, as the transcript keeps it: the device, or where on the
        core the words came from."""
        return self.source or self.device_id

    @property
    def can_confirm(self) -> bool:
        return self.confirm is not None

    @property
    def is_local(self) -> bool:
        """The core's own microphone and speaker — the only channel that may
        end the process by saying goodbye."""
        return self.device_id == LOCAL_DEVICE


def local(speak: Callable[..., None],
          confirm: Callable[[], Awaitable[bool]], source: str = "") -> Channel:
    """The core's own mic and speaker, built from the classic `TurnDeps` edges.
    `source` names text typed on the core ("dashboard", "keyboard")."""

    async def _confirm(_request: ConfirmRequest) -> bool:
        return await confirm()

    from nora import speaker
    return Channel(device_id=LOCAL_DEVICE, kind="text" if source else "voice", speak=speak,
                   confirm=_confirm, source=source, records_itself=speak is speaker.speak)


class Collector:
    """A `speak` that records instead of talking, for text channels whose reply
    is returned rather than spoken (Telegram, the fake device)."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, text: str, *_a, **_kw) -> None:
        if text:
            self.said.append(text)

    def text(self) -> str:
        return " ".join(self.said).strip()


_current: contextvars.ContextVar[Channel | None] = contextvars.ContextVar(
    "nora_channel", default=None)


def current() -> Channel | None:
    """The channel of the turn running in this context, if any."""
    return _current.get()


def bind(channel: Channel) -> contextvars.Token:
    return _current.set(channel)


def unbind(token: contextvars.Token) -> None:
    _current.reset(token)
