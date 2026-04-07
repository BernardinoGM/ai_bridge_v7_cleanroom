from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import choose_initial_lane, get_or_create_agent_profile, update_profile_after_turn
from app.models import TaskSession, TaskTurn
from app.routing import decide_route


TASK_TIMEOUT_HOURS = 6


def _is_correction_or_refactor_prompt(prompt: str) -> bool:
    text = prompt.lower()
    keywords = [
        "verify",
        "recheck",
        "double-check",
        "double check",
        "correct",
        "fix again",
        "audit",
        "bugfix",
        "bug fix",
        "refactor",
        "regression",
        "patch",
    ]
    return any(word in text for word in keywords)


def _expired(task: TaskSession) -> bool:
    return bool(task.expires_at and task.expires_at < datetime.utcnow())


def resolve_task(
    db: Session,
    user_id: int,
    requested_mode: str,
    prompt: str,
    task_id: str | None,
    task_action: str | None,
) -> TaskSession:
    profile = get_or_create_agent_profile(db, user_id)
    task = None
    if task_id:
        task = db.scalar(select(TaskSession).where(TaskSession.task_id == task_id, TaskSession.user_id == user_id))
    if task and not _expired(task):
        if task_action == "escalate":
            task.pinned_lane = "assured" if task.pinned_lane != "assured" else task.pinned_lane
            task.internal_lane = "premium"
            task.pinned_provider = "premium_reasoner"
            task.pinned_execution_profile = "premium_anthropic"
            task.quality_check_enabled = True
            task.continuity_status = "checked"
        elif task_action == "deescalate":
            task.pinned_lane = "smart" if task.pinned_lane == "assured" else "fast"
            task.internal_lane = "balanced" if task.pinned_lane == "smart" else "fast"
            task.pinned_provider = "remote_balanced" if task.pinned_lane == "smart" else "remote_fast"
            task.pinned_execution_profile = "remote_balanced" if task.pinned_lane == "smart" else "remote_fast"
            task.quality_check_enabled = False
            task.continuity_status = "in_progress"
        elif task.continuity_status == "checked" and task.turn_count >= 1 and not _is_correction_or_refactor_prompt(prompt):
            task.quality_check_enabled = False
        task.expires_at = datetime.utcnow() + timedelta(hours=TASK_TIMEOUT_HOURS)
        task.last_user_message = prompt[:1000]
        return task

    visible_lane, internal_lane, quality_check = choose_initial_lane(profile, requested_mode, prompt)
    route = decide_route(prompt, visible_lane, internal_lane=internal_lane, quality_check_override=quality_check)
    task = TaskSession(
        task_id=task_id or uuid4().hex,
        user_id=user_id,
        title=prompt[:80],
        state="active",
        task_mode=requested_mode,
        pinned_lane=visible_lane,
        internal_lane=internal_lane,
        pinned_provider=route.provider,
        pinned_execution_profile=route.execution_profile,
        continuity_status="in_progress",
        quality_check_enabled=quality_check,
        turn_count=0,
        expires_at=datetime.utcnow() + timedelta(hours=TASK_TIMEOUT_HOURS),
        last_user_message=prompt[:1000],
        notes_json={"continuity_policy": "task_pinned", "entered_via": "/v1/messages"},
    )
    db.add(task)
    db.flush()
    return task


def record_task_turn(
    db: Session,
    task: TaskSession,
    request_id: str,
    user_message: str,
    assistant_excerpt: str,
    quality_checked: bool,
    provider_family: str,
    execution_profile: str,
    premium_escalated: bool,
    fallback_used: bool,
    task_stable: bool,
) -> None:
    task.turn_count += 1
    task.continuity_status = "checked" if quality_checked else "in_progress"
    if task.turn_count > 1 and provider_family != "premium_anthropic" and not fallback_used and not _is_correction_or_refactor_prompt(user_message):
        task.quality_check_enabled = False
    task.expires_at = datetime.utcnow() + timedelta(hours=TASK_TIMEOUT_HOURS)
    db.add(
        TaskTurn(
            task_session_id=task.id,
            request_id=request_id,
            user_message=user_message[:2000],
            assistant_excerpt=assistant_excerpt[:400],
            visible_lane=task.pinned_lane,
            internal_lane=task.internal_lane,
            status_label=task.continuity_status,
            quality_checked=quality_checked,
        )
    )
    profile = get_or_create_agent_profile(db, task.user_id)
    update_profile_after_turn(
        profile=profile,
        task_id=task.task_id,
        visible_lane=task.pinned_lane,
        quality_checked=quality_checked,
        provider_family=provider_family,
        execution_profile=execution_profile,
        premium_escalated=premium_escalated,
        fallback_used=fallback_used,
        task_stable=task_stable,
        ds_succeeded_cleanly=provider_family != "premium_anthropic" and not fallback_used,
    )
