import unittest

from experience_safety.acceptance import run


class SafetyAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["incident_replay_stable"])
        self.assertTrue(result["conflicting_incident_rejected"])
        self.assertTrue(result["late_completion_rejected"])
        self.assertTrue(result["start_blocked_while_suspended"])
        self.assertTrue(result["restart_preserved"])
        self.assertEqual(2, result["suspensions_after_incident"])
        self.assertEqual(2, result["sealed_sessions"])
        self.assertEqual(result["suspensions_after_incident"], result["suspensions_lifted"])
        self.assertEqual(result["pending_reviews_after_incident"], result["reviews_resolved"])
        self.assertTrue(all(result["trace_links"].values()))


if __name__ == "__main__":
    unittest.main()
