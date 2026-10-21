import unittest
from datetime import datetime, timedelta, timezone

from public_contract import analyze_page_payload, research_payload


class SavedPmSelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.memo = {"thesis": "Saved DASH business thesis", "drivers": ["Saved company driver"],
                     "risks": ["Saved business risk"], "valuation": "Saved valuation", "quality": {"tier": "A"},
                     "_source": "claude", "_worker_generated_at": self.now.isoformat(), "_revision_id": "saved-revision"}
        self.fallback = {"pm": {"thesis": "Rules fallback", "drivers": ["Tactical driver"],
                               "quality": {"tier": "B"}, "_source": "rules fallback"},
                         "dossier": {"_source": "rules fallback", "thesis": "Failed later dossier"},
                         "_worker_generated_at": (self.now + timedelta(minutes=1)).isoformat(),
                         "_earnings_date": "2020-01-01", "_rule_action": "avoid"}

    def test_saved_memo_beats_newer_fallback_and_retains_its_own_date(self):
        result = research_payload("DASH", report=self.fallback, memo=self.memo,
                                  market={"security_profile": {"earnings_date": "2027-01-01"}}, rule={"action": "watch"})
        self.assertEqual(result["thesis"], self.memo["thesis"])
        self.assertEqual(result["drivers"], self.memo["drivers"])
        self.assertEqual(result["quality"], {"tier": "A"})
        self.assertEqual(result["generated_at"], self.memo["_worker_generated_at"])
        self.assertEqual(result["provenance"], "claude")
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["full_report_available"])
        self.assertEqual(result["selection"]["storage"], "pm_memos")
        self.assertEqual(result["selection"]["revision_id"], "saved-revision")
        self.assertTrue(result["selection"]["newer_attempt_incomplete"])
        self.assertEqual(analyze_page_payload("DASH", research=result)["portfolio_manager"]["thesis"], self.memo["thesis"])

    def test_newer_saved_memo_beats_older_successful_dossier_without_splicing(self):
        report = {"dossier": {"_source": "claude", "dossier": "Old full memo", "bullets": {"thesis": "Old thesis", "drivers": ["Old driver"]}},
                  "_worker_generated_at": (self.now-timedelta(days=1)).isoformat()}
        result = research_payload("DASH", report=report, memo=self.memo)
        self.assertEqual(result["thesis"], self.memo["thesis"])
        self.assertEqual(result["drivers"], self.memo["drivers"])
        self.assertIsNone(result["decision_memo"])

    def test_newer_dossier_wins_over_saved_memo(self):
        report = {"dossier": {"_source": "claude", "dossier": "New full memo", "bullets": {"thesis": "New thesis", "drivers": ["New driver"]}},
                  "_worker_generated_at": (self.now+timedelta(minutes=1)).isoformat()}
        result = research_payload("DASH", report=report, memo=self.memo)
        self.assertEqual(result["thesis"], "New thesis")
        self.assertEqual(result["selection"]["storage"], "research_reports")

    def test_newer_compact_pm_wins_over_old_dossier(self):
        report = {"pm": self.memo, "dossier": {"_source": "claude", "dossier": "Old report", "bullets": {"thesis": "Old thesis"},
                   "generated_at": (self.now-timedelta(days=1)).isoformat()}, "_worker_generated_at": (self.now-timedelta(days=1)).isoformat()}
        result = research_payload("DASH", report=report)
        self.assertEqual(result["thesis"], self.memo["thesis"])

    def test_legacy_wrapped_saved_memo_is_read(self):
        result = research_payload("DASH", report=self.fallback, memo={"view": self.memo, "updated_at": self.now.isoformat()})
        self.assertEqual(result["thesis"], self.memo["thesis"])
        self.assertEqual(result["provenance"], "claude")

    def test_old_saved_memo_is_not_redated_by_new_failed_report(self):
        memo = {**self.memo, "_worker_generated_at": (self.now-timedelta(days=10)).isoformat()}
        result = research_payload("DASH", report=self.fallback, memo=memo)
        self.assertEqual(result["status"], "stale")
        self.assertGreaterEqual(result["age_days"], 10)
        self.assertEqual(result["generated_at"], memo["_worker_generated_at"])

    def test_fallback_memo_does_not_become_genuine_research(self):
        result = research_payload("DASH", report=self.fallback, memo={**self.memo, "_source": "rules fallback"})
        self.assertEqual(result["provenance"], "rules_only")
        self.assertEqual(result["quality"], {})


if __name__ == "__main__":
    unittest.main()
