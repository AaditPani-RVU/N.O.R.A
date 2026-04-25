from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ActionStep(BaseModel):
    action: str
    parameters: dict[str, Any] = {}


class IntentResponse(BaseModel):
    intent: str
    steps: list[ActionStep]
    requires_confirmation: bool = False
    error: str | None = None


class StepResult(BaseModel):
    action: str
    success: bool
    message: str = ""
