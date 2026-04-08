from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import add_wallet_entry
from app.models import PaymentRecord, ReferralPerk, User


def maybe_grant_referral_perk(db: Session, payment: PaymentRecord) -> ReferralPerk | None:
    existing = db.scalar(select(ReferralPerk).where(ReferralPerk.trigger_payment_id == payment.id))
    if existing:
        return existing
    referred_user = db.get(User, payment.user_id)
    if not referred_user:
        return None
    referrer = None
    if payment.referred_by_code is not None:
        referrer = db.scalar(select(User).where(User.referral_code == payment.referred_by_code))
    elif referred_user.referred_by_user_id is not None:
        referrer = db.get(User, referred_user.referred_by_user_id)
    if not referrer or referrer.id == referred_user.id:
        return None
    prior_paid = db.scalar(
        select(PaymentRecord)
        .where(
            PaymentRecord.user_id == payment.user_id,
            PaymentRecord.status == "completed",
            PaymentRecord.id != payment.id,
        )
        .limit(1)
    )
    if prior_paid:
        return None
    perk_amount = round(payment.amount_usd * 0.10, 2)
    perk = ReferralPerk(
        referrer_user_id=referrer.id,
        referred_user_id=referred_user.id,
        trigger_payment_id=payment.id,
        perk_type="promo_credit",
        amount_usd=perk_amount,
        expires_at=datetime.utcnow() + timedelta(days=90),
        status="active",
    )
    db.add(perk)
    db.flush()
    add_wallet_entry(
        db=db,
        user_id=referrer.id,
        amount_usd=perk_amount,
        entry_type="referral_perk_credit",
        bucket="promo",
        description="Closed-loop referral perk",
        external_ref=f"referral:{payment.id}",
        metadata_json={"referred_user_id": referred_user.id},
    )
    return perk
