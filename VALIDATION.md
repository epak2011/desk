# Prospective market validation

This is a separate future-only experiment, not a rewrite of the eight-name trials or the pending 110-stock collector. It sends no broker orders, changes no production trading rule, and infers no discretionary allocation from text. Release and future-start registration are separate operations. Merging alone does not fabricate an experiment start.

## Delivered comparisons

Each portfolio starts with $100,000 and runs at both 10 and 25 bps per side:

| Portfolio | Policy |
|---|---|
| Full system | Explicit complete-policy target weights, with selection, sizing, risk and execution provenance |
| Selected passive | Top 20 selected names by frozen score at the first executable open of each month, 5% target each; unused slots remain cash |
| Technical timing | Existing technical entry and close-confirmed invalidation logic, with equal universe target slots when membership changes |
| Broad passive | Equal weight registered universe, monthly rebalance |
| SPY, QQQ | Registered buy-and-hold orders at inception |
| Simple trend | Price above 200-day average; equal universe target slots |
| MA-only entry shadow | Replaces the technical entry gates with price above the 50/200-day averages; retains stop/structural exits |
| No structural exit shadow | Keeps canonical entries and frozen close-confirmed stop; disables the structural-exit clause |

The MA-only variant replaces several entry conditions and is **not** a clean causal estimate of a single confirmation filter. Both shadows are exploratory, not automatically promoted. Technical portfolios use shared cash and explicit rebalance orders when active membership changes, rather than the old isolated sleeve accounting. Stops are close-confirmed, with next-open gap prices; no intraday stop fill is assumed. Full-system target weights must explicitly encode any reductions/overrides. Unchanged membership does not trigger daily technical rebalancing; incomplete fills are retried only through a newly recorded target. SPY/QQQ retain dividends as cash, earning the same disclosed SGOV cash proxy.

Full system minus selected passive includes sizing and rebalance-frequency differences. Technical timing minus broad passive is a useful diagnostic, not an exact decomposition of full-system alpha. Raw return, drawdown, volatility, beta and concentration must be considered together.

## Universe and important boundaries

`research/validation-template.json` starts with the disclosed 110-name current-survivor inception cohort. The code accepts more names; no eight- or 30-name cap applies. It does **not** pretend to have sourced a point-in-time 500-stock security master.

For an objective 500-name launch, supply `--universe-file` with `{ "source": "provider/export provenance", "records": [...] }`. Each security row requires `ticker`, `issuer_id`, timezone-aware `as_of`, `float_market_cap`, `price`, `median_dollar_volume_60`, `security_type`, `country`, `primary_listing`, and optionally `sector`. The importer rejects future data, filters US primary common stocks at $5 / $20m median dollar volume, selects the largest eligible class per issuer, ranks by float cap, and requires all 500 eligible names. The input hash and rule are registered.

The initial cohort stays fixed for this version. **Automatic quarterly membership refresh is not implemented.** Replacing it needs a separately registered version preserving original holdings and history. A missing/delisted held stock blocks accounting for explicit corporate-action reconciliation; it is never silently discarded or replaced. Cash acquisitions, bankruptcy recovery and symbol changes need reviewed price/action evidence before continuation. Yahoo prices and ex-date dividend cash are a paper approximation, not broker settlement or tax accounting. Sector-peer candidate diagnostics are available when dated sector labels exist; there is no size/style factor attribution or matched-beta causal claim.

## Launch

1. Install `requirements.txt`. Run `python -m unittest discover -s tests -q`.
2. Review the complete policy manifest if full-system measurement is desired: concrete versioned selection/ranking, sizing/cash, macro/risk overrides, execution and stop instructions. Freeze any model/prompt identities and required input receipts in it. The four required object sections are `selection`, `sizing`, `risk`, `execution`. A missing manifest leaves the two full-system selection arms visibly unavailable, while technical controls remain usable. There is no current authoritative full-portfolio policy producer to infer those choices automatically.
3. After release approval, register a **future US exchange session** with the production database. Example: `python scripts/run_validation.py register --starts-on YYYY-MM-DD --policy-file /reviewed/policy.json`. Add `--universe-file /dated/security-master.json` for the objective 500 cohort. Omit `--policy-file` only for an explicitly limited technical diagnostic. The source files, calculation dependency versions, policy, costs, universe, success gates and registration time are locked.
4. Capture the immediately preceding session's completed-close seed before the first open: `python scripts/run_validation.py collect`. Primary collection is at 23:15 UTC (08:15 the next day in Seoul), with recovery attempts at 02:15, 08:15 and 12:15 UTC, including weekends. These times permit delayed Friday data to seed Monday before its open. Each recorded decision keeps its actual arrival time; retries never backdate an order or overwrite a sealed snapshot. Do not register too late to capture this seed. A missing seed permanently flags the affected arm's record as incomplete.
5. Before each scheduled collection, the complete-policy producer submits its after-close receipt through owner-only `POST /v1/operator/validation/plans` or `python scripts/run_validation.py import-plan --plan-file /receipt.json`. Intake uses server arrival time, not the client's claimed arrival. Each section includes `policy_sha256` equal to the canonical SHA256 of its registered manifest section, plus the decision's input provenance. `policy_id` is the canonical hash of the full manifest. Candidate `selected` means the selector's decision **before portfolio risk/sizing vetoes**; post-veto allocation is in `target_weights`.
6. Every receipt requires `session`, timezone-aware `observed_at`, the frozen `policy_id`, all four provenance sections, `target_weights` summing to <=1, optional explicit `stops`, and exactly one candidate record for every eligible stock: `ticker`, finite `score`, boolean `selected`, nonempty acceptance/rejection `reason`, optional dated `sector`. Never send just the winning names. An explicit empty target object means cash; omission is an error. Full-system plans must cover the entire current allocation, including retained positions.

For isolated rehearsal, append `--sqlite /task-local/evidence.sqlite` to every command. This creates no production records. `status` prints the scoreboard; `export --output /private/evidence.json` exports archived inputs, decisions, fills, holdings, incidents and assessments. Such exports contain private portfolio information and should not be committed or attached publicly. Registration will not overwrite an active experiment. Runtime/source drift blocks collection rather than silently changing the method; use an explicitly reviewed parallel version when changes are needed. The current CLI intentionally does not provide an erase/reset operation.

The GitHub workflow can manually register a future date using a reviewed repository policy file, or collect an existing registration. Its schedule runs only on `main`. Before registration the job reports `not_registered` and creates no returns. Collection failures roll back the accounting transaction, preserve the prior ledger, append an operational incident in a fresh transaction and fail the workflow. GitHub's configured workflow notifications plus the owner incident view provide operational visibility; no email/Slack messages are sent by this code.

## Scoring and interface

Owner-only `GET /v1/operator/performance` adds `market_validation`. The Streamlit Rules Performance page displays the scoreboard, equity/drawdown curves, higher-cost sensitivity, accepted/rejected outcomes and integrity/incident details. Incomplete arms have null performance headlines and are excluded from equity charts. The Lovable frontend receives the new owner API field; its separate frontend still needs to render that field if it does not mirror the Streamlit page.

Each raw dataset is archived as compressed CSV with hashes. The append-only database evidence chain detects payload edits/conflicts and supports idempotent reruns; it is not a third-party timestamp or defense against a privileged administrator rewriting the whole chain. Database backups and access control remain necessary.

Daily scoreboard: capital-weighted return, SPY/QQQ excess, drawdown, annualized volatility, exposure, costs, turnover, holdings and unfilled orders. Rolling 63/126/252-session excess returns mature over time. Market-only beta/intercept and tracking error require 20 observations and are labeled descriptive, not factor-adjusted skill.

Accepted/rejected candidates use the first complete-policy snapshot per next-session month, with fixed 5/14/30-session gross returns from the next open and explicit maturity counts. Outcomes include splits/dividends; same-day ex dividends are not awarded to opening purchases. They overlap and are not independent trades. When sectors exist, peer excess uses contemporaneously recorded labels, not a future classification.

At 252 completed sessions, save a preliminary assessment; at 504, save the single primary confirmatory assessment. Proposed economic gate: >=2 percentage points annualized net excess versus SPY, and volatility plus maximum-drawdown magnitude each <=1.10 times SPY. Confirmatory evidence also needs the predeclared 95% moving-block bootstrap interval for relative wealth growth above zero (20-session blocks, 10,000 draws, fixed seed; 10/40-session sensitivities disclosed). No daily significance stopping or automatic winner promotion. Missing full-system observations block its verdict. The higher-cost arm is shown separately and must be reviewed before any deployment decision. Thresholds and source policy are registered before collection, not retrofitted to results. Two years may remain insufficient to establish an edge across market regimes.

## Actual execution reconciliation

`python scripts/run_validation.py import-executions --executions-file /broker-export.json` imports explicit fills only. Schema: `source`, immutable `statement_id`, `fills` with `fill_id`, `ticker`, `side` (`buy`/`sell`), `quantity`, `price`, optional `fee`, timezone-aware `executed_at`, and `decision_id` for matching. Partial fills aggregate to VWAP; comparisons require the same decision, ticker, side and US session. Unmatched quantities/orders remain visible; conflicting fill IDs fail. Private exports contain the detailed reconciliation; the owner summary exposes counts only.

This measures price/quantity execution differences. It does not import bank balances, transfers, starting positions or personal taxes, and therefore does not claim actual-account returns. No brokerage connection or trading authorization is requested or used.
