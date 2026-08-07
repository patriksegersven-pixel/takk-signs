# Google Ads — change infrastructure & rules of engagement

How changes to the Babyshop/Lekmer Google Ads accounts are made from Claude
sessions, and the approval policy that governs every change.

## THE RULE (non-negotiable)

**No write/mutation to any Google Ads account without explicit user approval
of the specific proposed change, in the current conversation.**

- Reads (GAQL queries, listings, simulations) — always allowed.
- `--validate-only` dry runs — always allowed (they write nothing).
- Actual mutations (tROAS changes, or anything else if tooling grows) —
  ONLY after presenting: current value → proposed value, the data behind
  the proposal, and receiving an explicit go-ahead. A general instruction
  like "optimize my campaigns" is NOT approval for specific values —
  propose first, wait for approval, then apply.
- After applying: verify by re-reading, and append to the change log below.

## The chain (how a change physically happens)

```
User (chat, any device)
  └─ Claude Code session (remote container)
       └─ scripts/ads/set_troas.py            stdlib-only CLI in this repo
            └─ auth: SA key JSON  ──────────  google-ads-mcp@project-a7ade44e-
               (env GOOGLE_ADS_SA_KEY_JSON,    e7e3-4871-a83.iam.gserviceaccount.com
                openssl-signed JWT → OAuth2)   Standard user on the MCC
                 └─ Google Ads REST API v24
                      └─ login-customer-id 6899790415 (Babyshop MCC 689-979-0415)
                           └─ client accounts (Babyshop SE/NO/DK/FI/ROW/RU,
                                               Lekmer SE/NO/DK)
```

Credentials & identifiers: see `CREDENTIALS.md`. Env vars live in the
claude.ai Claude Code environment settings; the developer token comes from
the MCC's API Center.

## Standard change workflow

1. **Read current state** — `set_troas.py list --customer-id <id>` (or a
   targeted GAQL query). Never propose against stale/remembered values.
2. **Gather evidence** — for tROAS levels, pull the live Google bid
   simulation (`campaign_simulation` / `bidding_strategy_simulation`,
   type TARGET_ROAS) and apply the GP3 math below; or cite whatever data
   motivates the change.
3. **Propose** — current → proposed, expected impact, source of the number.
4. **WAIT for approval.** No approval, no write. Ambiguous reply → ask.
5. **Dry run** — `set ... --validate-only` (server-side validation).
6. **Apply** — same command without the flag.
7. **Verify** — re-read the value from the API; report it back.
8. **Log** — append a row to the change log in this file and push.

## GP3 optimization math (from babyshop-roas-simulations.html, verbatim)

- Cost = cost_micros / 1e6 (account currency)
- **GP2 = conversions value** (primary conversion action already reports
  cart-level gross profit — do NOT multiply by a margin, that would deduct
  COGS twice)
- **GP3 = GP2 − cost**
- **iGP3 = GP2 × incrementality_factor − cost**; recommended target =
  simulated point with max iGP3
- Incrementality factors (pipeline/PIPELINE.md): pb-generic → 1.00;
  brand/pb-product campaigns are discounted (see PIPELINE.md table) —
  their raw GP3-max is NOT a valid recommendation, use iGP3
- Sanity band: cross-check against the targets in
  `apps/babyshop-dashboard/refresh_roas_impact.py` and the group bands in
  the roas-impact dashboard before proposing

## Scope & safety properties

- The SA has **Standard** access (not Admin): it can change bidding and
  campaigns but cannot manage users or billing.
- The CLI refuses `--campaign-id` on campaigns attached to a portfolio
  strategy — portfolio changes affect every attached campaign and must be
  made explicitly with `--strategy-id`.
- tROAS is a ratio (2.1 = 210%). The CLI accepts `210%` too and rejects
  suspiciously large ratios.
- Currently the only mutation the tooling supports is target ROAS. Any new
  mutation capability added to `set_troas.py` inherits THE RULE above.

## Change log

| Date | Account | Target | Change | Basis | Approved by |
|---|---|---|---|---|---|
| 2026-08-06 | Babyshop SE 4851485396 | campaign 21139965046 `p-shopping-se-pb-generic` | tROAS 2.5 → 2.1 | GP3-max of live bid simulation 2026-07-27→08-02 (189,316 vs 186,020 SEK/wk); consistent with pipeline band 2.0–2.2 | Patrik (chat request: "change ROAS level … to max gp3 roas level") |
