# CHANGELOG_REBUILD

## v7 clean-room rebuild

- Rebuilt AI Bridge in a new standalone folder with no writes to Golden Coin or any existing project files.
- Reset public product model around `Fast`, `Smart`, and `Assured`.
- Removed public cost-plus, take-rate, savings-split, reward-wallet, cashback-loop, and perpetual referral assumptions.
- Added pack-based top-up model with Starter, Growth, and Scale.
- Added Stripe checkout architecture with verified webhooks and idempotent crediting.
- Added closed-loop first-topup referral perks with promo balance instead of withdrawable rewards.
- Added add-on architecture for Bill Guard, Team Vault, Priority Queue, Custom Routing Rules, and Analytics Pro.
- Shifted dashboard from token-centric reporting to runway-centric reporting.
- Added internal-only benchmark and serving COGS telemetry with cost discipline zone support.
- Added task continuity with task-pinned lanes for `/v1/messages`.
- Added lightweight per-user agent profiles to improve continuity and reduce unnecessary escalation.
- Hid internal routing details from standard users and moved route telemetry behind admin-only access.
- Added provider abstraction ready for future local or other self-hosted inference.
- Documented Railway deployment and the explicit non-requirement for consumer VPN in production.
