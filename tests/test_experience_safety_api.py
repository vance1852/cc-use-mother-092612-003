import unittest
from datetime import datetime, timezone

from night_market_foundation.clock import FixedClock
from night_market_foundation.storage import Database

from experience_safety.api import route
from experience_safety.service import ExperienceSafetyService

HEADERS = {"X-Actor-Id": "op1"}
REVIEWER = {"X-Actor-Id": "rv1"}


class SafetyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = ExperienceSafetyService(
            self.database, FixedClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="夜市机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rv1",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="耳穴体验站",
                                   timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _prepare_session(self):
        route(self.service, "POST", "/safety/protocols",
              {"request_id": "proto", "protocol_id": "ear", "version": 1,
               "title": "耳穴规程", "content": {"steps": ["贴压"]}}, HEADERS)
        route(self.service, "POST", "/safety/practitioners",
              {"request_id": "prac", "practitioner_id": "p1", "display_name": "技师",
               "qualification_no": "Q1", "valid_from": "2026-01-01T00:00:00Z",
               "valid_until": "2026-12-31T00:00:00Z"}, HEADERS)
        route(self.service, "POST", "/safety/participants",
              {"request_id": "part", "participant_id": "u1", "display_name": "体验者"}, HEADERS)
        route(self.service, "POST", "/safety/screenings",
              {"request_id": "scr", "screening_id": "sc1", "participant_id": "u1",
               "conclusion": "cleared", "valid_until": "2026-10-01T00:00:00Z"}, HEADERS)
        route(self.service, "POST", "/safety/equipment",
              {"request_id": "eq", "equipment_id": "e1", "name": "探笔", "batch_no": "b1"}, HEADERS)
        route(self.service, "POST", "/safety/checkouts",
              {"request_id": "co", "checkout_id": "c1", "equipment_id": "e1", "site_id": "s1"},
              HEADERS)

    def test_protocol_route_and_immutable_conflict(self):
        status, payload = route(self.service, "POST", "/safety/protocols",
                                {"request_id": "proto", "protocol_id": "ear", "version": 1,
                                 "title": "耳穴规程", "content": {"steps": ["贴压"]}}, HEADERS)
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/safety/protocols",
                          {"request_id": "proto", "protocol_id": "ear", "version": 1,
                           "title": "耳穴规程", "content": {"steps": ["贴压"]}}, HEADERS)
        self.assertEqual(200, status)
        status, payload = route(self.service, "POST", "/safety/protocols",
                                {"request_id": "proto2", "protocol_id": "ear", "version": 1,
                                 "title": "耳穴规程", "content": {"steps": ["改写"]}}, HEADERS)
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_session_incident_and_trace_flow(self):
        self._prepare_session()
        status, _ = route(self.service, "POST", "/safety/sessions",
                          {"request_id": "s1", "session_id": "ss1", "site_id": "s1",
                           "protocol_id": "ear", "protocol_version": 1, "practitioner_id": "p1",
                           "participant_id": "u1", "equipment_id": "e1"}, HEADERS)
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/safety/sessions/facts",
                          {"request_id": "f1", "session_id": "ss1", "kind": "observation",
                           "detail": {"note": "贴压完成"}}, HEADERS)
        self.assertEqual(201, status)
        status, incident = route(self.service, "POST", "/safety/incidents",
                                 {"incident_id": "inc-1", "session_id": "ss1",
                                  "severity": "moderate", "description": "体验者头晕",
                                  "occurred_at": "2026-09-26T08:20:00Z"}, HEADERS)
        self.assertEqual(201, status)
        self.assertFalse(incident["replayed"])
        self.assertEqual(["ss1"], incident["sealed_sessions"])
        status, replay = route(self.service, "POST", "/safety/incidents",
                               {"incident_id": "inc-1", "session_id": "ss1",
                                "severity": "moderate", "description": "体验者头晕",
                                "occurred_at": "2026-09-26T08:20:00Z"}, HEADERS)
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        status, payload = route(self.service, "POST", "/safety/incidents",
                                {"incident_id": "inc-1", "session_id": "ss1",
                                 "severity": "severe", "description": "不同的事实",
                                 "occurred_at": "2026-09-26T08:20:00Z"}, HEADERS)
        self.assertEqual(409, status)
        status, payload = route(self.service, "POST", "/safety/sessions/completion",
                                {"request_id": "late", "session_id": "ss1",
                                 "summary": "迟到回执"}, HEADERS)
        self.assertEqual(409, status)
        status, payload = route(self.service, "GET", "/safety/suspensions?status=active",
                                None, HEADERS)
        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["items"]))
        status, payload = route(self.service, "GET", "/safety/review-items?status=pending",
                                None, HEADERS)
        self.assertEqual(200, status)
        self.assertEqual(4, len(payload["items"]))
        status, trace = route(self.service, "GET", "/safety/sessions/trace?session_id=ss1",
                              None, HEADERS)
        self.assertEqual(200, status)
        self.assertEqual("ear", trace["protocol"]["protocol_id"])
        self.assertEqual("p1", trace["practitioner"]["practitioner_id"])
        self.assertEqual("e1", trace["equipment"]["equipment_id"])
        self.assertEqual(1, len(trace["incidents"]))
        self.assertEqual(2, len(trace["suspensions"]))
        # 复核人员逐项关联证据后解除暂停。
        status, payload = route(self.service, "POST", "/safety/reviews/resolutions",
                                {"request_id": "rs0", "review_id": trace["review_items"][0]["review_id"],
                                 "evidence_ref": "evidence://1", "note": "已处置"}, HEADERS)
        self.assertEqual(403, status)
        for index, item in enumerate(trace["review_items"]):
            status, _ = route(self.service, "POST", "/safety/reviews/resolutions",
                              {"request_id": f"rs{index}", "review_id": item["review_id"],
                               "evidence_ref": "evidence://1", "note": "已处置"}, REVIEWER)
            self.assertEqual(201, status)
        for suspension in trace["suspensions"]:
            status, _ = route(self.service, "POST", "/safety/suspensions/lift",
                              {"request_id": f"lift-{suspension['suspension_id']}",
                               "suspension_id": suspension["suspension_id"]}, REVIEWER)
            self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/safety/suspensions?status=active",
                                None, HEADERS)
        self.assertEqual(0, len(payload["items"]))

    def test_unknown_safety_route_falls_back_to_404(self):
        status, payload = route(self.service, "GET", "/safety/missing", None, HEADERS)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_foundation_routes_still_available(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])


if __name__ == "__main__":
    unittest.main()
