import unittest

from night_market_foundation.api import route_services
from night_market_foundation.safety import SafetyService
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database


def _setup():
    database = Database()
    service = DomainService(database)
    safety = SafetyService(database)
    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="夜市机构")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                           display_name="管理员", role="admin", organization_id="o1")
    service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                           display_name="技师", role="operator", organization_id="o1")
    service.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                           display_name="复核员", role="reviewer", organization_id="o1")
    service.register_site(request_id="site", actor_id="op1", site_id="s1",
                          organization_id="o1", name="一号站", timezone_name="Asia/Shanghai")
    return database, service, safety


class SafetyApiTest(unittest.TestCase):
    def setUp(self):
        self.database, self.service, self.safety = _setup()

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="op1"):
        return route_services(self.service, self.safety, method, path, body or {},
                              {"X-Actor-Id": actor})

    def _ready(self):
        self._call("POST", "/procedures", {
            "request_id": "proc", "procedure_id": "ear", "version": "v1",
            "title": "耳穴规程", "content": {"step": ["贴压"]}}, actor="a1")
        self._call("POST", "/qualifications", {
            "request_id": "qual", "qualification_id": "q1", "target_actor_id": "op1",
            "procedure_id": "ear", "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z"}, actor="a1")
        self._call("POST", "/screenings", {
            "request_id": "scr", "screening_id": "sc1", "site_id": "s1",
            "procedure_id": "ear", "participant_id": "p1", "conclusion": "fit",
            "valid_until": "2026-12-31T00:00:00Z"})
        self._call("POST", "/equipment-batches", {
            "request_id": "batch", "batch_id": "b1", "site_id": "s1", "name": "耳穴批次"})
        self._call("POST", "/equipment-loans", {
            "request_id": "loan", "loan_id": "l1", "batch_id": "b1",
            "borrower_actor_id": "op1"})

    def _ready_two_sessions(self):
        self._ready()
        self._call("POST", "/safety-sessions/start", {
            "request_id": "start", "session_id": "ss1", "site_id": "s1",
            "procedure_id": "ear", "procedure_version": "v1", "qualification_id": "q1",
            "participant_id": "p1", "screening_id": "sc1", "loan_id": "l1"})
        self._call("POST", "/equipment-loans", {
            "request_id": "loan2", "loan_id": "l2", "batch_id": "b1",
            "borrower_actor_id": "op1"})
        self._call("POST", "/safety-sessions/start", {
            "request_id": "start2", "session_id": "ss2", "site_id": "s1",
            "procedure_id": "ear", "procedure_version": "v1", "qualification_id": "q1",
            "participant_id": "p1", "screening_id": "sc1", "loan_id": "l2"})

    def test_full_safety_flow_over_http(self):
        self._ready_two_sessions()
        status, payload = self._call("POST", "/safety-sessions/facts", {
            "request_id": "f1", "session_id": "ss1", "kind": "discomfort",
            "payload": {"symptom": "头晕"}})
        self.assertEqual(201, status)
        self.assertTrue(payload["effective"])

        status, incident = self._call("POST", "/incidents", {
            "request_id": "inc", "incident_no": "I1", "session_id": "ss1",
            "summary": "耳穴后头晕", "severity": "major", "facts": {"symptom": "头晕"}})
        self.assertEqual(201, status)
        self.assertEqual("ss1", incident["sealed_session_id"])
        self.assertEqual(["ss2"], incident["affected_session_ids"])

        # 重放：HTTP 语义返回 200
        status, replay = self._call("POST", "/incidents", {
            "request_id": "inc-r", "incident_no": "I1", "session_id": "ss1",
            "summary": "耳穴后头晕", "severity": "major", "facts": {"symptom": "头晕"}})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        # 同号异文 409
        status, conflict = self._call("POST", "/incidents", {
            "request_id": "inc-c", "incident_no": "I1", "session_id": "ss1",
            "summary": "耳穴后头晕", "severity": "minor", "facts": {"symptom": "恶心"}})
        self.assertEqual(409, status)
        self.assertEqual("conflict", conflict["error"])

        # 暂停边界下新操作被前置条件拒绝
        status, blocked = self._call("POST", "/safety-sessions/start", {
            "request_id": "start2", "session_id": "ss2", "site_id": "s1",
            "procedure_id": "ear", "procedure_version": "v1", "qualification_id": "q1",
            "participant_id": "p1", "screening_id": "sc1", "loan_id": "l1"})
        self.assertEqual(412, status)
        self.assertEqual("precondition_failed", blocked["error"])

        # 待复核清单
        status, items = self._call("GET", "/review-items?incident_no=I1&status=pending")
        self.assertEqual(200, status)
        self.assertEqual(3, len(items["items"]))

        # 逐项关联证据，全部完成后暂停解除
        for index, item in enumerate(items["items"]):
            status, resolved = self._call("POST", "/review-items/resolve", {
                "request_id": f"r{index}", "item_id": item["item_id"],
                "evidence_ref": f"ev://{index}", "resolution_note": "已处置"}, actor="rv1")
            self.assertEqual(201, status)
        self.assertTrue(resolved["suspension_lifted"])

        status, suspensions = self._call("GET", "/suspensions?status=lifted")
        self.assertEqual(200, status)
        self.assertEqual(1, len(suspensions["items"]))

        # 追溯接口
        status, trace = self._call("GET", "/safety-sessions/ss1/trace")
        self.assertEqual(200, status)
        self.assertEqual("sealed", trace["session"]["state"])
        self.assertEqual("ear", trace["procedure"]["procedure_id"])
        self.assertEqual("b1", trace["batch"]["batch_id"])
        self.assertEqual("I1", trace["incidents"][0]["incident_no"])
        self.assertTrue(trace["facts_valid"])

    def test_reviewer_required_for_resolution(self):
        self._ready()
        self._call("POST", "/safety-sessions/start", {
            "request_id": "start", "session_id": "ss1", "site_id": "s1",
            "procedure_id": "ear", "procedure_version": "v1", "qualification_id": "q1",
            "participant_id": "p1", "screening_id": "sc1", "loan_id": "l1"})
        _, incident = self._call("POST", "/incidents", {
            "request_id": "inc", "incident_no": "I1", "session_id": "ss1",
            "summary": "不适", "facts": {}})
        status, payload = self._call("POST", "/review-items/resolve", {
            "request_id": "x", "item_id": incident["review_item_ids"][0],
            "evidence_ref": "ev://1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_trace_unknown_session_returns_404(self):
        status, payload = self._call("GET", "/safety-sessions/nope/trace")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
