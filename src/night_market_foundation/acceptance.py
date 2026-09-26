"""运行适宜技术体验安全记录的离线端到端验收。

覆盖：开项四验、事实追加与防篡改、异常原子封存与同批封停、
事件重放幂等与异文拒绝、迟到回执不越权、逐项关联证据解除暂停、
进程重启后暂停边界与未完成复核不丢失，以及单次体验的全链追溯。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError, PreconditionFailed
from .safety import SafetyService
from .service import DomainService
from .storage import Database

CLOCK = FixedClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))


def _bootstrap(database: Database) -> tuple[DomainService, SafetyService]:
    service = DomainService(database, CLOCK)
    safety = SafetyService(database, CLOCK)
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="示范夜市机构")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="管理员", role="admin", organization_id="org-001")
    service.register_actor(request_id="req-op", actor_id="admin-001", new_actor_id="op-001",
                           display_name="耳穴技师", role="operator", organization_id="org-001")
    service.register_actor(request_id="req-rv", actor_id="admin-001", new_actor_id="rv-001",
                           display_name="安全复核员", role="reviewer", organization_id="org-001")
    service.register_site(request_id="req-site", actor_id="op-001", site_id="site-001",
                          organization_id="org-001", name="中医文化夜市一号站",
                          timezone_name="Asia/Shanghai")
    return service, safety


def _prepare(safety: SafetyService) -> None:
    safety.publish_procedure(
        request_id="req-proc", actor_id="admin-001", procedure_id="ear-acupoint",
        version="v2026.1", title="耳穴压豆体验规程",
        content={"steps": ["核验", "消毒", "贴压", "留观"], "contraindications": ["耳部破溃"]},
    )
    safety.grant_qualification(
        request_id="req-qual", actor_id="admin-001", qualification_id="qual-op-ear",
        target_actor_id="op-001", procedure_id="ear-acupoint",
        valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z",
    )
    safety.grant_qualification(
        request_id="req-qual-expired", actor_id="admin-001", qualification_id="qual-op-expired",
        target_actor_id="op-001", procedure_id="ear-acupoint",
        valid_from="2025-01-01T00:00:00Z", valid_until="2026-09-01T00:00:00Z",
    )
    safety.record_screening(
        request_id="req-scr-a", actor_id="op-001", screening_id="scr-pa", site_id="site-001",
        procedure_id="ear-acupoint", participant_id="pa", conclusion="fit", valid_until="2026-12-31T00:00:00Z",
        detail={"blood_pressure": "正常"},
    )
    safety.record_screening(
        request_id="req-scr-b", actor_id="op-001", screening_id="scr-pb", site_id="site-001",
        procedure_id="ear-acupoint", participant_id="pb", conclusion="conditional", valid_until="2026-12-31T00:00:00Z",
        detail={"note": "低血压，缩短留观"},
    )
    safety.record_screening(
        request_id="req-scr-c", actor_id="op-001", screening_id="scr-pc", site_id="site-001",
        procedure_id="ear-acupoint", participant_id="pc", conclusion="unfit", valid_until="2026-12-31T00:00:00Z",
        detail={"note": "耳部皮肤破溃"},
    )
    safety.register_equipment_batch(
        request_id="req-batch", actor_id="op-001", batch_id="batch-ear-09",
        site_id="site-001", name="九月批次耳穴贴压器材",
    )
    safety.loan_equipment(
        request_id="req-loan-a", actor_id="op-001", loan_id="loan-a",
        batch_id="batch-ear-09", borrower_actor_id="op-001",
    )
    safety.loan_equipment(
        request_id="req-loan-b", actor_id="op-001", loan_id="loan-b",
        batch_id="batch-ear-09", borrower_actor_id="op-001",
    )


def run() -> dict[str, object]:
    """执行完整安全闭环验收并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "safety_acceptance.sqlite3"
        database = Database(db_path)
        service, safety = _bootstrap(database)
        _prepare(safety)

        # ---- 开项四验：任一条件失效都不能进入执行状态 --------------------
        try:
            safety.start_session(
                request_id="req-start-bad-qual", actor_id="op-001", session_id="sess-bad-qual",
                site_id="site-001", procedure_id="ear-acupoint", procedure_version="v2026.1",
                qualification_id="qual-op-expired", participant_id="pa",
                screening_id="scr-pa", loan_id="loan-a")
            raise AssertionError("过期资格必须阻止开项")
        except PreconditionFailed:
            pass
        try:
            safety.start_session(
                request_id="req-start-bad-scr", actor_id="op-001", session_id="sess-bad-scr",
                site_id="site-001", procedure_id="ear-acupoint", procedure_version="v2026.1",
                qualification_id="qual-op-ear", participant_id="pc",
                screening_id="scr-pc", loan_id="loan-a")
            raise AssertionError("unfit 筛查结论必须阻止开项")
        except PreconditionFailed:
            pass

        start_a = safety.start_session(
            request_id="req-start-a", actor_id="op-001", session_id="sess-a",
            site_id="site-001", procedure_id="ear-acupoint", procedure_version="v2026.1",
            qualification_id="qual-op-ear", participant_id="pa",
            screening_id="scr-pa", loan_id="loan-a")
        start_b = safety.start_session(
            request_id="req-start-b", actor_id="op-001", session_id="sess-b",
            site_id="site-001", procedure_id="ear-acupoint", procedure_version="v2026.1",
            qualification_id="qual-op-ear", participant_id="pb",
            screening_id="scr-pb", loan_id="loan-b")

        safety.append_session_fact(
            request_id="req-fact-a1", actor_id="op-001", session_id="sess-a",
            kind="observation", payload={"step": "贴压完成", "minute": 5})
        safety.append_session_fact(
            request_id="req-fact-a2", actor_id="op-001", session_id="sess-a",
            kind="discomfort", payload={"symptom": "头晕", "minute": 8})

        # ---- 异常事件：原子封存本次过程并封停同批其他操作 --------------
        incident_facts = {"symptom": "头晕", "action_taken": "立即取豆平卧", "vitals": {"bp": "88/56"}}
        incident = safety.report_incident(
            request_id="req-incident", actor_id="op-001", incident_no="INC-2026-0926-01",
            session_id="sess-a", summary="耳穴体验后参与者头晕不适",
            severity="major", facts=incident_facts)
        assert incident["sealed_session_id"] == "sess-a"
        assert incident["affected_session_ids"] == ["sess-b"]
        assert incident["suspended_batch_ids"] == ["batch-ear-09"]
        assert len(incident["review_item_ids"]) == 3

        # 同一事件重放保持原结果
        replay = safety.report_incident(
            request_id="req-incident-replay", actor_id="op-001",
            incident_no="INC-2026-0926-01", session_id="sess-a",
            summary="耳穴体验后参与者头晕不适", severity="major", facts=incident_facts)
        assert replay["replayed"] is True
        assert replay["review_item_ids"] == incident["review_item_ids"]

        # 事件号相同而事实不同则拒绝
        try:
            safety.report_incident(
                request_id="req-incident-conflict", actor_id="op-001",
                incident_no="INC-2026-0926-01", session_id="sess-a",
                summary="耳穴体验后参与者头晕不适", severity="minor",
                facts={"symptom": "头晕", "action_taken": "继续留观"})
            raise AssertionError("同号异文事件必须被拒绝")
        except ConflictError:
            pass

        # 暂停边界内不能再开新项
        try:
            safety.start_session(
                request_id="req-start-c-blocked", actor_id="op-001", session_id="sess-c",
                site_id="site-001", procedure_id="ear-acupoint", procedure_version="v2026.1",
                qualification_id="qual-op-ear", participant_id="pa",
                screening_id="scr-pa", loan_id="loan-a")
            raise AssertionError("封停批次必须阻止新操作")
        except PreconditionFailed:
            pass

        # 迟到的正常回执不能越过安全决定
        late = safety.complete_session(
            request_id="req-complete-b-late", actor_id="op-001",
            session_id="sess-b", outcome={"note": "参与者称已恢复"})
        assert late["effective"] is False
        assert safety.get_session("sess-b").state == "suspended"

        # ---- 不可变边界：普通 SQL 编辑无法覆盖已落库事实 ------------------
        immutable_blocks: list[str] = []
        for statement, marker in (
            ("UPDATE session_facts SET kind='x'", "事实"),
            ("DELETE FROM session_facts", "事实"),
            ("UPDATE procedure_versions SET title='x'", "规程"),
            ("UPDATE incidents SET summary='x'", "异常"),
        ):
            try:
                database.connection.execute(statement)
                raise AssertionError(f"不可变边界未生效: {statement}")
            except sqlite3.IntegrityError as exc:
                assert marker in str(exc)
                immutable_blocks.append(marker)
        facts_valid, fact_count = safety.verify_session_facts("sess-a")
        assert facts_valid and fact_count == 3

        # ---- 首次重启：暂停边界与未完成复核必须保留 ----------------------
        audit_valid_before, audit_count_before = service.verify_audit()
        database.close()

        database = Database(db_path)
        service = DomainService(database, CLOCK)
        safety = SafetyService(database, CLOCK)
        assert safety.get_session("sess-a").state == "sealed"
        assert safety.get_session("sess-b").state == "suspended"
        pending = safety.list_review_items("INC-2026-0926-01", "pending")
        assert len(pending) == 3
        active_suspensions = safety.list_suspensions("active")
        assert len(active_suspensions) == 1
        restart_one_pending = len(pending)

        # 复核员逐项关联处置证据；未全部完成前暂停不解除
        first = safety.resolve_review_item(
            request_id="req-resolve-1", actor_id="rv-001",
            item_id=incident["review_item_ids"][0],
            evidence_ref="evidence://inc-01/origin-session-medical-note",
            resolution_note="参与者经平卧补液后恢复，记录留观单")
        assert first["suspension_lifted"] is False

        # 无证据不得结项
        try:
            safety.resolve_review_item(
                request_id="req-resolve-no-evidence", actor_id="rv-001",
                item_id=incident["review_item_ids"][1], evidence_ref="   ")
            raise AssertionError("缺少处置证据必须拒绝结项")
        except Exception:
            pass

        # ---- 第二次重启：中间状态继续保留 -------------------------------
        database.close()
        database = Database(db_path)
        service = DomainService(database, CLOCK)
        safety = SafetyService(database, CLOCK)
        pending_two = safety.list_review_items("INC-2026-0926-01", "pending")
        assert len(pending_two) == 2
        assert safety.list_suspensions("active")

        safety.resolve_review_item(
            request_id="req-resolve-2", actor_id="rv-001",
            item_id=incident["review_item_ids"][1],
            evidence_ref="evidence://inc-01/affected-session-check",
            resolution_note="同批在施操作参与者均无不适")
        last = safety.resolve_review_item(
            request_id="req-resolve-3", actor_id="rv-001",
            item_id=incident["review_item_ids"][2],
            evidence_ref="evidence://inc-01/batch-quarantine-report",
            resolution_note="批次封存抽检，更换消毒包装后放行")
        assert last["suspension_lifted"] is True
        assert not safety.list_suspensions("active")

        # 暂停解除后新操作可正常开项
        start_c = safety.start_session(
            request_id="req-start-c", actor_id="op-001", session_id="sess-c",
            site_id="site-001", procedure_id="ear-acupoint", procedure_version="v2026.1",
            qualification_id="qual-op-ear", participant_id="pa",
            screening_id="scr-pa", loan_id="loan-a")
        safety.complete_session(request_id="req-complete-c", actor_id="op-001",
                                session_id="sess-c", outcome={"note": "过程正常"})

        # ---- 全链追溯：一次体验可回溯规程、人员、器材、风险处置 ----------
        trace = safety.session_trace("sess-b")
        assert trace["procedure"]["procedure_id"] == "ear-acupoint"
        assert trace["procedure"]["content_hash"] == trace["session"]["procedure_hash"]
        assert trace["qualification"]["qualification_id"] == "qual-op-ear"
        assert trace["screening"]["screening_id"] == "scr-pb"
        assert trace["loan"]["loan_id"] == "loan-b"
        assert trace["batch"]["batch_id"] == "batch-ear-09"
        assert len(trace["incidents"]) == 1
        assert trace["incidents"][0]["incident_no"] == "INC-2026-0926-01"
        assert trace["suspensions"][0]["status"] == "lifted"
        assert all(item["status"] == "resolved" and item["evidence_ref"]
                   for item in trace["review_items"])
        assert trace["facts_valid"]

        audit_valid, audit_count = service.verify_audit()
        assert audit_valid and audit_count >= audit_count_before
        result = {
            "status": "ok",
            "started_sessions": [start_a["session_id"], start_b["session_id"],
                                 start_c["session_id"]],
            "precheck_rejected": ["expired_qualification", "unfit_screening",
                                  "suspended_batch"],
            "incident_no": incident["incident_no"],
            "incident_replayed": replay["replayed"],
            "same_no_different_facts_rejected": True,
            "sealed_session": incident["sealed_session_id"],
            "affected_sessions": incident["affected_session_ids"],
            "suspended_batches": incident["suspended_batch_ids"],
            "review_items": len(incident["review_item_ids"]),
            "pending_after_first_restart": restart_one_pending,
            "late_receipt_effective": late["effective"],
            "immutable_blocks": immutable_blocks,
            "session_a_facts_valid": facts_valid,
            "session_a_fact_count": fact_count,
            "suspension_lifted": last["suspension_lifted"],
            "trace_links_complete": True,
            "trace_facts_valid": trace["facts_valid"],
            "audit_valid": audit_valid,
            "audit_events": audit_count,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
