import unittest

from night_market_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["trace_facts_valid"])
        self.assertEqual("sess-a", result["sealed_session"])

    def test_safety_closure_results(self):
        result = run()
        self.assertEqual(["expired_qualification", "unfit_screening", "suspended_batch"],
                         result["precheck_rejected"])
        self.assertEqual("sess-a", result["sealed_session"])
        self.assertEqual(["sess-b"], result["affected_sessions"])
        self.assertEqual(["batch-ear-09"], result["suspended_batches"])
        self.assertTrue(result["incident_replayed"])
        self.assertTrue(result["same_no_different_facts_rejected"])
        self.assertFalse(result["late_receipt_effective"])
        self.assertTrue(result["suspension_lifted"])
        self.assertEqual(3, result["pending_after_first_restart"])
        self.assertTrue(result["trace_links_complete"])


if __name__ == "__main__":
    unittest.main()
