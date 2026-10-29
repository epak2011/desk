"""Single requested live generation; log only schema diagnostics, never prompts."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker

original = worker.get_decision_dossier
def observed(*args, **kwargs):
    result = original(*args, **kwargs)
    print({"generation_diagnostics": result.get("_generation_diagnostics"),
           "contract_issues": worker._dossier_contract_issues(result)})
    return result

worker.get_decision_dossier = observed
ticker = os.environ.get("VERIFY_TICKER", "BE")
if ticker not in {"BE", "DASH"}:
    raise ValueError("Unsupported verification ticker")
worker.refresh_full_report(ticker)
p = worker.backend.read_json_table("research_reports", ticker).get(ticker) or {}
dossier = p.get("dossier") or {}
a = dossier.get("independent_assessment") or {}
print({"ticker": ticker, "report_valid": not worker._dossier_contract_issues(dossier),
       "assessment_status": a.get("status"), "assessment_id": a.get("assessment_id"),
       "model": a.get("model")})
assert not worker._dossier_contract_issues(dossier)
if ticker == "DASH":
    assert a.get("status") == "ready", "Independent generation did not complete"
