from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    user_id: int = Field(..., description="AI Bridge internal user id")
    mode: Literal["fast", "smart", "assured"] = "smart"
    messages: list[ChatMessage]
    stream: bool = False


class MessagesRequest(BaseModel):
    user_id: int
    mode: Literal["fast", "smart", "assured"] = "smart"
    system: str | None = None
    messages: list[dict[str, Any]]
    task_id: str | None = None
    task_action: Literal["continue", "escalate", "deescalate"] | None = None
    source_surface: str | None = None
    stream: bool = False


class CheckoutCreateRequest(BaseModel):
    user_id: int
    pack_code: Literal["starter", "growth", "scale"]
    referred_by_code: str | None = None


class DemoChatRequest(BaseModel):
    example: Literal["spec", "refactor", "reply"]
