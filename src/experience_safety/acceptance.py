"""运行体验安全记录模块的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import ConflictError
from night_market_foundation.storage import Database

from .service import ExperienceSafetyService


def _expect_conflict(action: Callable[[], object]) -> bool:
    try:
        action()
    except ConflictError:
        return True
    return False


def run() -> dict[str, object]:
    """执行完整安全闭环并返回验收结果。"""

    clock = FixedClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "safety.sqlite3"
        database = Database(path)
        service = ExperienceSafetyService(database, clock)

        # 基础层建档：机构、角色、场所。
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="夜市主办机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001",
                               new_actor_id="operator-001", display_name="现场操作员",
                               role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001",
                               new_actor_id="reviewer-001", display_name="安全复核员",
                               role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="耳穴体验站",
                              timezone_name="Asia/Shanghai")

        # 安全档案：规程版本、执行人员资格、参与者与筛查、器材批次与领用。
        service.register_protocol(request_id="req-protocol", actor_id="operator-001",
                                  protocol_id="ear-acupoint", version=1, title="耳穴压豆体验规程",
                                  content={"steps": ["评估", "贴压", "观察"],
                                           "contraindications": ["耳部皮肤破损"]})
        service.register_practitioner(request_id="req-practitioner", actor_id="operator-001",
                                      practitioner_id="prac-001", display_name="执行技师",
                                      qualification_no="Q-2026-001",
                                      valid_from="2026-01-01T00:00:00Z",
                                      valid_until="2026-12-31T23:59:59Z")
        service.register_participant(request_id="req-part-1", actor_id="operator-001",
                                     participant_id="part-001", display_name="体验者甲")
        service.register_participant(request_id="req-part-2", actor_id="operator-001",
                                     participant_id="part-002", display_name="体验者乙")
        service.register_screening(request_id="req-scr-1", actor_id="operator-001",
                                   screening_id="scr-001", participant_id="part-001",
                                   conclusion="cleared", valid_until="2026-10-31T23:59:59Z")
        service.register_screening(request_id="req-scr-2", actor_id="operator-001",
                                   screening_id="scr-002", participant_id="part-002",
                                   conclusion="cleared", valid_until="2026-10-31T23:59:59Z")
        for index in ("001", "002", "003"):
            service.register_equipment(request_id=f"req-eq-{index}", actor_id="operator-001",
                                       equipment_id=f"eq-{index}", name="耳穴探笔",
                                       batch_no="batch-2026-09")
            service.issue_equipment(request_id=f"req-co-{index}", actor_id="operator-001",
                                    checkout_id=f"co-{index}", equipment_id=f"eq-{index}",
                                    site_id="site-001")

        # 两次体验通过门禁进入执行状态。
        service.start_session(request_id="req-s1", actor_id="operator-001", session_id="sess-001",
                              site_id="site-001", protocol_id="ear-acupoint", protocol_version=1,
                              practitioner_id="prac-001", participant_id="part-001",
                              equipment_id="eq-001")
        service.append_fact(request_id="req-f1", actor_id="operator-001", session_id="sess-001",
                            kind="observation", detail={"note": "贴压完成，开始观察"})
        service.start_session(request_id="req-s2", actor_id="operator-001", session_id="sess-002",
                              site_id="site-001", protocol_id="ear-acupoint", protocol_version=1,
                              practitioner_id="prac-001", participant_id="part-002",
                              equipment_id="eq-002")

        # 异常事件：原子封存、建立暂停、生成待复核清单。
        incident = service.report_incident(actor_id="operator-001", incident_id="inc-001",
                                           session_id="sess-001", severity="moderate",
                                           description="体验者贴压后自述头晕",
                                           occurred_at="2026-09-26T08:20:00Z")
        replay = service.report_incident(actor_id="reviewer-001", incident_id="inc-001",
                                         session_id="sess-001", severity="moderate",
                                         description="体验者贴压后自述头晕",
                                         occurred_at="2026-09-26T08:20:00Z")
        conflict_rejected = _expect_conflict(
            lambda: service.report_incident(actor_id="operator-001", incident_id="inc-001",
                                            session_id="sess-001", severity="severe",
                                            description="与首次上报不同的事实",
                                            occurred_at="2026-09-26T08:20:00Z"))
        suspensions_active = service.list_suspensions(status="active")
        pending_reviews = service.list_review_items(status="pending")
        late_completion_rejected = _expect_conflict(
            lambda: service.complete_session(request_id="req-late-complete", actor_id="operator-001",
                                             session_id="sess-001", summary="迟到的正常回执"))
        start_blocked = _expect_conflict(
            lambda: service.start_session(request_id="req-s3", actor_id="operator-001",
                                          session_id="sess-003", site_id="site-001",
                                          protocol_id="ear-acupoint", protocol_version=1,
                                          practitioner_id="prac-001", participant_id="part-002",
                                          equipment_id="eq-003"))

        # 进程重启：安全暂停边界与未完成复核必须保留。
        database.close()
        database = Database(path)
        service = ExperienceSafetyService(database, clock)
        restart_preserved = (
            len(service.list_suspensions(status="active")) == len(suspensions_active)
            and len(service.list_review_items(status="pending")) == len(pending_reviews)
            and _expect_conflict(
                lambda: service.start_session(request_id="req-s3b", actor_id="operator-001",
                                              session_id="sess-003", site_id="site-001",
                                              protocol_id="ear-acupoint", protocol_version=1,
                                              practitioner_id="prac-001", participant_id="part-002",
                                              equipment_id="eq-003")))

        # 复核人员逐项关联处置证据后解除暂停。
        resolved = 0
        for item in service.list_review_items(status="pending"):
            service.resolve_review_item(request_id=f"req-resolve-{item.review_id}",
                                        actor_id="reviewer-001", review_id=item.review_id,
                                        evidence_ref="evidence://disposition/inc-001",
                                        note="已完成现场处置与器材检查")
            resolved += 1
        lifted = 0
        for suspension in service.list_suspensions(status="active"):
            service.lift_suspension(request_id=f"req-lift-{suspension.suspension_id}",
                                    actor_id="reviewer-001",
                                    suspension_id=suspension.suspension_id)
            lifted += 1

        # 放行后新体验可以进入执行状态。
        service.start_session(request_id="req-s4", actor_id="operator-001", session_id="sess-004",
                              site_id="site-001", protocol_id="ear-acupoint", protocol_version=1,
                              practitioner_id="prac-001", participant_id="part-002",
                              equipment_id="eq-003")

        # 从被封存的体验追溯全部维度。
        trace = service.trace_session("sess-001")
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "sealed_sessions": len(incident["sealed_sessions"]),
            "suspensions_after_incident": len(suspensions_active),
            "pending_reviews_after_incident": len(pending_reviews),
            "incident_replay_stable": bool(replay["replayed"])
            and replay["suspensions"] == incident["suspensions"]
            and replay["review_items"] == incident["review_items"],
            "conflicting_incident_rejected": conflict_rejected,
            "late_completion_rejected": late_completion_rejected,
            "start_blocked_while_suspended": start_blocked,
            "restart_preserved": restart_preserved,
            "reviews_resolved": resolved,
            "suspensions_lifted": lifted,
            "trace_links": {
                "protocol": trace["protocol"]["protocol_id"] == "ear-acupoint"
                and trace["protocol"]["version"] == 1,
                "practitioner": trace["practitioner"]["practitioner_id"] == "prac-001",
                "participant": trace["participant"]["participant_id"] == "part-001",
                "screening": trace["screening"]["screening_id"] == "scr-001",
                "equipment": trace["equipment"]["equipment_id"] == "eq-001"
                and trace["equipment"]["batch_no"] == "batch-2026-09",
                "facts": len(trace["facts"]) == 2,
                "incidents": len(trace["incidents"]) == 1,
                "suspensions": len(trace["suspensions"]) == 2,
                "review_items": len(trace["review_items"]) == len(pending_reviews),
            },
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (
        result["status"] == "ok"
        and result["audit_valid"]
        and result["incident_replay_stable"]
        and result["conflicting_incident_rejected"]
        and result["late_completion_rejected"]
        and result["start_blocked_while_suspended"]
        and result["restart_preserved"]
        and result["suspensions_lifted"] == result["suspensions_after_incident"]
        and all(result["trace_links"].values())
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
