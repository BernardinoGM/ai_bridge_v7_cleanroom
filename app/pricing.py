from dataclasses import dataclass
from typing import Literal


Mode = Literal["fast", "smart", "assured"]


@dataclass(frozen=True)
class TopUpPack:
    code: str
    name: str
    price_usd: float
    bonus_usd: float
    tagline: str
    governance_note: str


TOP_UP_PACKS = {
    "starter": TopUpPack(
        code="starter",
        name="Starter Pack",
        price_usd=10.0,
        bonus_usd=0.0,
        tagline="Try AI Bridge without committing to a heavy spend.",
        governance_note="Fast entry into Smart mode workflows.",
    ),
    "growth": TopUpPack(
        code="growth",
        name="Growth Pack",
        price_usd=50.0,
        bonus_usd=5.0,
        tagline="The default operating pack for regular builders and operators.",
        governance_note="Includes a controlled bonus for longer runway without giving away margin.",
    ),
    "scale": TopUpPack(
        code="scale",
        name="Scale Pack",
        price_usd=200.0,
        bonus_usd=30.0,
        tagline="For teams that want governance, priority, and safer scaling.",
        governance_note="Premium pack with stronger controls, runway management, and upsell surface.",
    ),
}

MODE_MULTIPLIERS: dict[Mode, float] = {
    "fast": 0.012,
    "smart": 0.028,
    "assured": 0.085,
}


def get_pack(code: str) -> TopUpPack:
    if code not in TOP_UP_PACKS:
        raise KeyError(f"Unknown pack: {code}")
    return TOP_UP_PACKS[code]


def estimate_public_charge(mode: Mode, prompt_tokens: int, completion_tokens: int, quality_check: bool = False) -> float:
    base = (prompt_tokens + completion_tokens) / 1000.0 * MODE_MULTIPLIERS[mode]
    if quality_check:
        base += 0.015
    minimum = {"fast": 0.01, "smart": 0.03, "assured": 0.09}[mode]
    return round(max(base, minimum), 4)

