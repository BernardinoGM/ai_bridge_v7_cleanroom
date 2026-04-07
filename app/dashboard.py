from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.add_ons import ADD_ONS
from app.billing import wallet_balance
from app.models import UsageEvent


@dataclass
class RunwayEstimate:
    mode: str
    days_left: int
    heavy_workdays_left: int


def estimate_runway(balance_usd: float, daily_spend_usd: float, heavy_day_multiplier: float = 2.6) -> tuple[int, int]:
    if daily_spend_usd <= 0:
        return 365, 140
    days = int(balance_usd / daily_spend_usd)
    heavy_days = int(balance_usd / (daily_spend_usd * heavy_day_multiplier))
    return max(days, 0), max(heavy_days, 0)


def build_dashboard(db: Session, user_id: int) -> dict:
    balance = wallet_balance(db, user_id, "main")
    events = db.scalars(
        select(UsageEvent).where(UsageEvent.user_id == user_id).order_by(desc(UsageEvent.created_at)).limit(30)
    ).all()
    total_recent_spend = sum(event.public_charge_usd for event in events)
    daily_spend = total_recent_spend / max(len(events) / 3, 1) if events else 1.25
    estimates = []
    for mode, multiplier in [("fast", 0.75), ("smart", 1.0), ("assured", 1.9)]:
        days, heavy_days = estimate_runway(balance, daily_spend * multiplier)
        estimates.append(RunwayEstimate(mode=mode, days_left=days, heavy_workdays_left=heavy_days))
    premium_savings = round(sum(max(event.benchmark_cost_usd - event.serving_cogs_usd, 0) for event in events), 2)
    return {
        "balance_usd": round(balance, 2),
        "days_left": estimates[1].days_left,
        "heavy_workdays_left": estimates[1].heavy_workdays_left,
        "mode_estimates": estimates,
        "premium_savings_estimate_usd": premium_savings,
        "events": events,
        "upsells": ADD_ONS,
    }
