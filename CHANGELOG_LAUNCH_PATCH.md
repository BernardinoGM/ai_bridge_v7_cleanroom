# CHANGELOG_LAUNCH_PATCH

- Replaced the production execution path with real provider adapters for local OpenAI-compatible serving, remote OpenAI-compatible serving, and Anthropic premium serving.
- Kept mock providers available only for testing and development through explicit configuration.
- Added task-level execution stickiness with `pinned_provider` and `pinned_execution_profile`.
- Replaced hardcoded serving cost multipliers with a pluggable serving cost estimator.
- Removed the hardcoded `/dashboard/1` landing CTA and switched to launch-safe preview actions.
- Updated Railway-facing configuration guidance and launch smoke tests.
