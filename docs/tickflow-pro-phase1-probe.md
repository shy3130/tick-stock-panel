# TickFlow Pro Phase 1 probe

Process-local rate-limit safety (80% RPM) is already on branch `cursor/tickflow-pro-rate-limit-p0`.

## Dry-run (safe anytime)

```bash
cd backend
python3 scripts/probe_tickflow_pro.py
```

Writes sanitized plan under `reports/tickflow_pro_probe/<timestamp>/` with status `DRY_RUN_OK`
(gates/plan only — not proof of SDK methods, network, or auth).

## Live probe (off-peak)

Uses real SDK namespaces aligned with the app: `tf.klines.batch` and `tf.quotes.get`.
Do not start multiple Phase 1 probe/sync processes concurrently.

Requirements:

- `TICKFLOW_API_KEY` in the environment (never commit the key)
- Prefer after **16:00 Asia/Shanghai** (Stage A off-peak vs Gold)
- Before 16:00 only with `--force`

```bash
cd backend
TICKFLOW_API_KEY=... python3 scripts/probe_tickflow_pro.py --live
```

Golden symbols: `000403.SZ`, `600489.SH`, `300059.SZ`.

## Out of scope

- Full-market backfill
- Gold Shadow deploy / Telegram ownership
- Claiming cross-product shared rate limits (still process-local)
