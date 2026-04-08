from dataclasses import dataclass

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import add_wallet_entry
from app.config import Settings
from app.models import PaymentRecord, ProcessedWebhook, User
from app.perks import maybe_grant_referral_perk
from app.pricing import TOP_UP_PACKS, get_pack


@dataclass(frozen=True)
class CheckoutResult:
    checkout_url: str
    session_id: str


def configure_stripe(settings: Settings) -> None:
    stripe.api_key = settings.stripe_secret_key


def ensure_seed_user(db: Session, email: str, name: str, referral_code: str | None = None) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user:
        return user
    code = referral_code or email.split("@")[0][:8].upper()
    user = User(email=email, name=name, referral_code=code)
    db.add(user)
    db.flush()
    return user


def create_checkout_session(
    db: Session,
    settings: Settings,
    user_id: int,
    pack_code: str,
    referred_by_code: str | None = None,
) -> CheckoutResult:
    pack = get_pack(pack_code)
    user = db.get(User, user_id)
    effective_referred_by_code = referred_by_code
    if effective_referred_by_code is None and user and user.referred_by_user_id:
        referrer = db.get(User, user.referred_by_user_id)
        effective_referred_by_code = referrer.referral_code if referrer else None
    configure_stripe(settings)
    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=settings.stripe_success_url,
        cancel_url=settings.stripe_cancel_url,
        line_items=[
            {
                "price_data": {
                    "currency": settings.default_currency,
                    "product_data": {"name": pack.name},
                    "unit_amount": int(pack.price_usd * 100),
                },
                "quantity": 1,
            }
        ],
        metadata={"user_id": str(user_id), "pack_code": pack_code, "referred_by_code": effective_referred_by_code or ""},
    )
    payment = PaymentRecord(
        user_id=user_id,
        pack_code=pack_code,
        amount_usd=pack.price_usd,
        bonus_usd=pack.bonus_usd,
        status="pending",
        stripe_session_id=session.id,
        referred_by_code=effective_referred_by_code,
    )
    db.add(payment)
    db.flush()
    return CheckoutResult(checkout_url=session.url, session_id=session.id)


def _credit_payment_if_needed(db: Session, payment: PaymentRecord) -> None:
    if payment.status == "completed":
        return
    pack = TOP_UP_PACKS[payment.pack_code]
    add_wallet_entry(
        db=db,
        user_id=payment.user_id,
        amount_usd=pack.price_usd,
        entry_type="topup_credit",
        description=f"{pack.name} main balance credit",
        external_ref=payment.stripe_session_id,
    )
    if pack.bonus_usd > 0:
        add_wallet_entry(
            db=db,
            user_id=payment.user_id,
            amount_usd=pack.bonus_usd,
            entry_type="topup_bonus",
            description=f"{pack.name} controlled bonus",
            external_ref=f"{payment.stripe_session_id}:bonus",
        )
    payment.status = "completed"
    maybe_grant_referral_perk(db, payment)


def process_checkout_completed(
    db: Session,
    event_id: str,
    stripe_session_id: str,
    stripe_payment_intent_id: str | None,
) -> bool:
    if db.scalar(select(ProcessedWebhook).where(ProcessedWebhook.event_id == event_id)):
        return False
    payment = db.scalar(select(PaymentRecord).where(PaymentRecord.stripe_session_id == stripe_session_id))
    if payment is None:
        raise ValueError("Unknown Stripe session")
    payment.stripe_payment_intent_id = stripe_payment_intent_id
    _credit_payment_if_needed(db, payment)
    db.add(ProcessedWebhook(event_id=event_id, event_type="checkout.session.completed"))
    db.flush()
    return True
