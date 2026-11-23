# Strategies: product ownership and acceptance

## Working agreement

The user is the CPO; the assistant acts as Director of Product for this work. Own routine product, engineering and presentation choices. Report major product milestones and findings, not a running list of implementation steps. Ask the CPO only for material architectural/product decisions that cannot reasonably be resolved from the agreed goals, or when an explicit external approval requirement applies. Put detailed evidence and status in the Notion roadmap.

Keep the page research-only. Page completeness does not certify a strategy, change trading rules, authorize trades or establish a full historical S&P edge. Use free local tests; no paid model or Lovable AI calls. Production publication follows the applicable approved release scope.

## Purpose

An investor should be able to choose a saved experiment, compare all policies under the same assumptions, inspect the account path, assess different periods, understand the controls and limitations, and share/export the evidence.

## Acceptance for the research workspace

- All six policies compared in one exact backend scope; no cross-period, cross-funding or cross-version mixing.
- Keep all 37 windows, both study universes, three funding options and both fills. Defaults unchanged. No parameter search or new simulations.
- Saved year-end account curves, including matched SPY and annual controls; label deposits, annual sampling and log scale clearly. Never invent daily equity curves from annual returns.
- Total/annualized return, ending balance, calendar-year comparisons and intrayear maximum drawdown remain distinct.
- Five existing nonoverlapping five-year blocks shown separately from the other overlapping sensitivity windows. Counts describe blocks or years, not stocks or independent experiments.
- Shareable selection URLs survive reload; invalid selections are withheld, not silently replaced. Shared views use current published evidence; CSVs include the artifact hash.
- Comparison, annual-return and period CSV exports preserve backend values and scope. No automatic paid calls or regeneration.
- Explain SPY, matched annual selection, rotate/hold, compounding, funding and missing historical coverage in plain language.
- Narrow-screen tables scroll inside the page; chart labels remain readable; semantic headings, native inputs, accessible chart descriptions and exact data tables are available.
- Loading, unavailable, incomplete and version-mismatch states are explicit and offer manual recovery. No false result fallback.
- Public backend endpoints remain read-only with no account/DB/model access. Existing auth/private behavior is unchanged.

## Evidence

The workspace adds saved annual closing balances to the 2,664 existing result records, preserving every prior financial field. Reproduction: `work/strategies-workspace/enrich.py` in the research workspace; its input manifest records the previously published artifact, archived annual records, and 888 upfront-account ledgers. Backend comparison and period APIs derive bounded views from that immutable artifact.

An exact source snapshot is included at `research_artifacts/workspace-enrichment-source.py`. To reproduce, place that snapshot back at the original `work/strategies-workspace/enrich.py` path in the archived research workspace, with the referenced frozen CSVs/ledgers available. It is evidence-generation source, not an API runtime job. No downloads, new simulations or paid calls occur in the enrichment.

Live publication must verify the exact artifact hash, all six policies, matched controls, unknown-scope handling, interactive filters/share links and hosted build; keep the Notion roadmap current with release commits and limitations.
