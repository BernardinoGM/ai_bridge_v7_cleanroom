# Architecture Notes

## Serving architecture

AI Bridge is built as a routing layer over multiple serving lanes:

- Fast lane: remote commercial-grade provider for low-friction throughput.
- Smart lane: remote balanced provider for most default work.
- Assured lane: remote premium provider for high-risk and release-critical work.

The public product stays `Fast`, `Smart`, and `Assured`. Provider and serving math remain internal. Railway hosts only the app layer in the launch version.

## Launch deployment boundary

Launch production boundaries are strict:

- Railway runs the application layer only.
- Production inference uses remote commercial-grade providers only.
- The founder laptop is not a serving node and is never part of the production path.
- Consumer VPN products are not part of production routing.
- Local or self-hosted inference remains future-ready in the provider abstraction and is disabled by default.

## Task continuity model

`/v1/messages` uses a task continuity layer:

- `agent_profiles` stores exactly one lightweight logical agent binding per user.
- `task_sessions` stores task state, pinned lane, continuity status, and timeout window.
- `task_turns` stores turn summaries for continuity without building a heavy memory system.
- New tasks pick an initial lane from task continuity first, then agent profile, then requested mode, then system default.
- Ongoing tasks keep their pinned lane unless explicitly escalated, de-escalated, timed out, or recovered after failure.

This favors continuity and trust over clever turn-by-turn rerouting.

## Agent profile fields

The user-bound agent profile keeps structured routing hints only:

- preferred mode
- default provider family
- escalation sensitivity
- QA preference
- cost guardrail band
- workload pattern
- stable task bias
- last successful provider
- recent premium trigger count
- recent DS success rate
- learned hints JSON

This is a logical decision layer backed by database state, not a per-user long-running process.

## Efficiency and cost-control loop

After each turn the bound agent profile updates:

- last successful provider
- recent DS success rate
- recent premium trigger count
- fallback count
- QA trigger count
- fallback count 7d
- QA trigger rate 7d
- stable task completion rate 7d
- DS clean success count 7d
- premium escalation count 7d
- last execution profile

Those signals feed future decisions so stable users and stable tasks stay on the DS-first path more often, repeated QA can be reduced on low-risk follow-ups, and premium escalation is reserved for cases where the history or task risk justifies it.

## Internal-only routing telemetry

Route telemetry remains internal and admin-only:

- route chosen
- premium escalation rate
- fallback rate
- retry rate
- serving cost estimate
- benchmark estimate
- quality check trigger rate
- latency

Standard user surfaces do not show backend model names, route traces, benchmark values, or complexity labels.

## Serving COGS

Serving COGS is treated as an internal variable-cost estimate that can include:

- third-party model API cost
- future local GPU or inference serving cost when enabled outside the launch path
- fallback cost
- retry cost
- network or bandwidth cost
- training amortization when applicable

## Benchmark and discipline zones

Benchmark is an internal measuring stick derived from configured premium-model input and output rates. It is never used as the public invoice formula.

Internal reporting should highlight:

- target zone: blended Serving COGS <= 10% of benchmark
- healthy zone: <= 15%
- red line ceiling: <= 25%

## Network reality

Production routing must use commercial-grade provider connectivity. Consumer VPN products are not part of the production architecture because they introduce fragility, latency variance, and avoidable operational risk.
