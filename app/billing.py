from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import WalletLedger


def wallet_balance(db: Session, user_id: int, bucket: str = "main") -> float:
    value = db.scalar(
        select(func.coalesce(func.sum(WalletLedger.amount_usd), 0.0)).where(
            WalletLedger.user_id == user_id, WalletLedger.bucket == bucket
        )
    )
    return round(float(value or 0.0), 4)


def add_wallet_entry(
    db: Session,
    user_id: int,
    amount_usd: float,
    entry_type: str,
    description: str,
    bucket: str = "main",
    external_ref: str | None = None,
    metadata_json: dict | None = None,
) -> WalletLedger:
    entry = WalletLedger(
        user_id=user_id,
        amount_usd=amount_usd,
        entry_type=entry_type,
        bucket=bucket,
        description=description,
        external_ref=external_ref,
        metadata_json=metadata_json,
    )
    db.add(entry)
    db.flush()
    return entry


def debit_usage(db: Session, user_id: int, amount_usd: float, request_id: str, mode: str) -> WalletLedger:
    return add_wallet_entry(
        db=db,
        user_id=user_id,
        amount_usd=-abs(amount_usd),
        entry_type="usage_debit",
        bucket="main",
        description=f"{mode.title()} mode usage",
        external_ref=request_id,
        metadata_json={"mode": mode},
    )

