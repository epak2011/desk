"""Curated operator log for material Trading Desk engine changes.

Keep proposed work separate from deployed work. Every material rule change should
record its rationale, validation evidence, and production status here.
"""

from __future__ import annotations

from datetime import datetime, timezone


ENGINE_UPDATES_CONTRACT_VERSION = 1

RULES_UPDATES = (
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
