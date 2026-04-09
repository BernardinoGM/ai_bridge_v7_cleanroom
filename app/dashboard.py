from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.add_ons import ADD_ONS
from app.billing import wallet_balance
from app.models import ApiKey, DemoTrial, PaymentRecord, ReferralPerk, RequestFailure, UsageEvent, User, WalletLedger


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
    user = db.get(User, user_id)
    balance = wallet_balance(db, user_id, "main")
    ledger = db.scalars(
        select(WalletLedger).where(WalletLedger.user_id == user_id, WalletLedger.bucket == "main").order_by(desc(WalletLedger.created_at)).limit(20)
    ).all()
    reward_ledger = db.scalars(
        select(WalletLedger).where(WalletLedger.user_id == user_id, WalletLedger.bucket == "promo").order_by(desc(WalletLedger.created_at)).limit(20)
    ).all()
    api_keys = db.scalars(
        select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None)).order_by(desc(ApiKey.created_at)).limit(5)
    ).all()
    payments = db.scalars(
        select(PaymentRecord).where(PaymentRecord.user_id == user_id).order_by(desc(PaymentRecord.created_at)).limit(10)
    ).all()
    referral_perks = db.scalars(
        select(ReferralPerk).where(ReferralPerk.referrer_user_id == user_id).order_by(desc(ReferralPerk.created_at)).limit(10)
    ).all()
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
        "user_id": user_id,
        "user_name": user.name if user else "Demo user",
        "user_email": user.email if user else "founder@aibridge.local",
        "referral_code": user.referral_code if user else "FOUNDER10",
        "referral_link": f"/r/{user.referral_code}" if user else "/r/FOUNDER10",
        "balance_usd": round(balance, 2),
        "promo_balance_usd": round(wallet_balance(db, user_id, "promo"), 2),
        "reward_balance_usd": round(wallet_balance(db, user_id, "promo"), 2),
        "days_left": estimates[1].days_left,
        "heavy_workdays_left": estimates[1].heavy_workdays_left,
        "mode_estimates": estimates,
        "premium_savings_estimate_usd": premium_savings,
        "recent_usage_count": len(events),
        "recent_usage_spend_usd": round(total_recent_spend, 2),
        "recent_usage": events[:8],
        "api_keys": api_keys,
        "recent_topups": payments,
        "recent_referral_perks": referral_perks,
        "main_ledger": ledger[:8],
        "reward_ledger": reward_ledger[:8],
        "events": events,
        "upsells": ADD_ONS,
        "onboarding_commands": [
            'export ANTHROPIC_BASE_URL="https://getaibridge.com/v1"',
            'export ANTHROPIC_API_KEY="YOUR_KEY_FROM_ABOVE"',
            "claude",
        ],
    }


def build_admin_dashboard(db: Session) -> dict:
    now = datetime.utcnow()
    window = now - timedelta(days=7)
    total_users = db.scalar(select(func.count(User.id))) or 0
    new_signups = db.scalar(select(func.count(User.id)).where(User.created_at >= window)) or 0
    total_trials = db.scalar(select(func.count(DemoTrial.id))) or 0
    exhausted_trials = db.scalar(select(func.count(DemoTrial.id)).where(DemoTrial.tries_used >= 3)) or 0
    total_signups = db.scalar(select(func.count(func.distinct(ApiKey.user_id)))) or 0
    users_with_completed_topups = db.scalar(
        select(func.count(func.distinct(PaymentRecord.user_id))).where(PaymentRecord.status == "completed")
    ) or 0
    completed_payments = db.scalars(
        select(PaymentRecord).where(PaymentRecord.status == "completed").order_by(desc(PaymentRecord.created_at)).limit(20)
    ).all()
    total_topups_usd = round(sum(payment.amount_usd + payment.bonus_usd for payment in completed_payments), 2)
    recent_usage = db.scalars(select(UsageEvent).order_by(desc(UsageEvent.created_at)).limit(50)).all()
    total_usage_usd = round(sum(event.public_charge_usd for event in recent_usage), 2)
    total_savings_usd = round(sum(max(event.benchmark_cost_usd - event.serving_cogs_usd, 0) for event in recent_usage), 2)
    referral_perks = db.scalars(select(ReferralPerk).order_by(desc(ReferralPerk.created_at)).limit(20)).all()
    recent_failures = db.scalars(select(RequestFailure).order_by(desc(RequestFailure.created_at)).limit(20)).all()
    return {
        "total_users": int(total_users),
        "new_signups_7d": int(new_signups),
        "trial_count": int(total_trials),
        "exhausted_trials": int(exhausted_trials),
        "trial_to_signup_conversion": round((total_signups / total_trials) * 100, 1) if total_trials else 0.0,
        "signup_to_topup_conversion": round((users_with_completed_topups / total_signups) * 100, 1) if total_signups else 0.0,
        "recent_topups": completed_payments,
        "total_topups_usd": total_topups_usd,
        "total_usage_usd": total_usage_usd,
        "total_savings_usd": total_savings_usd,
        "referral_count": len(referral_perks),
        "referral_credit_usd": round(sum(perk.amount_usd for perk in referral_perks), 2),
        "recent_failures": recent_failures,
    }
