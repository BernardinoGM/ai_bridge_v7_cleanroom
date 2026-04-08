from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    referral_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    referred_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    preferred_mode: Mapped[str] = mapped_column(String(20), default="smart")
    auto_reload_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_reload_threshold_usd: Mapped[float | None] = mapped_column(Float)
    auto_reload_pack_code: Mapped[str | None] = mapped_column(String(50))


class AgentProfile(TimestampMixin, Base):
    __tablename__ = "agent_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    preferred_mode: Mapped[str] = mapped_column(String(20), default="smart")
    default_provider_family: Mapped[str] = mapped_column(String(30), default="ds")
    workload_pattern: Mapped[str] = mapped_column(String(50), default="general")
    escalation_sensitivity: Mapped[str] = mapped_column(String(20), default="balanced")
    qa_preference: Mapped[str] = mapped_column(String(20), default="adaptive")
    cost_guardrail_band: Mapped[str] = mapped_column(String(20), default="standard")
    stable_task_bias: Mapped[str] = mapped_column(String(20), default="enabled")
    pacing_context: Mapped[str] = mapped_column(String(30), default="steady")
    last_successful_provider: Mapped[str | None] = mapped_column(String(50))
    recent_premium_trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    recent_ds_success_rate: Mapped[float] = mapped_column(Float, default=1.0)
    fallback_count: Mapped[int] = mapped_column(Integer, default=0)
    qa_trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    fallback_count_7d: Mapped[int] = mapped_column(Integer, default=0)
    qa_trigger_rate_7d: Mapped[float] = mapped_column(Float, default=0.0)
    stable_task_completion_rate_7d: Mapped[float] = mapped_column(Float, default=1.0)
    ds_clean_success_count_7d: Mapped[int] = mapped_column(Integer, default=0)
    premium_escalation_count_7d: Mapped[int] = mapped_column(Integer, default=0)
    last_execution_profile: Mapped[str | None] = mapped_column(String(50))
    learned_hints_json: Mapped[dict | None] = mapped_column(JSON)
    last_task_id: Mapped[str | None] = mapped_column(String(64), index=True)


class TaskSession(TimestampMixin, Base):
    __tablename__ = "task_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(20), default="active")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    task_mode: Mapped[str] = mapped_column(String(20), default="smart")
    pinned_lane: Mapped[str] = mapped_column(String(20), default="smart")
    internal_lane: Mapped[str] = mapped_column(String(30), default="balanced")
    pinned_provider: Mapped[str] = mapped_column(String(50), default="remote_balanced")
    pinned_execution_profile: Mapped[str] = mapped_column(String(50), default="balanced_remote")
    continuity_status: Mapped[str] = mapped_column(String(20), default="in_progress")
    quality_check_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary: Mapped[str | None] = mapped_column(String(255))
    last_assistant_excerpt: Mapped[str | None] = mapped_column(Text)
    last_status_label: Mapped[str | None] = mapped_column(String(20))
    source_surface: Mapped[str | None] = mapped_column(String(40))
    last_user_message: Mapped[str | None] = mapped_column(Text)
    notes_json: Mapped[dict | None] = mapped_column(JSON)


class TaskTurn(TimestampMixin, Base):
    __tablename__ = "task_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_session_id: Mapped[int] = mapped_column(ForeignKey("task_sessions.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_message: Mapped[str] = mapped_column(Text)
    assistant_excerpt: Mapped[str | None] = mapped_column(Text)
    visible_lane: Mapped[str] = mapped_column(String(20))
    internal_lane: Mapped[str] = mapped_column(String(30))
    status_label: Mapped[str] = mapped_column(String(20), default="in_progress")
    quality_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    source_surface: Mapped[str | None] = mapped_column(String(40))


class WalletLedger(TimestampMixin, Base):
    __tablename__ = "wallet_ledger"
    __table_args__ = (UniqueConstraint("external_ref", "entry_type", name="uq_wallet_external_ref_entry_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(40), index=True)
    bucket: Mapped[str] = mapped_column(String(20), default="main")
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(String(255))
    external_ref: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    user: Mapped[User] = relationship()


class PaymentRecord(TimestampMixin, Base):
    __tablename__ = "payment_records"
    __table_args__ = (UniqueConstraint("stripe_session_id", name="uq_payment_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    pack_code: Mapped[str] = mapped_column(String(50))
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    bonus_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    stripe_session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255))
    referred_by_code: Mapped[str | None] = mapped_column(String(32))


class ProcessedWebhook(TimestampMixin, Base):
    __tablename__ = "processed_webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(255))


class ReferralPerk(TimestampMixin, Base):
    __tablename__ = "referral_perks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    trigger_payment_id: Mapped[int] = mapped_column(ForeignKey("payment_records.id"), unique=True)
    perk_type: Mapped[str] = mapped_column(String(50), default="promo_credit")
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="active")


class AddOnSubscription(TimestampMixin, Base):
    __tablename__ = "addon_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    addon_code: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), default="available")
    monthly_price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)


class TeamWorkspace(TimestampMixin, Base):
    __tablename__ = "team_workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    shared_wallet_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class TeamMember(TimestampMixin, Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_team_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("team_workspaces.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(30), default="member")


class UsageEvent(TimestampMixin, Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(20))
    public_charge_usd: Mapped[float] = mapped_column(Float, nullable=False)
    serving_cogs_usd: Mapped[float] = mapped_column(Float, nullable=False)
    benchmark_cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    gross_margin_guardrail_usd: Mapped[float] = mapped_column(Float, nullable=False)
    route_chosen: Mapped[str] = mapped_column(String(80))
    premium_escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    local_model_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_check_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer)
    prompt_tokens_est: Mapped[int] = mapped_column(Integer)
    completion_tokens_est: Mapped[int] = mapped_column(Integer)
    request_payload: Mapped[dict | None] = mapped_column(JSON)
    response_excerpt: Mapped[str | None] = mapped_column(Text)


class DemoTrial(TimestampMixin, Base):
    __tablename__ = "demo_trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tries_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_example: Mapped[str | None] = mapped_column(String(32))
