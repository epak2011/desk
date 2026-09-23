"""Curated operator log for material Trading Desk engine changes.

Keep proposed work separate from deployed work. Every material rule change should
record its rationale, validation evidence, and production status here.
"""

from __future__ import annotations

from datetime import datetime, timezone


ENGINE_UPDATES_CONTRACT_VERSION = 1

RULES_UPDATES = (
    {
        "date": "2026-09-23",
        "title": "Complete frontend parity and deployment governance",
        "status": "Repository updated",
        "summary": (
            "Complete the shared page contracts, correct portfolio-aware sizing inputs, "
            "and prevent Streamlit/Lovable drift after future backend releases."
        ),
        "changes": (
            "Publish the Today daily workflow summary and mark every page contract shared.",
            "Normalize account size, per-trade risk, and position caps across legacy Streamlit and Lovable workspace shapes.",
            "Publish portfolio exposure, unallocated value, concentration flags, largest positions, and per-position decisions.",
            "Add universal freshness, refresh, stale-state, and data-as-of display instructions to response metadata.",
            "Fingerprint every page contract and publish required response keys for route-by-route frontend parity checks.",
            "Verify after every main-branch push that Render is on the exact revision and public Market/Analyze contracts remain complete.",
        ),
        "validation": "All 189 automated tests pass. This release changes presentation contracts and portfolio context, not the production trading rules.",
    },
    {
        "date": "2026-09-18",
        "title": "Historical shadow backfill and promotion governance",
        "status": "Repository updated",
        "summary": (
            "Evaluate the new entry filters on eligible historical decision snapshots and publish explicit evidence gates for human promotion review."
        ),
        "changes": (
            "Backfill entry-timing and regime-quality candidates only from frozen decision-time inputs.",
            "Re-score matured historical paths without downloading or substituting current market inputs.",
            "Require matched outcomes, changed decisions, cross-regime breadth, success lift, return lift, and no materially harmed regime.",
            "Allow the system to recommend human review but never promote a rule automatically.",
        ),
        "validation": "Promotion still requires all published evidence gates; no production action changes in this release.",
    },
    {
        "date": "2026-09-17",
        "title": "Entry timing and regime quality shadow gates",
        "status": "Shadow testing",
        "summary": (
            "Test whether stricter entry timing and stronger evidence requirements in "
            "mixed or unfavorable markets improve the weak Enter/Accumulate cohort without changing live calls."
        ),
        "changes": (
            "Add an entry-timing candidate that waits on any material extension warning.",
            "Add a regime-quality candidate requiring setup score at least 8 and reward/risk at least 2 in constrained regimes.",
            "Score filtered entries by the gain or loss the candidate avoided on the identical 14-session path.",
            "Keep every production action unchanged while evidence accumulates.",
        ),
        "validation": "All 175 automated tests pass. Promotion still requires mature, integrity-approved shadow outcomes across multiple regimes.",
    },
    {
        "date": "2026-09-11",
        "title": "Structural state and action consistency",
        "status": "Shadow testing",
        "summary": (
            "Remove contradictory TRENDING/TRANSITION state and AVOID combinations by "
            "giving structural classification and tactical action one shared definition."
        ),
        "changes": (
            "Add the missing lower-low structure penalty.",
            "Evaluate strict BROKEN conditions before TRANSITION.",
            "Distinguish recovering from weakening transition structures.",
            "Make structural AVOID consume the canonical structural state.",
        ),
        "validation": "Boundary tests are active and the candidate is logging beside production. Promotion still requires mature historical comparison evidence.",
    },
    {
        "date": "2026-09-10",
        "title": "Daily market context narration",
        "status": "Repository updated",
        "summary": (
            "Market context now uses the canonical regime inputs and a natural-language "
            "research layer while preserving the deterministic regime decision."
        ),
        "changes": (
            "Unified the canonical regime decision across app surfaces.",
            "Added specific daily context grounded in the current market inputs.",
            "Kept generated commentary separate from the rules-owned action.",
        ),
        "validation": "Automated regime-context and market-schedule checks cover the contract.",
    },
)


def engine_updates_payload() -> dict:
    """Return the presentation-neutral feed shared by every frontend."""
    return {
        "contract_version": ENGINE_UPDATES_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "updates": [
            {
                **entry,
                "changes": list(entry.get("changes") or ()),
            }
            for entry in RULES_UPDATES
        ],
        "count": len(RULES_UPDATES),
    }
