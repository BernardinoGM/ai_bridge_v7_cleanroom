from dataclasses import dataclass
from typing import Literal

from app.pricing import Mode


RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class RouteDecision:
    provider: str
    provider_model: str
    premium_escalated: bool
    quality_check: bool
    fallback_provider: str | None
    local_model_hit: bool
    execution_profile: str


def classify_risk(prompt: str, mode: Mode) -> RiskLevel:
    text = prompt.lower()
    if mode == "assured":
        return "high"
    if any(term in text for term in ["release", "legal", "security", "prod", "migration"]):
        return "high"
    if any(term in text for term in ["strategy", "architecture", "finance", "customer"]):
        return "medium"
    return "low"


def decide_route(prompt: str, mode: Mode, internal_lane: str | None = None, quality_check_override: bool | None = None) -> RouteDecision:
    risk = classify_risk(prompt, mode)
    if internal_lane == "premium":
        return RouteDecision(
            provider="premium_reasoner",
            provider_model="assured-lane",
            premium_escalated=True,
            quality_check=True if quality_check_override is None else quality_check_override,
            fallback_provider="balanced_lane",
            local_model_hit=False,
            execution_profile="premium_anthropic",
        )
    if internal_lane == "balanced":
        return RouteDecision(
            provider="balanced_lane",
            provider_model="smart-lane",
            premium_escalated=False,
            quality_check=(risk != "low") if quality_check_override is None else quality_check_override,
            fallback_provider="premium_reasoner",
            local_model_hit=False,
            execution_profile="remote_balanced",
        )
    if internal_lane == "fast":
        return RouteDecision(
            provider="fast_lane",
            provider_model="fast-lane",
            premium_escalated=False,
            quality_check=False if quality_check_override is None else quality_check_override,
            fallback_provider="balanced_lane",
            local_model_hit=False,
            execution_profile="remote_fast",
        )
    if mode == "fast":
        return RouteDecision(
            provider="fast_lane",
            provider_model="fast-lane",
            premium_escalated=False,
            quality_check=False,
            fallback_provider="balanced_lane",
            local_model_hit=False,
            execution_profile="remote_fast",
        )
    if mode == "smart":
        if risk == "high":
            return RouteDecision(
                provider="premium_reasoner",
                provider_model="assured-lane",
                premium_escalated=True,
                quality_check=True,
                fallback_provider="balanced_lane",
                local_model_hit=False,
                execution_profile="premium_anthropic",
            )
        return RouteDecision(
            provider="balanced_lane",
            provider_model="smart-lane",
            premium_escalated=False,
            quality_check=risk == "medium",
            fallback_provider="premium_reasoner",
            local_model_hit=False,
            execution_profile="remote_balanced",
        )
    return RouteDecision(
        provider="premium_reasoner",
        provider_model="assured-lane",
        premium_escalated=True,
        quality_check=True,
        fallback_provider="balanced_lane",
        local_model_hit=False,
        execution_profile="premium_anthropic",
    )
