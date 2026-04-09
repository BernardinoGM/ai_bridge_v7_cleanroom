from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import add_wallet_entry, wallet_balance
from app.config import Settings
from app.models import ApiKey, User, WalletLedger
from app.payments import ensure_seed_user


STARTER_CREDIT_USD = 3.0


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _display_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
    return local.title() or "AI Bridge User"


def _hash_key(raw_key: str, settings: Settings) -> str:
    return hashlib.sha256(f"{settings.secret_key}:{raw_key}".encode("utf-8")).hexdigest()


def authenticate_api_key(
    db: Session,
    settings: Settings,
    raw_key: str | None,
) -> User | None:
    if not raw_key:
        return None
    key_hash = _hash_key(raw_key.strip(), settings)
    api_key = db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None)))
    if api_key is None:
        return None
    api_key.last_used_at = datetime.utcnow()
    return db.get(User, api_key.user_id)


def attach_referrer_by_code(db: Session, user: User, referred_by_code: str | None) -> None:
    if not referred_by_code or user.referred_by_user_id:
        return
    referrer = db.scalar(select(User).where(User.referral_code == referred_by_code.strip().upper()))
    if referrer and referrer.id != user.id:
        user.referred_by_user_id = referrer.id


def issue_api_key(
    db: Session,
    settings: Settings,
    email: str,
    name: str | None = None,
    use_case: str | None = None,
    referred_by_code: str | None = None,
) -> tuple[User, str, float, float]:
    normalized_email = _normalize_email(email)
    display_name = name.strip()[:120] if name and name.strip() else _display_name_from_email(normalized_email)
    user = ensure_seed_user(db, email=normalized_email, name=display_name)
    if not user.name and display_name:
        user.name = display_name
    attach_referrer_by_code(db, user, referred_by_code)
    existing_grant = db.scalar(
        select(WalletLedger).where(
            WalletLedger.user_id == user.id,
            WalletLedger.entry_type == "api_key_starter_credit",
            WalletLedger.external_ref == f"api_key_grant:{user.id}",
        )
    )
    raw_key = f"ab_live_{secrets.token_urlsafe(24)}"
    api_key = ApiKey(
        user_id=user.id,
        key_prefix=raw_key[:16],
        key_hash=_hash_key(raw_key, settings),
        label=(use_case or "Launch key")[:120],
    )
    db.add(api_key)
    granted_credit = 0.0
    if STARTER_CREDIT_USD > 0 and existing_grant is None:
        add_wallet_entry(
            db=db,
            user_id=user.id,
            amount_usd=STARTER_CREDIT_USD,
            entry_type="api_key_starter_credit",
            description="Starter credit for API key launch access",
            bucket="main",
            external_ref=f"api_key_grant:{user.id}",
        )
        granted_credit = STARTER_CREDIT_USD
    db.flush()
    return user, raw_key, granted_credit, wallet_balance(db, user.id, "main")
