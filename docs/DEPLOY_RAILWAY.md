# Railway deploy notes

## Environment

Set the variables from `.env.example`, then replace:

- `SECRET_KEY`
- `STRIPE_SECRET_KEY`
- `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `DATABASE_URL`
- `PROVIDER_FAST_API_KEY`
- `PROVIDER_REMOTE_API_KEY`
- `PROVIDER_PREMIUM_API_KEY`

For Railway production, prefer Postgres and set `DATABASE_URL` to the Railway Postgres connection string.
Keep `PROVIDER_LOCAL_ENABLED=false` for launch.

## Start command

Railway can use the included `railway.json`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

This target points to the clean-room app only.

## Health

- Health endpoint: `/api/health`

## Notes

- Production config validation fails loudly if Stripe secrets, admin key, or required provider API keys are missing.
- Streaming billing is intentionally disabled for launch to avoid underbilling or inaccurate debit behavior.
- Auto-reload is scaffolded and should remain feature-flagged until Stripe customer + payment-method flows are fully wired for production.
- Railway hosts the app layer only; production inference is remote-only.
- No production path depends on the founder laptop or a consumer VPN.
