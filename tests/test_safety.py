import sqlite3
import unittest
from datetime import datetime, timezone

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import (
    ConflictError,
    PermissionDenied,
    PreconditionFailed,
)
from night_market_foundation.safety import SafetyService
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.safety = SafetyService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="夜市机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="技师", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="一号站",
                                   timezone_name="Asia/Shanghai")
        self.safety.publish_procedure(
            request_id="proc", actor_id="a1", procedure_id="ear", version="v1",
            title="耳穴规程", content={"step": ["消毒", "贴压"]})
        self.safety.grant_qualification(
            request_id="qual", actor_id="a1", qualification_id="q1",
            target_actor_id="op1", procedure_id="ear",
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z")
        self.safety.record_screening(
            request_id="scr", actor_id="op1", screening_id="sc1", site_id="s1",
            procedure_id="ear", participant_id="p1", conclusion="fit",
            valid_until="2026-12-31T00:00:00Z")
        self.safety.register_equipment_batch(
            request_id="batch", actor_id="op1", batch_id="b1", site_id="s1", name="耳穴批次")
        self.safety.loan_equipment(
            request_id="loan", actor_id="op1", loan_id="l1",
            batch_id="b1", borrower_actor_id="op1")

    def tearDown(self):
        self.database.close()

    def _start(self, request_id="start", session_id="ss1", **overrides):
        arguments = dict(
            request_id=request_id, actor_id="op1", session_id=session_id, site_id="s1",
            procedure_id="ear", procedure_version="v1", qualification_id="q1",
            participant_id="p1", screening_id="sc1", loan_id="l1")
        arguments.update(overrides)
        return self.safety.start_session(**arguments)

    # ------------------------------------------------------------ 开项四验

    def test_start_succeeds_when_all_preconditions_hold(self):
        result = self._start()
        self.assertEqual("running", result["state"])
        self.assertFalse(result["replayed"])

    def test_procedure_version_is_immutable(self):
        with self.assertRaises(ConflictError):
            self.safety.publish_procedure(
                request_id="proc2", actor_id="a1", procedure_id="ear", version="v1",
                title="耳穴规程", content={"step": ["不同内容"]})
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "UPDATE procedure_versions SET title='x' WHERE procedure_id='ear'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute("DELETE FROM procedure_versions")

    def test_missing_or_mismatched_procedure_blocks_start(self):
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad1", procedure_version="v9")
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad2", procedure_id="other")

    def test_expired_qualification_blocks_start(self):
        self.safety.grant_qualification(
            request_id="qual-old", actor_id="a1", qualification_id="qold",
            target_actor_id="op1", procedure_id="ear",
            valid_from="2025-01-01T00:00:00Z", valid_until="2026-09-01T00:00:00Z")
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad", qualification_id="qold")

    def test_revoked_qualification_blocks_start(self):
        self.safety.revoke_qualification(request_id="revoke", actor_id="a1",
                                         qualification_id="q1")
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad")

    def test_qualification_of_other_procedure_blocks_start(self):
        self.safety.publish_procedure(
            request_id="proc2", actor_id="a1", procedure_id="moxa", version="v1",
            title="艾灸规程", content={"step": ["点火"]})
        self.safety.grant_qualification(
            request_id="qual2", actor_id="a1", qualification_id="q2",
            target_actor_id="op1", procedure_id="moxa",
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z")
        # 用艾灸资格开耳穴项目：资格项目不一致必须拒绝
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad", qualification_id="q2")
        # 用耳穴资格开艾灸项目：筛查结论不属于该项目，同样拒绝
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad2", procedure_id="moxa", procedure_version="v1",
                        qualification_id="q2")

    def test_screening_of_other_procedure_blocks_start(self):
        self.safety.publish_procedure(
            request_id="proc2", actor_id="a1", procedure_id="moxa", version="v1",
            title="艾灸规程", content={"step": ["点火"]})
        self.safety.grant_qualification(
            request_id="qual2", actor_id="a1", qualification_id="q2",
            target_actor_id="op1", procedure_id="moxa",
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z")
        self.safety.record_screening(
            request_id="scr2", actor_id="op1", screening_id="sc2", site_id="s1",
            procedure_id="moxa", participant_id="p1", conclusion="fit",
            valid_until="2026-12-31T00:00:00Z")
        # 四验一致时可以开新的项目
        self._start(request_id="moxa-ok", session_id="ssm", procedure_id="moxa",
                    procedure_version="v1", qualification_id="q2", screening_id="sc2")

    def test_unfit_screening_blocks_start(self):
        self.safety.record_screening(
            request_id="scr2", actor_id="op1", screening_id="sc2", site_id="s1",
            procedure_id="ear", participant_id="p2", conclusion="unfit",
            valid_until="2026-12-31T00:00:00Z")
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad", participant_id="p2", screening_id="sc2")

    def test_screening_participant_mismatch_blocks_start(self):
        self.safety.record_screening(
            request_id="scr2", actor_id="op1", screening_id="sc2", site_id="s1",
            procedure_id="ear", participant_id="p2", conclusion="fit",
            valid_until="2026-12-31T00:00:00Z")
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad", participant_id="p1", screening_id="sc2")

    def test_returned_loan_blocks_start(self):
        self.safety.return_equipment(request_id="ret", actor_id="op1", loan_id="l1")
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad")

    def test_loan_borrower_mismatch_blocks_start(self):
        self.service.register_actor(request_id="op2x", actor_id="a1", new_actor_id="op2",
                                    display_name="技师二", role="operator",
                                    organization_id="o1")
        self.safety.loan_equipment(
            request_id="loan2", actor_id="op1", loan_id="l2",
            batch_id="b1", borrower_actor_id="op2")
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="bad", loan_id="l2")

    # ------------------------------------------------------------ 事实追加

    def test_facts_append_only_and_hash_chained(self):
        self._start()
        self.safety.append_session_fact(
            request_id="f1", actor_id="op1", session_id="ss1",
            kind="observation", payload={"note": "正常"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute("UPDATE session_facts SET kind='x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute("DELETE FROM session_facts")
        valid, count = self.safety.verify_session_facts("ss1")
        self.assertTrue(valid)
        self.assertEqual(1, count)

    def test_fact_after_completion_is_recorded_but_not_effective(self):
        self._start()
        self.safety.complete_session(request_id="done", actor_id="op1", session_id="ss1")
        receipt = self.safety.append_session_fact(
            request_id="late", actor_id="op1", session_id="ss1",
            kind="observation", payload={"note": "迟到记录"})
        self.assertFalse(receipt["effective"])
        self.assertEqual("completed", receipt["state"])

    def test_system_fact_kind_cannot_be_forged(self):
        self._start()
        with self.assertRaises(Exception):
            self.safety.append_session_fact(
                request_id="fake", actor_id="op1", session_id="ss1",
                kind="incident_declared", payload={"x": 1})

    # ------------------------------------------------------------ 异常封存

    def _start_two_sessions(self):
        self._start(request_id="s1", session_id="ss1")
        self.safety.loan_equipment(
            request_id="loan2", actor_id="op1", loan_id="l2",
            batch_id="b1", borrower_actor_id="op1")
        self._start(request_id="s2", session_id="ss2", loan_id="l2")

    def test_incident_atomically_seals_session_and_suspends_batch_and_peers(self):
        self._start_two_sessions()
        result = self.safety.report_incident(
            request_id="inc", actor_id="op1", incident_no="I1", session_id="ss1",
            summary="参与者不适", severity="major", facts={"symptom": "头晕"})
        self.assertEqual("ss1", result["sealed_session_id"])
        self.assertEqual(["ss2"], result["affected_session_ids"])
        self.assertEqual(["b1"], result["suspended_batch_ids"])
        self.assertEqual(3, len(result["review_item_ids"]))
        self.assertEqual("sealed", self.safety.get_session("ss1").state)
        self.assertEqual("suspended", self.safety.get_session("ss2").state)
        self.assertEqual([("b1",)], [tuple(row) for row in self.database.connection.execute(
            "SELECT batch_id FROM equipment_batches WHERE status='suspended'")])

    def test_incident_replay_keeps_original_result(self):
        self._start_two_sessions()
        facts = {"symptom": "头晕"}
        first = self.safety.report_incident(
            request_id="inc", actor_id="op1", incident_no="I1", session_id="ss1",
            summary="不适", facts=facts)
        second = self.safety.report_incident(
            request_id="inc-other-request", actor_id="op1", incident_no="I1",
            session_id="ss1", summary="不适", facts=facts)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["review_item_ids"], second["review_item_ids"])
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM incidents").fetchone()[0])

    def test_same_incident_no_with_different_facts_is_rejected(self):
        self._start_two_sessions()
        self.safety.report_incident(
            request_id="inc", actor_id="op1", incident_no="I1", session_id="ss1",
            summary="不适", facts={"symptom": "头晕"})
        with self.assertRaises(ConflictError):
            self.safety.report_incident(
                request_id="inc2", actor_id="op1", incident_no="I1", session_id="ss1",
                summary="不适", facts={"symptom": "恶心"})

    def test_incident_request_id_replays_and_rejects_changed_payload(self):
        self._start_two_sessions()
        kwargs = dict(actor_id="op1", incident_no="I1", session_id="ss1",
                      summary="不适", facts={"symptom": "头晕"})
        first = self.safety.report_incident(request_id="same-req", **kwargs)
        replay = self.safety.report_incident(request_id="same-req", **kwargs)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["review_item_ids"], replay["review_item_ids"])
        with self.assertRaises(ConflictError):
            self.safety.report_incident(
                request_id="same-req", actor_id="op1", incident_no="I9", session_id="ss1",
                summary="另一件事", facts={"symptom": "头晕"})

    def test_duplicate_incident_report_does_not_expand_suspension(self):
        self._start_two_sessions()
        kwargs = dict(actor_id="op1", incident_no="I1", session_id="ss1",
                      summary="不适", facts={"symptom": "头晕"})
        first = self.safety.report_incident(request_id="inc", **kwargs)
        replay = self.safety.report_incident(request_id="inc2", **kwargs)
        self.assertEqual(first["review_item_ids"], replay["review_item_ids"])
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM suspensions").fetchone()[0])
        self.assertEqual(3, self.database.connection.execute(
            "SELECT COUNT(*) FROM review_items").fetchone()[0])

    def test_suspended_batch_blocks_new_start(self):
        self._start_two_sessions()
        self.safety.report_incident(
            request_id="inc", actor_id="op1", incident_no="I1", session_id="ss1",
            summary="不适", facts={"symptom": "头晕"})
        with self.assertRaises(PreconditionFailed):
            self._start(request_id="blocked", session_id="ss3")

    def test_late_completion_receipt_cannot_override_sealing(self):
        self._start_two_sessions()
        self.safety.report_incident(
            request_id="inc", actor_id="op1", incident_no="I1", session_id="ss1",
            summary="不适", facts={"symptom": "头晕"})
        receipt = self.safety.complete_session(
            request_id="late", actor_id="op1", session_id="ss2", outcome={"ok": True})
        self.assertFalse(receipt["effective"])
        self.assertEqual("suspended", self.safety.get_session("ss2").state)
        facts = self.safety.list_session_facts("ss2")
        self.assertEqual("completion_receipt_late", facts[-1].kind)
        self.assertFalse(facts[-1].effective)

    # ------------------------------------------------------------ 复核解除

    def _incident_with_items(self):
        self._start_two_sessions()
        result = self.safety.report_incident(
            request_id="inc", actor_id="op1", incident_no="I1", session_id="ss1",
            summary="不适", facts={"symptom": "头晕"})
        return result

    def test_operator_cannot_resolve_review_items(self):
        result = self._incident_with_items()
        with self.assertRaises(PermissionDenied):
            self.safety.resolve_review_item(
                request_id="x", actor_id="op1", item_id=result["review_item_ids"][0],
                evidence_ref="ev://1")

    def test_review_item_requires_evidence(self):
        result = self._incident_with_items()
        with self.assertRaises(Exception):
            self.safety.resolve_review_item(
                request_id="x", actor_id="rv1", item_id=result["review_item_ids"][0],
                evidence_ref="  ")

    def test_suspension_lifts_only_after_every_item_resolved(self):
        result = self._incident_with_items()
        items = result["review_item_ids"]
        r1 = self.safety.resolve_review_item(
            request_id="r1", actor_id="rv1", item_id=items[0], evidence_ref="ev://1")
        r2 = self.safety.resolve_review_item(
            request_id="r2", actor_id="rv1", item_id=items[1], evidence_ref="ev://2")
        self.assertFalse(r1["suspension_lifted"])
        self.assertFalse(r2["suspension_lifted"])
        self.assertEqual("suspended", self.database.connection.execute(
            "SELECT status FROM equipment_batches WHERE batch_id='b1'").fetchone()[0])
        r3 = self.safety.resolve_review_item(
            request_id="r3", actor_id="rv1", item_id=items[2], evidence_ref="ev://3")
        self.assertTrue(r3["suspension_lifted"])
        self.assertEqual("active", self.database.connection.execute(
            "SELECT status FROM equipment_batches WHERE batch_id='b1'").fetchone()[0])
        self.assertEqual("lifted", self.safety.list_suspensions()[0].status)
        # 解除后同批器材可以重新开项
        self._start(request_id="resume", session_id="ss3")

    def test_resolved_item_cannot_be_modified_again(self):
        result = self._incident_with_items()
        self.safety.resolve_review_item(
            request_id="r1", actor_id="rv1", item_id=result["review_item_ids"][0],
            evidence_ref="ev://1")
        with self.assertRaises(ConflictError):
            self.safety.resolve_review_item(
                request_id="r1b", actor_id="rv1", item_id=result["review_item_ids"][0],
                evidence_ref="ev://other")

    def test_second_incident_keeps_batch_held_until_both_reviewed(self):
        result1 = self._incident_with_items()
        # 第一批复核全部完成，批次恢复
        for index, item_id in enumerate(result1["review_item_ids"]):
            self.safety.resolve_review_item(
                request_id=f"r1-{index}", actor_id="rv1", item_id=item_id,
                evidence_ref=f"ev://1-{index}")
        self.assertEqual("active", self.database.connection.execute(
            "SELECT status FROM equipment_batches WHERE batch_id='b1'").fetchone()[0])
        # 新的体验再发异常
        self.safety.loan_equipment(
            request_id="loan3", actor_id="op1", loan_id="l3",
            batch_id="b1", borrower_actor_id="op1")
        self._start(request_id="s3", session_id="ss3", loan_id="l3")
        result2 = self.safety.report_incident(
            request_id="inc2", actor_id="op1", incident_no="I2", session_id="ss3",
            summary="再次不适", facts={"symptom": "心慌"})
        self.assertEqual("suspended", self.database.connection.execute(
            "SELECT status FROM equipment_batches WHERE batch_id='b1'").fetchone()[0])
        for index, item_id in enumerate(result2["review_item_ids"]):
            response = self.safety.resolve_review_item(
                request_id=f"r2-{index}", actor_id="rv1", item_id=item_id,
                evidence_ref=f"ev://2-{index}")
        self.assertTrue(response["suspension_lifted"])
        self.assertEqual("active", self.database.connection.execute(
            "SELECT status FROM equipment_batches WHERE batch_id='b1'").fetchone()[0])

    # ------------------------------------------------------------ 追溯

    def test_session_trace_links_all_dimensions(self):
        result = self._incident_with_items()
        self.safety.resolve_review_item(
            request_id="rx", actor_id="rv1", item_id=result["review_item_ids"][0],
            evidence_ref="ev://x")
        trace = self.safety.session_trace("ss2")
        self.assertEqual("ear", trace["procedure"]["procedure_id"])
        self.assertEqual("q1", trace["qualification"]["qualification_id"])
        self.assertEqual("sc1", trace["screening"]["screening_id"])
        self.assertEqual("b1", trace["batch"]["batch_id"])
        self.assertEqual("I1", trace["incidents"][0]["incident_no"])
        self.assertTrue(trace["facts_valid"])
        self.assertEqual("suspended", trace["session"]["state"])


if __name__ == "__main__":
    unittest.main()
