from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import (
    ExecutionStrategy,
    assess_request,
    build_execution_strategy,
    get_or_create_agent_profile,
    runtime_plan_for_strategy,
    strategy_summary,
    update_profile_after_turn,
)
from app.models import TaskSession, TaskTurn


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
    source_surface: str | None = None,
) -> TaskSession:
    profile = get_or_create_agent_profile(db, user_id)
    task = None
    surface = source_surface or "api"
    messages = [{"role": "user", "content": prompt}]
    if task_id:
        task = db.scalar(select(TaskSession).where(TaskSession.task_id == task_id, TaskSession.user_id == user_id))
    if task and not _expired(task):
        if task_action == "escalate":
            task.pinned_lane = "assured"
            task.internal_lane = "high_assurance"
            task.pinned_provider = "ab_orchestrator"
            task.pinned_execution_profile = "premium_anthropic"
            task.quality_check_enabled = True
            task.continuity_status = "checked"
        elif task_action == "deescalate":
            task.pinned_lane = "smart"
            task.internal_lane = "coding_primary"
            task.pinned_provider = "ab_orchestrator"
            task.pinned_execution_profile = "remote_balanced"
            task.quality_check_enabled = False
            task.continuity_status = "in_progress"
        elif task.continuity_status == "checked" and task.turn_count >= 1 and not _is_correction_or_refactor_prompt(prompt):
            task.quality_check_enabled = False
        task.expires_at = datetime.utcnow() + timedelta(hours=TASK_TIMEOUT_HOURS)
        task.last_user_message = prompt[:1000]
        if source_surface:
            task.source_surface = source_surface
        notes = dict(task.notes_json or {})
        assessment = assess_request(
            messages,
            profile,
            surface,
            session_context={"task_id": task.task_id, "resume_task": True, "continuity_status": task.continuity_status},
            workspace_context=notes.get("workspace_context") if isinstance(notes.get("workspace_context"), dict) else None,
        )
        strategy = build_execution_strategy(
            assessment,
            profile,
            session_context={"task_id": task.task_id, "resume_task": True, "continuity_status": task.continuity_status},
            workspace_context=notes.get("workspace_context") if isinstance(notes.get("workspace_context"), dict) else None,
        )
        runtime_plan = runtime_plan_for_strategy(strategy, surface)
        if task.pinned_lane == "assured" and task_action != "deescalate":
            task.internal_lane = "high_assurance"
            task.pinned_lane = "assured"
            task.pinned_provider = "ab_orchestrator"
            task.pinned_execution_profile = "premium_anthropic"
            task.quality_check_enabled = True
            task.continuity_status = "checked"
        else:
            task.internal_lane = strategy.primary_lane
            task.pinned_lane = runtime_plan.visible_mode
            task.pinned_provider = "ab_orchestrator"
            task.pinned_execution_profile = runtime_plan.primary_execution_profile
            task.quality_check_enabled = runtime_plan.quality_check
        notes["strategy"] = strategy_summary(strategy)
        task.notes_json = notes
        return task

    assessment = assess_request(messages, profile, surface)
    strategy = build_execution_strategy(assessment, profile)
    runtime_plan = runtime_plan_for_strategy(strategy, surface)
    task = TaskSession(
        task_id=task_id or uuid4().hex,
        user_id=user_id,
        title=prompt[:80],
        state="active",
        task_mode=requested_mode,
        pinned_lane=runtime_plan.visible_mode,
        internal_lane=strategy.primary_lane,
        pinned_provider="ab_orchestrator",
        pinned_execution_profile=runtime_plan.primary_execution_profile,
        continuity_status="in_progress",
        quality_check_enabled=runtime_plan.quality_check,
        turn_count=0,
        expires_at=datetime.utcnow() + timedelta(hours=TASK_TIMEOUT_HOURS),
        summary=prompt[:180],
        last_status_label=runtime_plan.status_label,
        source_surface=surface,
        last_user_message=prompt[:1000],
        notes_json={
            "continuity_policy": "task_pinned",
            "entered_via": "/v1/messages",
            "source_surface": surface,
            "assessment": strategy_summary(strategy),
            "strategy": strategy_summary(strategy),
        },
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
    source_surface: str | None = None,
) -> None:
    task.turn_count += 1
    task.continuity_status = "checked" if quality_checked else "in_progress"
    task.summary = (task.summary or task.title or user_message[:180])[:180]
    task.last_assistant_excerpt = assistant_excerpt[:1200]
    task.last_status_label = "Verified" if task.pinned_lane == "assured" else ("Checked" if quality_checked else "In progress")
    if source_surface:
        task.source_surface = source_surface
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
            source_surface=source_surface or task.source_surface or "api",
        )
    )
    profile = get_or_create_agent_profile(db, task.user_id)
    strategy_notes = dict(task.notes_json or {}).get("strategy") or {}
    strategy = ExecutionStrategy(
        task_type=strategy_notes.get("task_type", "general"),
        difficulty=strategy_notes.get("difficulty", "simple"),
        primary_lane=strategy_notes.get("primary_lane", "balanced"),
        silent_qc=bool(strategy_notes.get("silent_qc", quality_checked)),
        staged_execution=bool(strategy_notes.get("staged_execution", False)),
        planning_mode=strategy_notes.get("planning_mode", "direct"),
        continuity_mode=strategy_notes.get("continuity_mode", "none"),
        needs_repo_context=bool(strategy_notes.get("needs_repo_context", False)),
        needs_long_context=bool(strategy_notes.get("needs_long_context", False)),
        risk_level=strategy_notes.get("risk_level", "low"),
        profile_updates=dict(strategy_notes.get("profile_updates") or {}),
        user_visible_mode=strategy_notes.get("user_visible_mode", "assistant"),
    )
    update_profile_after_turn(
        profile=profile,
        turn_input={
            "task_id": task.task_id,
            "prompt": user_message,
            "surface": source_surface or task.source_surface or "api",
        },
        turn_output={"reply": assistant_excerpt},
        strategy=strategy,
        observed_signals={
            "provider_family": provider_family,
            "execution_profile": execution_profile,
            "premium_escalated": premium_escalated,
            "fallback_used": fallback_used,
            "task_stable": task_stable,
            "ds_succeeded_cleanly": provider_family != "premium_anthropic" and not fallback_used,
            "status_label": task.last_status_label,
        },
    )
