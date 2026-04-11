from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentProfile, User


ROLLING_WINDOW_DAYS = 7
TaskType = Literal["coding", "general", "mixed"]
Difficulty = Literal["simple", "medium", "hard"]
RiskLevel = Literal["low", "normal", "high"]
Surface = Literal["try", "terminal", "api"]
LanguagePreference = Literal["en", "zh", "auto"]
PrimaryLane = Literal["fast_preview", "balanced", "coding_primary", "high_assurance"]
PlanningMode = Literal["direct", "plan_first", "plan_then_execute"]
ContinuityMode = Literal["none", "light_resume", "resume_if_possible", "strong_resume"]
UserVisibleMode = Literal["preview", "assistant", "coding_assistant"]


CODING_SIGNAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("stack_trace", "traceback"),
    ("stack_trace", "exception"),
    ("stack_trace", "stack trace"),
    ("stack_trace", "error:"),
    ("stack_trace", "line "),
    ("repo_language", "repo"),
    ("repo_language", "repository"),
    ("repo_language", "branch"),
    ("repo_language", "commit"),
    ("repo_language", "pull request"),
    ("repo_language", "pr "),
    ("repo_language", "diff"),
    ("code_change", "patch"),
    ("code_change", "refactor"),
    ("code_change", "fix"),
    ("code_change", "bug"),
    ("code_change", "test"),
    ("code_change", "pytest"),
    ("code_change", "debug"),
    ("code_change", "failing"),
    ("code_change", "module"),
    ("code_change", "function"),
    ("code_change", "class "),
    ("code_change", "endpoint"),
    ("code_change", "schema"),
    ("code_change", "migration"),
    ("code_change", "runtime"),
    ("code_change", "config"),
    ("stack_hint", ".py"),
    ("stack_hint", ".ts"),
    ("stack_hint", ".tsx"),
    ("stack_hint", ".js"),
    ("stack_hint", ".jsx"),
    ("stack_hint", ".sql"),
    ("stack_hint", "fastapi"),
    ("stack_hint", "django"),
    ("stack_hint", "react"),
    ("stack_hint", "next.js"),
    ("stack_hint", "postgres"),
    ("stack_hint", "sqlite"),
    ("stack_hint", "docker"),
    ("stack_hint", "kubernetes"),
    ("stack_hint", "terraform"),
)

NONCODING_SIGNAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("writing", "write"),
    ("writing", "draft"),
    ("writing", "summarize"),
    ("writing", "summarise"),
    ("support", "customer reply"),
    ("support", "email"),
    ("support", "message"),
    ("support", "memo"),
    ("analysis", "strategy"),
    ("analysis", "roadmap"),
    ("analysis", "proposal"),
    ("analysis", "pricing"),
)

HIGH_RISK_TERMS = (
    "auth",
    "authentication",
    "session",
    "payment",
    "billing",
    "referral",
    "admin",
    "migration",
    "schema",
    "delete ",
    "drop ",
    "destroy",
    "rollback",
    "runtime cutover",
    "production",
    "security",
)

MEDIUM_RISK_TERMS = (
    "release",
    "deploy",
    "checkout",
    "login",
    "signup",
    "wallet",
    "ledger",
    "webhook",
    "checkout",
    "database",
    "runtime",
    "orchestr",
)

LONG_CONTEXT_TERMS = (
    "entire repo",
    "whole repo",
    "across the codebase",
    "full context",
    "large context",
    "multiple files",
    "end-to-end",
    "full runtime",
)

CONTINUITY_TERMS = (
    "continue",
    "follow up",
    "follow-up",
    "same task",
    "resume",
    "pick up",
    "again",
    "next step",
)


@dataclass(frozen=True)
class RequestAssessment:
    task_type: TaskType
    difficulty: Difficulty
    risk_level: RiskLevel
    needs_repo_context: bool
    needs_long_context: bool
    needs_staged_execution: bool
    needs_silent_qc: bool
    surface: Surface
    continuity_candidate: bool
    language_preference: LanguagePreference
    coding_signals: list[str]
    noncoding_signals: list[str]


@dataclass(frozen=True)
class ExecutionStrategy:
    task_type: TaskType
    difficulty: Difficulty
    primary_lane: PrimaryLane
    silent_qc: bool
    staged_execution: bool
    planning_mode: PlanningMode
    continuity_mode: ContinuityMode
    needs_repo_context: bool
    needs_long_context: bool
    risk_level: RiskLevel
    profile_updates: dict[str, Any]
    user_visible_mode: UserVisibleMode


@dataclass(frozen=True)
class AgentProfileUpdate:
    profile_fields: dict[str, Any]
    learned_hints: dict[str, Any]


@dataclass(frozen=True)
class RuntimeExecutionPlan:
    task_type: TaskType
    visible_mode: Literal["fast", "smart", "assured"]
    primary_execution_profile: str
    fallback_execution_profile: str | None
    quality_check: bool
    premium_escalated: bool
    status_label: str
    source_surface: str


def _now_ts() -> int:
    return int(datetime.utcnow().timestamp())


def _trim_window(events: list[int]) -> list[int]:
    cutoff = int((datetime.utcnow() - timedelta(days=ROLLING_WINDOW_DAYS)).timestamp())
    return [value for value in events if value >= cutoff]


def _append_event(hints: dict[str, Any], key: str) -> None:
    values = list(hints.get(key, []))
    values.append(_now_ts())
    hints[key] = _trim_window(values)


def _normalize_surface(surface: str | None) -> Surface:
    normalized = (surface or "api").strip().lower()
    if normalized in {"try", "demo", "preview"}:
        return "try"
    if normalized in {"terminal", "ab_cli", "terminal_api", "terminal_compat"}:
        return "terminal"
    return "api"


def _flatten_messages(messages: list[Any]) -> tuple[str, list[str]]:
    user_parts: list[str] = []
    system_parts: list[str] = []
    for item in messages:
        role = None
        content = None
        if hasattr(item, "role"):
            role = getattr(item, "role", None)
            content = getattr(item, "content", None)
        elif isinstance(item, dict):
            role = item.get("role")
            content = item.get("content")
        if not isinstance(content, str):
            continue
        if role == "user":
            user_parts.append(content)
        elif role == "system":
            system_parts.append(content)
    prompt = "\n".join(part.strip() for part in user_parts if part.strip())
    return prompt, system_parts


def _derive_stack_hint(prompt: str) -> str:
    lowered = prompt.lower()
    if any(term in lowered for term in ["python", "pytest", "pip", "fastapi", "django", ".py"]):
        return "python"
    if any(term in lowered for term in ["typescript", "javascript", "node", "react", "next.js", ".ts", ".tsx", ".js"]):
        return "typescript"
    if any(term in lowered for term in ["sql", "postgres", "mysql", "sqlite"]):
        return "sql"
    if any(term in lowered for term in ["go ", "golang"]):
        return "go"
    if any(term in lowered for term in ["rust", "cargo"]):
        return "rust"
    return "general"


def _infer_language_preference(prompt: str, profile: AgentProfile | None, surface: Surface) -> LanguagePreference:
    hints = dict(profile.learned_hints_json or {}) if profile else {}
    if surface == "try":
        return "en"
    if any("\u4e00" <= ch <= "\u9fff" for ch in prompt):
        return "zh"
    stored = str(hints.get("english_response_preference") or "").lower()
    if stored == "en":
        return "en"
    if str(hints.get("language_preference") or "").lower() in {"python", "typescript", "sql", "go", "rust"}:
        return "en"
    return "en"


def _collect_signals(prompt: str, patterns: tuple[tuple[str, str], ...], *, include_code_markers: bool = False) -> list[str]:
    lowered = prompt.lower()
    signals: list[str] = []
    for label, term in patterns:
        if term in lowered and label not in signals:
            signals.append(label)
    if include_code_markers and "```" in prompt and "code_block" not in signals:
        signals.append("code_block")
    if include_code_markers and "/" in prompt and "path" not in signals:
        signals.append("path")
    return signals


def _detect_task_type(coding_signals: list[str], noncoding_signals: list[str]) -> TaskType:
    if coding_signals and noncoding_signals:
        return "mixed"
    if coding_signals:
        return "coding"
    return "general"


def _needs_repo_context(prompt: str, coding_signals: list[str], workspace_context: dict[str, Any] | None) -> bool:
    lowered = prompt.lower()
    if workspace_context and any(workspace_context.get(key) for key in ("repo_path", "repo_name", "branch", "workspace_fingerprint")):
        return True
    repo_terms = ("repo", "repository", "module", "branch", "commit", "diff", "file ", "folder ", "workspace")
    return bool(coding_signals) and any(term in lowered for term in repo_terms)


def _needs_long_context(prompt: str, difficulty: Difficulty, workspace_context: dict[str, Any] | None) -> bool:
    lowered = prompt.lower()
    if any(term in lowered for term in LONG_CONTEXT_TERMS):
        return True
    if workspace_context and workspace_context.get("large_repo"):
        return True
    return difficulty == "hard"


def _difficulty_for_prompt(
    prompt: str,
    task_type: TaskType,
    coding_signals: list[str],
    needs_repo_context: bool,
    continuity_candidate: bool,
) -> Difficulty:
    lowered = prompt.lower()
    breadth_markers = sum(
        1
        for term in (
            "across",
            "multiple",
            "full",
            "entire",
            "end-to-end",
            "migration",
            "cutover",
            "orchestr",
            "session",
            "payment",
            "auth",
            "admin",
            "referral",
            "schema",
        )
        if term in lowered
    )
    if task_type == "coding" and ("```" in prompt or len(coding_signals) >= 4):
        breadth_markers += 1
    if needs_repo_context:
        breadth_markers += 1
    if continuity_candidate:
        breadth_markers += 1
    if breadth_markers >= 4:
        return "hard"
    if breadth_markers >= 2 or task_type == "mixed":
        return "medium"
    return "simple"


def _risk_for_prompt(prompt: str, surface: Surface) -> RiskLevel:
    lowered = prompt.lower()
    if any(term in lowered for term in HIGH_RISK_TERMS):
        return "high"
    if any(term in lowered for term in MEDIUM_RISK_TERMS):
        return "normal"
    if surface == "terminal" and any(term in lowered for term in ("debug", "patch", "refactor", "test", "runtime")):
        return "normal"
    return "low"


def assess_request(
    messages: list[Any],
    user_profile: AgentProfile | None,
    surface: str,
    session_context: dict[str, Any] | None = None,
    workspace_context: dict[str, Any] | None = None,
) -> RequestAssessment:
    normalized_surface = _normalize_surface(surface)
    prompt, _ = _flatten_messages(messages)
    coding_signals = _collect_signals(prompt, CODING_SIGNAL_PATTERNS, include_code_markers=True)
    noncoding_signals = _collect_signals(prompt, NONCODING_SIGNAL_PATTERNS)
    continuity_candidate = bool(session_context and any(session_context.get(key) for key in ("task_id", "last_task_id", "resume_task"))) or any(
        term in prompt.lower() for term in CONTINUITY_TERMS
    )
    needs_repo_context = _needs_repo_context(prompt, coding_signals, workspace_context)
    task_type = _detect_task_type(coding_signals, noncoding_signals)
    difficulty = _difficulty_for_prompt(prompt, task_type, coding_signals, needs_repo_context, continuity_candidate)
    risk_level = _risk_for_prompt(prompt, normalized_surface)
    needs_long_context = _needs_long_context(prompt, difficulty, workspace_context)
    needs_staged_execution = risk_level == "high" or (normalized_surface == "terminal" and difficulty == "hard")
    needs_silent_qc = risk_level == "high" or difficulty == "hard" or (
        normalized_surface == "terminal" and task_type in {"coding", "mixed"} and difficulty == "medium"
    )
    return RequestAssessment(
        task_type=task_type,
        difficulty=difficulty,
        risk_level=risk_level,
        needs_repo_context=needs_repo_context,
        needs_long_context=needs_long_context,
        needs_staged_execution=needs_staged_execution,
        needs_silent_qc=needs_silent_qc,
        surface=normalized_surface,
        continuity_candidate=continuity_candidate,
        language_preference=_infer_language_preference(prompt, user_profile, normalized_surface),
        coding_signals=coding_signals,
        noncoding_signals=noncoding_signals,
    )


def build_execution_strategy(
    assessment: RequestAssessment,
    user_profile: AgentProfile | None,
    session_context: dict[str, Any] | None = None,
    workspace_context: dict[str, Any] | None = None,
) -> ExecutionStrategy:
    learned_hints = dict(user_profile.learned_hints_json or {}) if user_profile else {}
    if assessment.surface == "try":
        primary_lane: PrimaryLane = "balanced" if assessment.difficulty != "simple" else "fast_preview"
        continuity_mode: ContinuityMode = "light_resume" if assessment.continuity_candidate else "none"
        return ExecutionStrategy(
            task_type=assessment.task_type,
            difficulty=assessment.difficulty,
            primary_lane=primary_lane,
            silent_qc=assessment.risk_level == "high",
            staged_execution=False,
            planning_mode="direct",
            continuity_mode=continuity_mode,
            needs_repo_context=False,
            needs_long_context=False,
            risk_level=assessment.risk_level,
            profile_updates={
                "language_preference": "en",
                "recent_task_pattern": assessment.task_type,
                "surface_preference": "try",
            },
            user_visible_mode="preview",
        )

    execution_bias = str(learned_hints.get("execution_bias") or "execute_first")
    plan_first_bias = execution_bias == "plan_first"
    continuity_mode: ContinuityMode = "resume_if_possible" if assessment.continuity_candidate else "none"
    planning_mode: PlanningMode = "direct"
    primary_lane: PrimaryLane = "balanced"
    user_visible_mode: UserVisibleMode = "assistant"

    if assessment.task_type in {"coding", "mixed"}:
        primary_lane = "coding_primary"
        user_visible_mode = "coding_assistant"
        continuity_mode = "strong_resume" if assessment.continuity_candidate and assessment.risk_level == "high" else (
            "resume_if_possible" if assessment.continuity_candidate else "resume_if_possible"
        )
        if assessment.difficulty == "medium":
            planning_mode = "plan_first" if plan_first_bias or assessment.risk_level != "low" else "direct"
        if assessment.difficulty == "hard":
            planning_mode = "plan_then_execute"
    else:
        primary_lane = "high_assurance" if assessment.risk_level == "high" else "balanced"
        planning_mode = "plan_first" if assessment.difficulty != "simple" else "direct"

    if assessment.risk_level == "high":
        primary_lane = "high_assurance" if assessment.task_type == "general" else "coding_primary"
        planning_mode = "plan_then_execute"
        continuity_mode = "strong_resume" if assessment.continuity_candidate else "resume_if_possible"

    return ExecutionStrategy(
        task_type=assessment.task_type,
        difficulty=assessment.difficulty,
        primary_lane=primary_lane,
        silent_qc=assessment.needs_silent_qc or assessment.risk_level == "high",
        staged_execution=assessment.needs_staged_execution or assessment.risk_level == "high",
        planning_mode=planning_mode,
        continuity_mode=continuity_mode,
        needs_repo_context=assessment.needs_repo_context,
        needs_long_context=assessment.needs_long_context,
        risk_level=assessment.risk_level,
        profile_updates={
            "language_preference": assessment.language_preference,
            "recent_task_pattern": assessment.task_type,
            "surface_preference": assessment.surface,
            "stability_preference": "stability_first" if assessment.risk_level == "high" else "balanced",
            "execution_bias": "plan_first" if planning_mode != "direct" else execution_bias,
            "workspace_fingerprint": (workspace_context or {}).get("workspace_fingerprint"),
        },
        user_visible_mode=user_visible_mode,
    )


def runtime_plan_for_strategy(strategy: ExecutionStrategy, surface: str) -> RuntimeExecutionPlan:
    normalized_surface = _normalize_surface(surface)
    if normalized_surface == "try":
        if strategy.primary_lane == "fast_preview":
            return RuntimeExecutionPlan(
                task_type=strategy.task_type,
                visible_mode="fast",
                primary_execution_profile="remote_fast",
                fallback_execution_profile="remote_balanced",
                quality_check=False,
                premium_escalated=False,
                status_label="In progress",
                source_surface="try",
            )
        return RuntimeExecutionPlan(
            task_type=strategy.task_type,
            visible_mode="smart",
            primary_execution_profile="remote_balanced",
            fallback_execution_profile="remote_fast",
            quality_check=False,
            premium_escalated=False,
            status_label="In progress",
            source_surface="try",
        )

    if strategy.primary_lane == "coding_primary":
        return RuntimeExecutionPlan(
            task_type=strategy.task_type,
            visible_mode="assured" if strategy.risk_level == "high" else "smart",
            primary_execution_profile="remote_balanced",
            fallback_execution_profile="premium_anthropic" if strategy.silent_qc else None,
            quality_check=strategy.silent_qc,
            premium_escalated=strategy.risk_level == "high",
            status_label="Verified" if strategy.risk_level == "high" else ("Checked" if strategy.silent_qc else "In progress"),
            source_surface="terminal",
        )
    if strategy.primary_lane == "high_assurance":
        return RuntimeExecutionPlan(
            task_type=strategy.task_type,
            visible_mode="assured",
            primary_execution_profile="premium_anthropic",
            fallback_execution_profile="remote_balanced",
            quality_check=True,
            premium_escalated=True,
            status_label="Verified",
            source_surface="terminal",
        )
    if strategy.primary_lane == "fast_preview":
        return RuntimeExecutionPlan(
            task_type=strategy.task_type,
            visible_mode="fast",
            primary_execution_profile="remote_fast",
            fallback_execution_profile="remote_balanced",
            quality_check=False,
            premium_escalated=False,
            status_label="In progress",
            source_surface="terminal",
        )
    return RuntimeExecutionPlan(
        task_type=strategy.task_type,
        visible_mode="smart",
        primary_execution_profile="remote_balanced",
        fallback_execution_profile="premium_anthropic" if strategy.silent_qc else None,
        quality_check=strategy.silent_qc,
        premium_escalated=False,
        status_label="Checked" if strategy.silent_qc else "In progress",
        source_surface="terminal",
    )


def hydrate_profile_for_request(
    profile: AgentProfile,
    prompt: str,
    source_surface: str | None,
    workspace_context: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> AgentProfile:
    hints = dict(profile.learned_hints_json or {})
    bootstrapped = bool(hints.get("profile_bootstrapped_at"))
    lowered = prompt.lower()
    stack_hint = _derive_stack_hint(prompt)
    explanation_dense = any(term in lowered for term in ("explain", "walk me through", "teach", "why"))
    debug_heavy = any(term in lowered for term in ("debug", "trace", "error", "failing test", "stack trace"))
    coding_work = any(
        label in _collect_signals(prompt, CODING_SIGNAL_PATTERNS, include_code_markers=True)
        for label in ("code_change", "repo_language", "stack_trace", "code_block")
    )
    plan_first = any(term in lowered for term in ("plan", "outline", "strategy", "approach first"))
    if not bootstrapped:
        hints["profile_bootstrapped_at"] = _now_ts()
        hints["returning_session_count"] = 0
    else:
        hints["returning_session_count"] = int(hints.get("returning_session_count", 0)) + 1
    hints["profile_state"] = "returning" if bootstrapped else "new"
    hints["last_surface"] = source_surface or "terminal"
    hints["stack_hint"] = stack_hint
    hints["repo_type"] = "coding" if coding_work else "general"
    hints["language_preference"] = "en"
    hints["output_style"] = "concise"
    hints["patch_granularity"] = "surgical"
    hints["debug_preference"] = "reproduce_first" if debug_heavy else "balanced"
    hints["explanation_preference"] = "detailed" if explanation_dense else "concise"
    hints["testing_preference"] = "run_relevant_tests" if coding_work else "light_checks"
    hints["execution_bias"] = "plan_first" if plan_first else "execute_first"
    hints["stability_preference"] = "balanced"
    hints["last_prompt_excerpt"] = prompt[:255]
    if session_id:
        hints["last_session_id"] = session_id
    if workspace_context:
        if workspace_context.get("workspace_fingerprint"):
            hints["last_known_workspace_fingerprint"] = workspace_context["workspace_fingerprint"]
            profile.last_known_workspace_fingerprint = workspace_context["workspace_fingerprint"]
        if workspace_context.get("repo_type"):
            hints["repo_type_hint"] = workspace_context["repo_type"]
    recent_patterns = list(hints.get("recent_task_patterns", []))
    recent_patterns.append("coding" if coding_work else "general")
    hints["recent_task_patterns"] = recent_patterns[-8:]
    profile.recent_task_patterns_json = recent_patterns[-8:]
    profile.workload_pattern = "coding" if coding_work else "general"
    profile.pacing_context = "plan_first" if plan_first else "steady"
    profile.surface_preferences_json = dict(hints.get("surface_preferences") or {})
    profile.learned_hints_json = hints
    return profile


def get_or_create_agent_profile(db: Session, user_id: int) -> AgentProfile:
    profile = db.scalar(select(AgentProfile).where(AgentProfile.user_id == user_id))
    if profile:
        return profile
    user = db.get(User, user_id)
    preferred_mode = user.preferred_mode if user else "smart"
    profile = AgentProfile(
        user_id=user_id,
        preferred_mode=preferred_mode,
        default_provider_family="ds",
        workload_pattern="general",
        escalation_sensitivity="balanced",
        qa_preference="adaptive",
        cost_guardrail_band="standard",
        stable_task_bias="enabled",
        pacing_context="steady",
        last_successful_provider="remote_balanced",
        recent_premium_trigger_count=0,
        recent_ds_success_rate=1.0,
        fallback_count=0,
        qa_trigger_count=0,
        fallback_count_7d=0,
        qa_trigger_rate_7d=0.0,
        stable_task_completion_rate_7d=1.0,
        ds_clean_success_count_7d=0,
        premium_escalation_count_7d=0,
        last_execution_profile="remote_balanced",
        last_known_workspace_fingerprint=None,
        recent_task_patterns_json=[],
        surface_preferences_json={},
        last_strategy_json=None,
        learned_hints_json={
            "observed_high_risk_turns": 0,
            "stable_task_bias": "enabled",
            "fallback_count": 0,
            "qa_trigger_count": 0,
            "stable_task_count": 0,
            "ds_clean_success_count": 0,
            "ds_attempt_count": 0,
            "premium_events_7d": [],
            "fallback_events_7d": [],
            "qa_events_7d": [],
            "stable_task_events_7d": [],
            "task_events_7d": [],
            "ds_clean_success_events_7d": [],
            "ds_attempt_events_7d": [],
            "surface_preferences": {},
        },
    )
    db.add(profile)
    db.flush()
    return profile


def strategy_summary(strategy: ExecutionStrategy) -> dict[str, Any]:
    return asdict(strategy)


def update_profile_after_turn(
    profile: AgentProfile,
    turn_input: dict[str, Any],
    turn_output: dict[str, Any],
    strategy: ExecutionStrategy,
    observed_signals: dict[str, Any],
) -> AgentProfileUpdate:
    hints = dict(profile.learned_hints_json or {})
    _append_event(hints, "task_events_7d")
    if observed_signals.get("task_stable"):
        _append_event(hints, "stable_task_events_7d")
    if observed_signals.get("premium_escalated"):
        _append_event(hints, "premium_events_7d")
        profile.recent_premium_trigger_count += 1
    if observed_signals.get("fallback_used"):
        _append_event(hints, "fallback_events_7d")
        profile.fallback_count += 1
    if strategy.silent_qc:
        _append_event(hints, "qa_events_7d")
        profile.qa_trigger_count += 1
    if observed_signals.get("provider_family") != "premium_anthropic":
        _append_event(hints, "ds_attempt_events_7d")
        hints["ds_attempt_count"] = int(hints.get("ds_attempt_count", 0)) + 1
    if observed_signals.get("ds_succeeded_cleanly"):
        _append_event(hints, "ds_clean_success_events_7d")
        hints["ds_clean_success_count"] = int(hints.get("ds_clean_success_count", 0)) + 1

    profile.last_task_id = str(turn_input.get("task_id") or profile.last_task_id or "")
    profile.preferred_mode = "assured" if strategy.primary_lane == "high_assurance" else ("fast" if strategy.primary_lane == "fast_preview" else "smart")
    profile.last_successful_provider = str(observed_signals.get("provider_family") or profile.last_successful_provider or "remote_balanced")
    profile.last_execution_profile = str(observed_signals.get("execution_profile") or profile.last_execution_profile or "remote_balanced")
    profile.workload_pattern = strategy.task_type
    profile.pacing_context = strategy.planning_mode
    hints["last_strategy"] = strategy_summary(strategy)
    profile.last_strategy_json = strategy_summary(strategy)
    hints["last_user_visible_mode"] = strategy.user_visible_mode
    hints["last_surface"] = turn_input.get("surface") or hints.get("last_surface", "terminal")
    hints["surface_preferences"] = dict(hints.get("surface_preferences") or {})
    hints["surface_preferences"][turn_input.get("surface") or "terminal"] = {
        "language_preference": strategy.profile_updates.get("language_preference", "en"),
        "execution_bias": strategy.profile_updates.get("execution_bias", "execute_first"),
    }
    profile.surface_preferences_json = hints["surface_preferences"]
    hints["english_response_preference"] = "en"
    recent_patterns = list(hints.get("recent_task_patterns", []))
    recent_patterns.append(strategy.task_type)
    hints["recent_task_patterns"] = recent_patterns[-8:]
    profile.recent_task_patterns_json = recent_patterns[-8:]
    workspace_fingerprint = strategy.profile_updates.get("workspace_fingerprint")
    if workspace_fingerprint:
        profile.last_known_workspace_fingerprint = str(workspace_fingerprint)

    ds_attempts = max(int(hints.get("ds_attempt_count", 0)), 1)
    ds_successes = int(hints.get("ds_clean_success_count", 0))
    profile.recent_ds_success_rate = round(ds_successes / ds_attempts, 3)
    profile.fallback_count_7d = len(_trim_window(list(hints.get("fallback_events_7d", []))))
    qa_events = len(_trim_window(list(hints.get("qa_events_7d", []))))
    task_events = max(len(_trim_window(list(hints.get("task_events_7d", [])))), 1)
    profile.qa_trigger_rate_7d = round(qa_events / task_events, 3)
    stable_events = len(_trim_window(list(hints.get("stable_task_events_7d", []))))
    profile.stable_task_completion_rate_7d = round(stable_events / task_events, 3)
    profile.ds_clean_success_count_7d = len(_trim_window(list(hints.get("ds_clean_success_events_7d", []))))
    profile.premium_escalation_count_7d = len(_trim_window(list(hints.get("premium_events_7d", []))))
    hints["last_turn_input"] = {
        "prompt_excerpt": str(turn_input.get("prompt", ""))[:255],
        "surface": turn_input.get("surface"),
        "task_id": turn_input.get("task_id"),
    }
    hints["last_turn_output"] = {
        "reply_excerpt": str(turn_output.get("reply", ""))[:255],
        "status": observed_signals.get("status_label"),
    }
    profile.learned_hints_json = hints
    return AgentProfileUpdate(
        profile_fields={
            "preferred_mode": profile.preferred_mode,
            "last_successful_provider": profile.last_successful_provider,
            "last_execution_profile": profile.last_execution_profile,
            "workload_pattern": profile.workload_pattern,
            "pacing_context": profile.pacing_context,
        },
        learned_hints=hints,
    )
