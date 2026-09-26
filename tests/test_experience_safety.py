import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import (ConflictError, NotFoundError, PermissionDenied,
                                            ValidationError)
from night_market_foundation.storage import Database

from experience_safety.service import ExperienceSafetyService

CLOCK = FixedClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))


def build_world(service):
    """登记一次体验所需的全部基础档案。"""

    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="夜市机构")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                           display_name="管理员", role="admin", organization_id="o1")
    service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                           display_name="操作员", role="operator", organization_id="o1")
    service.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rv1",
                           display_name="复核员", role="reviewer", organization_id="o1")
    service.register_site(request_id="site", actor_id="op1", site_id="s1",
                          organization_id="o1", name="耳穴体验站", timezone_name="Asia/Shanghai")
    service.register_protocol(request_id="proto", actor_id="op1", protocol_id="ear", version=1,
                              title="耳穴压豆规程", content={"steps": ["评估", "贴压", "观察"]})
    service.register_practitioner(request_id="prac", actor_id="op1", practitioner_id="p1",
                                  display_name="技师", qualification_no="Q1",
                                  valid_from="2026-01-01T00:00:00Z",
                                  valid_until="2026-12-31T00:00:00Z")
    service.register_participant(request_id="part", actor_id="op1", participant_id="u1",
                                 display_name="体验者")
    service.register_screening(request_id="scr", actor_id="op1", screening_id="sc1",
                               participant_id="u1", conclusion="cleared",
                               valid_until="2026-10-01T00:00:00Z")
    service.register_equipment(request_id="eq1", actor_id="op1", equipment_id="e1",
                               name="耳穴探笔", batch_no="b1")
    service.issue_equipment(request_id="co1", actor_id="op1", checkout_id="c1",
                            equipment_id="e1", site_id="s1")


class GateTest(unittest.TestCase):
    """开始体验前的准入门禁。"""

    def setUp(self):
        self.database = Database()
        self.service = ExperienceSafetyService(self.database, CLOCK)
        build_world(self.service)

    def tearDown(self):
        self.database.close()

    def start(self, session_id="ss1", **overrides):
        params = dict(request_id=f"start-{session_id}", actor_id="op1", session_id=session_id,
                      site_id="s1", protocol_id="ear", protocol_version=1, practitioner_id="p1",
                      participant_id="u1", equipment_id="e1")
        params.update(overrides)
        return self.service.start_session(**params)

    def test_start_session_enters_executing_when_all_conditions_hold(self):
        receipt = self.start()
        self.assertFalse(receipt.replayed)
        session = self.service.get_session("ss1")
        self.assertEqual("executing", session.status)
        self.assertEqual("c1", session.checkout_id)
        self.assertEqual("b1", session.batch_no)
        self.assertEqual("sc1", session.screening_id)

    def test_start_rejected_when_protocol_version_missing(self):
        with self.assertRaises(NotFoundError):
            self.start(protocol_version=2)

    def test_protocol_version_is_immutable(self):
        with self.assertRaises(ConflictError):
            self.service.register_protocol(request_id="proto-again", actor_id="op1",
                                           protocol_id="ear", version=1, title="耳穴压豆规程",
                                           content={"steps": ["改写"]})
        same = self.service.register_protocol(request_id="proto-same", actor_id="op1",
                                              protocol_id="ear", version=1, title="耳穴压豆规程",
                                              content={"steps": ["评估", "贴压", "观察"]})
        self.assertFalse(same.replayed)
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "UPDATE safety_protocols SET title='x' WHERE protocol_id='ear'")

    def test_start_rejected_when_qualification_expired(self):
        self.service.register_practitioner(request_id="prac2", actor_id="op1",
                                           practitioner_id="p2", display_name="过期技师",
                                           qualification_no="Q2",
                                           valid_from="2025-01-01T00:00:00Z",
                                           valid_until="2026-06-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            self.start(practitioner_id="p2")

    def test_start_rejected_when_screening_not_cleared(self):
        self.service.register_participant(request_id="part2", actor_id="op1",
                                          participant_id="u2", display_name="体验者二")
        self.service.register_screening(request_id="scr2", actor_id="op1", screening_id="sc2",
                                        participant_id="u2", conclusion="rejected",
                                        valid_until="2026-10-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            self.start(participant_id="u2")

    def test_start_rejected_when_screening_expired(self):
        self.service.register_participant(request_id="part3", actor_id="op1",
                                          participant_id="u3", display_name="体验者三")
        self.service.register_screening(request_id="scr3", actor_id="op1", screening_id="sc3",
                                        participant_id="u3", conclusion="cleared",
                                        valid_until="2026-09-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            self.start(participant_id="u3")

    def test_start_rejected_without_screening(self):
        self.service.register_participant(request_id="part4", actor_id="op1",
                                          participant_id="u4", display_name="体验者四")
        with self.assertRaises(NotFoundError):
            self.start(participant_id="u4")

    def test_start_rejected_without_active_checkout(self):
        self.service.register_equipment(request_id="eq2", actor_id="op1", equipment_id="e2",
                                        name="耳穴探笔", batch_no="b1")
        with self.assertRaises(ValidationError):
            self.start(equipment_id="e2")
        self.service.issue_equipment(request_id="co2", actor_id="op1", checkout_id="c2",
                                     equipment_id="e2", site_id="s1")
        self.service.return_equipment(request_id="ret2", actor_id="op1", checkout_id="c2")
        with self.assertRaises(ValidationError):
            self.start(equipment_id="e2")

    def test_start_rejected_when_equipment_in_use(self):
        self.start("ss1")
        with self.assertRaises(ConflictError):
            self.start("ss2")

    def test_equipment_cannot_return_while_session_executing(self):
        self.start("ss1")
        with self.assertRaises(ConflictError):
            self.service.return_equipment(request_id="ret1", actor_id="op1", checkout_id="c1")


class FactTest(unittest.TestCase):
    """操作开始后的事实只追加不覆盖。"""

    def setUp(self):
        self.database = Database()
        self.service = ExperienceSafetyService(self.database, CLOCK)
        build_world(self.service)
        self.service.start_session(request_id="start", actor_id="op1", session_id="ss1",
                                   site_id="s1", protocol_id="ear", protocol_version=1,
                                   practitioner_id="p1", participant_id="u1", equipment_id="e1")

    def tearDown(self):
        self.database.close()

    def test_facts_append_while_executing(self):
        self.service.append_fact(request_id="f1", actor_id="op1", session_id="ss1",
                                 kind="observation", detail={"note": "贴压完成"})
        self.service.append_fact(request_id="f2", actor_id="op1", session_id="ss1",
                                 kind="reaction", detail={"note": "无不适"})
        self.assertEqual(2, len(self.service.list_facts("ss1")))

    def test_facts_cannot_be_updated_or_deleted(self):
        self.service.append_fact(request_id="f1", actor_id="op1", session_id="ss1",
                                 kind="observation", detail={"note": "贴压完成"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute("UPDATE safety_facts SET kind='x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute("DELETE FROM safety_facts")

    def test_facts_rejected_after_completion(self):
        self.service.complete_session(request_id="done", actor_id="op1", session_id="ss1",
                                      summary="体验结束，无不适")
        with self.assertRaises(ConflictError):
            self.service.append_fact(request_id="f9", actor_id="op1", session_id="ss1",
                                     kind="note", detail={"note": "补记"})

    def test_completion_records_receipt_fact(self):
        self.service.complete_session(request_id="done", actor_id="op1", session_id="ss1",
                                      summary="体验结束，无不适")
        session = self.service.get_session("ss1")
        self.assertEqual("completed", session.status)
        kinds = [fact.kind for fact in self.service.list_facts("ss1")]
        self.assertIn("completion", kinds)


class IncidentTest(unittest.TestCase):
    """异常事件的封存、暂停、幂等与复核放行。"""

    def setUp(self):
        self.database = Database()
        self.service = ExperienceSafetyService(self.database, CLOCK)
        build_world(self.service)
        # 第二件同批器材与第二位参与者，用于验证同批封停。
        self.service.register_equipment(request_id="eq2", actor_id="op1", equipment_id="e2",
                                        name="耳穴探笔", batch_no="b1")
        self.service.issue_equipment(request_id="co2", actor_id="op1", checkout_id="c2",
                                     equipment_id="e2", site_id="s1")
        self.service.register_participant(request_id="part2", actor_id="op1",
                                          participant_id="u2", display_name="体验者二")
        self.service.register_screening(request_id="scr2", actor_id="op1", screening_id="sc2",
                                        participant_id="u2", conclusion="cleared",
                                        valid_until="2026-10-01T00:00:00Z")
        # 第三件不同批次的器材，用于验证暂停边界。
        self.service.register_equipment(request_id="eq3", actor_id="op1", equipment_id="e3",
                                        name="耳穴探笔", batch_no="b2")
        self.service.issue_equipment(request_id="co3", actor_id="op1", checkout_id="c3",
                                     equipment_id="e3", site_id="s1")
        self.service.start_session(request_id="start1", actor_id="op1", session_id="ss1",
                                   site_id="s1", protocol_id="ear", protocol_version=1,
                                   practitioner_id="p1", participant_id="u1", equipment_id="e1")
        self.service.start_session(request_id="start2", actor_id="op1", session_id="ss2",
                                   site_id="s1", protocol_id="ear", protocol_version=1,
                                   practitioner_id="p1", participant_id="u2", equipment_id="e2")
        self.incident = self.service.report_incident(
            actor_id="op1", incident_id="inc-1", session_id="ss1", severity="moderate",
            description="体验者贴压后头晕", occurred_at="2026-09-26T08:20:00Z")

    def tearDown(self):
        self.database.close()

    def test_incident_seals_scope_and_builds_review_list_atomically(self):
        self.assertEqual(["ss1", "ss2"], sorted(self.incident["sealed_sessions"]))
        self.assertEqual("sealed", self.service.get_session("ss1").status)
        self.assertEqual("sealed", self.service.get_session("ss2").status)
        suspensions = self.service.list_suspensions(status="active")
        self.assertEqual(2, len(suspensions))
        scopes = {(item.scope_type, item.scope_key) for item in suspensions}
        self.assertEqual({("protocol", "ear@1"), ("equipment_batch", "b1")}, scopes)
        pending = self.service.list_review_items(status="pending")
        self.assertEqual(6, len(pending))
        subjects = {(item.subject_type, item.subject_id) for item in pending}
        self.assertIn(("protocol", "ear@1"), subjects)
        self.assertIn(("equipment_batch", "b1"), subjects)
        self.assertIn(("session", "ss1"), subjects)
        self.assertIn(("session", "ss2"), subjects)
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_incident_replay_keeps_original_result(self):
        replay = self.service.report_incident(
            actor_id="rv1", incident_id="inc-1", session_id="ss1", severity="moderate",
            description="体验者贴压后头晕", occurred_at="2026-09-26T08:20:00Z")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.incident["suspensions"], replay["suspensions"])
        self.assertEqual(self.incident["review_items"], replay["review_items"])
        self.assertEqual(self.incident["sealed_sessions"], replay["sealed_sessions"])
        self.assertEqual(2, len(self.service.list_suspensions()))
        self.assertEqual(6, len(self.service.list_review_items()))

    def test_incident_same_number_with_different_facts_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.report_incident(
                actor_id="op1", incident_id="inc-1", session_id="ss1", severity="severe",
                description="不同的事实", occurred_at="2026-09-26T08:20:00Z")

    def test_repeated_reports_do_not_expand_suspension_scope(self):
        second = self.service.report_incident(
            actor_id="op1", incident_id="inc-2", session_id="ss2", severity="mild",
            description="另一体验者轻微不适", occurred_at="2026-09-26T08:30:00Z")
        self.assertFalse(second["replayed"])
        self.assertEqual([], second["sealed_sessions"])
        self.assertEqual([], second["review_items"])
        self.assertTrue(all(item["reused"] for item in second["suspensions"]))
        self.assertEqual(2, len(self.service.list_suspensions(status="active")))
        self.assertEqual(6, len(self.service.list_review_items(status="pending")))

    def test_late_completion_cannot_override_safety_decision(self):
        with self.assertRaises(ConflictError):
            self.service.complete_session(request_id="late", actor_id="op1", session_id="ss1",
                                          summary="迟到的正常回执")
        self.assertEqual("sealed", self.service.get_session("ss1").status)

    def test_start_blocked_by_protocol_and_batch_suspension(self):
        with self.assertRaises(ConflictError):
            self.service.start_session(request_id="start3", actor_id="op1", session_id="ss3",
                                       site_id="s1", protocol_id="ear", protocol_version=1,
                                       practitioner_id="p1", participant_id="u2", equipment_id="e3")

    def test_resolve_requires_reviewer_role(self):
        pending = self.service.list_review_items(status="pending")
        with self.assertRaises(PermissionDenied):
            self.service.resolve_review_item(request_id="rs1", actor_id="op1",
                                             review_id=pending[0].review_id,
                                             evidence_ref="evidence://1", note="已处置")

    def test_lift_requires_all_items_resolved_with_evidence(self):
        suspension = self.service.list_suspensions(status="active")[0]
        with self.assertRaises(ConflictError):
            self.service.lift_suspension(request_id="lift1", actor_id="rv1",
                                         suspension_id=suspension.suspension_id)
        for item in self.service.list_review_items(status="pending"):
            self.service.resolve_review_item(request_id=f"rs-{item.review_id}", actor_id="rv1",
                                             review_id=item.review_id,
                                             evidence_ref="evidence://disposition",
                                             note="已完成处置")
        for active in self.service.list_suspensions(status="active"):
            self.service.lift_suspension(request_id=f"lift-{active.suspension_id}",
                                         actor_id="rv1", suspension_id=active.suspension_id)
        self.assertEqual([], self.service.list_suspensions(status="active"))
        receipt = self.service.start_session(request_id="start3", actor_id="op1",
                                             session_id="ss3", site_id="s1", protocol_id="ear",
                                             protocol_version=1, practitioner_id="p1",
                                             participant_id="u2", equipment_id="e3")
        self.assertFalse(receipt.replayed)
        self.assertEqual("executing", self.service.get_session("ss3").status)

    def test_trace_links_protocol_people_equipment_and_disposition(self):
        for item in self.service.list_review_items(status="pending"):
            self.service.resolve_review_item(request_id=f"rs-{item.review_id}", actor_id="rv1",
                                             review_id=item.review_id,
                                             evidence_ref="evidence://disposition",
                                             note="已完成处置")
        trace = self.service.trace_session("ss1")
        self.assertEqual("ear", trace["protocol"]["protocol_id"])
        self.assertEqual(1, trace["protocol"]["version"])
        self.assertEqual("p1", trace["practitioner"]["practitioner_id"])
        self.assertEqual("u1", trace["participant"]["participant_id"])
        self.assertEqual("sc1", trace["screening"]["screening_id"])
        self.assertEqual("e1", trace["equipment"]["equipment_id"])
        self.assertEqual("b1", trace["equipment"]["batch_no"])
        self.assertEqual("c1", trace["checkout"]["checkout_id"])
        self.assertEqual(1, len(trace["incidents"]))
        self.assertEqual(2, len(trace["suspensions"]))
        self.assertEqual(6, len(trace["review_items"]))
        self.assertTrue(all(item["status"] == "resolved" for item in trace["review_items"]))
        self.assertTrue(all(item["evidence_ref"] for item in trace["review_items"]))
        self.assertIn("sealed", [fact["kind"] for fact in trace["facts"]])


class RestartTest(unittest.TestCase):
    """进程重启不丢失暂停边界及未完成复核。"""

    def test_restart_preserves_suspensions_and_pending_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safety.sqlite3"
            database = Database(path)
            service = ExperienceSafetyService(database, CLOCK)
            build_world(service)
            service.start_session(request_id="start", actor_id="op1", session_id="ss1",
                                  site_id="s1", protocol_id="ear", protocol_version=1,
                                  practitioner_id="p1", participant_id="u1", equipment_id="e1")
            service.report_incident(actor_id="op1", incident_id="inc-1", session_id="ss1",
                                    severity="moderate", description="体验者不适",
                                    occurred_at="2026-09-26T08:20:00Z")
            active_before = service.list_suspensions(status="active")
            pending_before = service.list_review_items(status="pending")
            database.close()

            reopened = ExperienceSafetyService(Database(path), CLOCK)
            try:
                self.assertEqual([item.suspension_id for item in active_before],
                                 [item.suspension_id
                                  for item in reopened.list_suspensions(status="active")])
                self.assertEqual([item.review_id for item in pending_before],
                                 [item.review_id
                                  for item in reopened.list_review_items(status="pending")])
                self.assertGreater(len(pending_before), 0)
                with self.assertRaises(ConflictError):
                    reopened.start_session(request_id="start2", actor_id="op1", session_id="ss2",
                                           site_id="s1", protocol_id="ear", protocol_version=1,
                                           practitioner_id="p1", participant_id="u1",
                                           equipment_id="e1")
                trace = reopened.trace_session("ss1")
                self.assertEqual("sealed", trace["session"]["status"])
                valid, _ = reopened.verify_audit()
                self.assertTrue(valid)
            finally:
                reopened.database.close()


if __name__ == "__main__":
    unittest.main()
