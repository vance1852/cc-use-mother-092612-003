"""实现适宜技术体验的安全记录闭环。

边界规则：
- 开始操作前在同一事务内核验规程版本、人员资格期限、参与者筛查结论、
  器材领用关系与批次暂停边界，任一失效都不能进入执行状态；
- 操作开始后的事实只允许追加，过程事实以哈希链串联，数据库触发器
  物理禁止修改和删除；
- 异常事件在一个事务内封存源头过程、封停同批器材及其在施操作、
  生成待复核清单；事件号加事实摘要保证重放幂等、异文冲突；
- 暂停只能由复核人员逐项关联处置证据后解除，迟到的正常回执只追加
  事实、不能越过已经生效的安全决定。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Callable

from .audit import GENESIS_HASH, append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    PreconditionFailed,
    ValidationError,
)
from .models import (
    ReviewItem,
    SafetySession,
    SessionFact,
    Suspension,
)
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
SEVERITIES = frozenset({"minor", "major", "critical"})
SCREENING_CONCLUSIONS = frozenset({"fit", "unfit", "conditional"})
# 外部只能追加观察与不适；completion、completion_receipt_late、incident_declared
# 等系统事实只能由完成回执或异常处置动作生成。
APPENDABLE_FACT_KINDS = frozenset({"observation", "discomfort"})


class SafetyService:
    """提供安全记录所需的登记、开项、追述、封存与复核能力。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _timestamp(self, value: str, field: str) -> str:
        value = str(value).strip()
        self._parse_ts(value, field)
        return value

    def _parse_ts(self, value: str, field: str = "时间") -> datetime:
        normalized = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone()

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor_row, *roles: str) -> None:
        if actor_row["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        resource_type, resource_id, response = create()
        response = dict(response)
        response["replayed"] = False
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return response

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _same_org_or_admin(self, actor_row, site_row) -> None:
        if actor_row["role"] != "admin" and actor_row["organization_id"] != site_row["organization_id"]:
            raise PermissionDenied("不能操作其他组织场所的安全记录")

    # ---------------------------------------------------------- 规程与资格登记

    def publish_procedure(self, *, request_id: str, actor_id: str, procedure_id: str,
                          version: str, title: str, content: dict[str, Any]) -> dict[str, Any]:
        """登记不可变的项目规程版本；同号同版只能是同一内容。"""

        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        payload = {"actor_id": actor_id, "procedure_id": procedure_id, "version": version,
                   "title": title, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            procedure_id = self._identifier(procedure_id, "procedure_id")
            version = self._identifier(version, "version")
            title = self._text(title, "title")
            content_hash = digest(content)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM procedure_versions WHERE procedure_id=? AND version=?",
                    (procedure_id, version),
                ).fetchone()
                if existing:
                    if existing["content_hash"] != content_hash:
                        raise ConflictError("规程版本一经发布即不可变，不能登记不同内容")
                    return ("procedure_version", f"{procedure_id}@{version}",
                            {"procedure_id": procedure_id, "version": version,
                             "content_hash": existing["content_hash"]})
                connection.execute(
                    "INSERT INTO procedure_versions(procedure_id,version,title,content_json,"
                    "content_hash,published_by,published_at) VALUES(?,?,?,?,?,?,?)",
                    (procedure_id, version, title, canonical_json(content), content_hash,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="procedure.published",
                             resource_type="procedure_version",
                             resource_id=f"{procedure_id}@{version}",
                             detail={"procedure_id": procedure_id, "version": version,
                                     "content_hash": content_hash},
                             occurred_at=self._now())
                return ("procedure_version", f"{procedure_id}@{version}",
                        {"procedure_id": procedure_id, "version": version,
                         "content_hash": content_hash})

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_procedure", payload=payload, create=create)

    def grant_qualification(self, *, request_id: str, actor_id: str, qualification_id: str,
                            target_actor_id: str, procedure_id: str,
                            valid_from: str, valid_until: str) -> dict[str, Any]:
        """登记执行人员对某项目的资格及有效期限。"""

        payload = {"actor_id": actor_id, "qualification_id": qualification_id,
                   "target_actor_id": target_actor_id, "procedure_id": procedure_id,
                   "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            target = self._actor(connection, target_actor_id)
            qualification_id = self._identifier(qualification_id, "qualification_id")
            procedure_id = self._identifier(procedure_id, "procedure_id")
            valid_from = self._timestamp(valid_from, "valid_from")
            valid_until = self._timestamp(valid_until, "valid_until")
            if not valid_from < valid_until:
                raise ValidationError("资格起始时间必须早于失效时间")
            if connection.execute(
                "SELECT 1 FROM procedure_versions WHERE procedure_id=? LIMIT 1",
                (procedure_id,),
            ).fetchone() is None:
                raise NotFoundError("项目规程不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO qualifications(qualification_id,actor_id,procedure_id,"
                        "valid_from,valid_until,revoked,granted_by,granted_at) "
                        "VALUES(?,?,?,?,?,0,?,?)",
                        (qualification_id, target_actor_id, procedure_id, valid_from,
                         valid_until, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资格编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="qualification.granted",
                             resource_type="qualification", resource_id=qualification_id,
                             detail={"target_actor_id": target_actor_id,
                                     "procedure_id": procedure_id,
                                     "valid_from": valid_from, "valid_until": valid_until},
                             occurred_at=self._now())
                return ("qualification", qualification_id,
                        {"qualification_id": qualification_id})

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_qualification", payload=payload, create=create)

    def revoke_qualification(self, *, request_id: str, actor_id: str,
                             qualification_id: str) -> dict[str, Any]:
        """撤销资格；撤销后不能再用于开项。"""

        payload = {"actor_id": actor_id, "qualification_id": qualification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            qualification_id = self._identifier(qualification_id, "qualification_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM qualifications WHERE qualification_id=?", (qualification_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("资格不存在")
                if row["revoked"]:
                    return ("qualification", qualification_id,
                            {"qualification_id": qualification_id, "revoked": True})
                connection.execute(
                    "UPDATE qualifications SET revoked=1 WHERE qualification_id=?",
                    (qualification_id,),
                )
                append_event(connection, actor_id=actor_id, action="qualification.revoked",
                             resource_type="qualification", resource_id=qualification_id,
                             detail={"target_actor_id": row["actor_id"],
                                     "procedure_id": row["procedure_id"]},
                             occurred_at=self._now())
                return ("qualification", qualification_id,
                        {"qualification_id": qualification_id, "revoked": True})

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_qualification", payload=payload, create=create)

    def record_screening(self, *, request_id: str, actor_id: str, screening_id: str,
                         site_id: str, procedure_id: str, participant_id: str,
                         conclusion: str, valid_until: str,
                         detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """登记参与者针对某项目的筛查结论。"""

        payload = {"actor_id": actor_id, "screening_id": screening_id, "site_id": site_id,
                   "procedure_id": procedure_id, "participant_id": participant_id,
                   "conclusion": conclusion, "valid_until": valid_until, "detail": detail or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            site = self._site(connection, site_id)
            self._same_org_or_admin(actor, site)
            screening_id = self._identifier(screening_id, "screening_id")
            procedure_id = self._identifier(procedure_id, "procedure_id")
            participant_id = self._identifier(participant_id, "participant_id")
            if conclusion not in SCREENING_CONCLUSIONS:
                raise ValidationError("筛查结论只能是 fit、unfit 或 conditional")
            valid_until = self._timestamp(valid_until, "valid_until")
            detail = detail or {}
            if not isinstance(detail, dict):
                raise ValidationError("detail 必须是对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute(
                    "SELECT 1 FROM procedure_versions WHERE procedure_id=? LIMIT 1",
                    (procedure_id,),
                ).fetchone() is None:
                    raise NotFoundError("项目规程不存在")
                try:
                    connection.execute(
                        "INSERT INTO screenings(screening_id,site_id,procedure_id,"
                        "participant_id,conclusion,detail_json,decided_by,decided_at,"
                        "valid_until) VALUES(?,?,?,?,?,?,?,?,?)",
                        (screening_id, site_id, procedure_id, participant_id, conclusion,
                         canonical_json(detail), actor_id, self._now(), valid_until),
                    )
                except Exception as exc:
                    raise ConflictError("筛查编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="screening.recorded",
                             resource_type="screening", resource_id=screening_id,
                             detail={"site_id": site_id, "procedure_id": procedure_id,
                                     "participant_id": participant_id,
                                     "conclusion": conclusion, "valid_until": valid_until},
                             occurred_at=self._now())
                return ("screening", screening_id, {"screening_id": screening_id})

            return self._idempotent(connection, request_id=request_id,
                                    action="record_screening", payload=payload, create=create)

    # -------------------------------------------------------------- 器材与领用

    def register_equipment_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                                 site_id: str, name: str) -> dict[str, Any]:
        """登记可被整体封停的器材批次。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._same_org_or_admin(actor, site)
            batch_id = self._identifier(batch_id, "batch_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO equipment_batches(batch_id,site_id,name,status,created_at) "
                        "VALUES(?,?,?,'active',?)",
                        (batch_id, site_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("器材批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="equipment_batch.registered",
                             resource_type="equipment_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "name": name},
                             occurred_at=self._now())
                return ("equipment_batch", batch_id, {"batch_id": batch_id, "status": "active"})

            return self._idempotent(connection, request_id=request_id,
                                    action="register_equipment_batch", payload=payload,
                                    create=create)

    def loan_equipment(self, *, request_id: str, actor_id: str, loan_id: str,
                       batch_id: str, borrower_actor_id: str) -> dict[str, Any]:
        """建立器材批次对执行人员的领用关系（未归还前有效）。"""

        payload = {"actor_id": actor_id, "loan_id": loan_id, "batch_id": batch_id,
                   "borrower_actor_id": borrower_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = connection.execute(
                "SELECT * FROM equipment_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("器材批次不存在")
            site = self._site(connection, batch["site_id"])
            self._same_org_or_admin(actor, site)
            borrower = self._actor(connection, borrower_actor_id)
            if borrower["organization_id"] != site["organization_id"] and borrower["role"] != "admin":
                raise PermissionDenied("不能把器材领给其他组织的执行人员")
            loan_id = self._identifier(loan_id, "loan_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO equipment_loans(loan_id,batch_id,site_id,"
                        "borrower_actor_id,loaned_at,returned_at) VALUES(?,?,?,?,?,NULL)",
                        (loan_id, batch_id, batch["site_id"], borrower_actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("领用编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="equipment.loaned",
                             resource_type="equipment_loan", resource_id=loan_id,
                             detail={"batch_id": batch_id,
                                     "borrower_actor_id": borrower_actor_id},
                             occurred_at=self._now())
                return ("equipment_loan", loan_id, {"loan_id": loan_id})

            return self._idempotent(connection, request_id=request_id,
                                    action="loan_equipment", payload=payload, create=create)

    def return_equipment(self, *, request_id: str, actor_id: str, loan_id: str) -> dict[str, Any]:
        """登记器材归还；暂停状态下仍允许归还，但领用关系不再可用于新开项。"""

        payload = {"actor_id": actor_id, "loan_id": loan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            loan_id = self._identifier(loan_id, "loan_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM equipment_loans WHERE loan_id=?", (loan_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("领用关系不存在")
                if row["returned_at"]:
                    return ("equipment_loan", loan_id,
                            {"loan_id": loan_id, "returned_at": row["returned_at"]})
                now = self._now()
                connection.execute(
                    "UPDATE equipment_loans SET returned_at=? WHERE loan_id=?", (now, loan_id)
                )
                append_event(connection, actor_id=actor_id, action="equipment.returned",
                             resource_type="equipment_loan", resource_id=loan_id,
                             detail={"batch_id": row["batch_id"]}, occurred_at=now)
                return ("equipment_loan", loan_id, {"loan_id": loan_id, "returned_at": now})

            return self._idempotent(connection, request_id=request_id,
                                    action="return_equipment", payload=payload, create=create)

    # -------------------------------------------------------------- 开项四验

    def _precheck(self, connection, *, site_id: str, procedure_id: str, version: str,
                  operator_actor_id: str, qualification_id: str, participant_id: str,
                  screening_id: str, loan_id: str) -> dict[str, Any]:
        """在事务内同时核验四项条件，任一失效都抛出 PreconditionFailed。"""

        now_dt = self.clock.now()
        now = self._now()

        site_row = self._site(connection, site_id)
        operator = self._actor(connection, operator_actor_id)
        if operator["organization_id"] != site_row["organization_id"] and operator["role"] != "admin":
            raise PermissionDenied("不能在其他组织的站点开展操作")

        procedure = connection.execute(
            "SELECT * FROM procedure_versions WHERE procedure_id=? AND version=?",
            (procedure_id, version),
        ).fetchone()
        if procedure is None:
            raise PreconditionFailed("规程版本不存在或未发布，不能开项")

        qualification = connection.execute(
            "SELECT * FROM qualifications WHERE qualification_id=?", (qualification_id,)
        ).fetchone()
        if qualification is None:
            raise PreconditionFailed("执行资格不存在，不能开项")
        if qualification["actor_id"] != operator_actor_id:
            raise PreconditionFailed("资格不属于当前执行人员，不能开项")
        if qualification["procedure_id"] != procedure_id:
            raise PreconditionFailed("资格对应的项目与规程不一致，不能开项")
        if qualification["revoked"]:
            raise PreconditionFailed("执行资格已被撤销，不能开项")
        valid_from = self._parse_ts(qualification["valid_from"], "valid_from")
        valid_until = self._parse_ts(qualification["valid_until"], "valid_until")
        if not (valid_from <= now_dt < valid_until):
            raise PreconditionFailed("执行资格已过有效期限，不能开项")

        screening = connection.execute(
            "SELECT * FROM screenings WHERE screening_id=?", (screening_id,)
        ).fetchone()
        if screening is None:
            raise PreconditionFailed("参与者筛查结论不存在，不能开项")
        if screening["site_id"] != site_id:
            raise PreconditionFailed("筛查结论不属于当前站点，不能开项")
        if screening["procedure_id"] != procedure_id:
            raise PreconditionFailed("筛查结论对应的项目与规程不一致，不能开项")
        if screening["participant_id"] != participant_id:
            raise PreconditionFailed("筛查结论与参与者不一致，不能开项")
        if screening["conclusion"] == "unfit":
            raise PreconditionFailed("筛查结论为不适宜，不能开项")
        if screening["conclusion"] not in ("fit", "conditional"):
            raise PreconditionFailed("筛查结论无效，不能开项")
        if not self._parse_ts(screening["valid_until"], "screening.valid_until") > now_dt:
            raise PreconditionFailed("筛查结论已过有效期，不能开项")

        loan = connection.execute(
            "SELECT * FROM equipment_loans WHERE loan_id=?", (loan_id,)
        ).fetchone()
        if loan is None:
            raise PreconditionFailed("器材领用关系不存在，不能开项")
        if loan["site_id"] != site_id:
            raise PreconditionFailed("器材不属于当前站点，不能开项")
        if loan["borrower_actor_id"] != operator_actor_id:
            raise PreconditionFailed("器材不是由当前执行人员领用，不能开项")
        if loan["returned_at"] is not None:
            raise PreconditionFailed("器材已经归还，领用关系失效，不能开项")
        batch = connection.execute(
            "SELECT * FROM equipment_batches WHERE batch_id=?", (loan["batch_id"],)
        ).fetchone()
        if batch is None:
            raise PreconditionFailed("器材批次不存在，不能开项")
        active_suspension = connection.execute(
            "SELECT 1 FROM suspensions WHERE batch_id=? AND status='active' LIMIT 1",
            (batch["batch_id"],),
        ).fetchone()
        if active_suspension or batch["status"] != "active":
            raise PreconditionFailed("器材批次处于安全暂停边界内，不能开项")

        return {
            "checked_at": now,
            "procedure": {"procedure_id": procedure_id, "version": version,
                          "title": procedure["title"], "content_hash": procedure["content_hash"]},
            "qualification": {"qualification_id": qualification_id,
                              "valid_from": qualification["valid_from"],
                              "valid_until": qualification["valid_until"]},
            "screening": {"screening_id": screening_id,
                          "conclusion": screening["conclusion"],
                          "valid_until": screening["valid_until"]},
            "equipment": {"loan_id": loan_id, "batch_id": batch["batch_id"]},
        }

    def start_session(self, *, request_id: str, actor_id: str, session_id: str, site_id: str,
                      procedure_id: str, procedure_version: str, qualification_id: str,
                      participant_id: str, screening_id: str, loan_id: str) -> dict[str, Any]:
        """核验四项条件通过后，使过程进入执行状态。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "site_id": site_id,
                   "procedure_id": procedure_id, "procedure_version": procedure_version,
                   "qualification_id": qualification_id, "participant_id": participant_id,
                   "screening_id": screening_id, "loan_id": loan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session_id = self._identifier(session_id, "session_id")
            site_id = self._identifier(site_id, "site_id")
            procedure_id = self._identifier(procedure_id, "procedure_id")
            procedure_version = self._identifier(procedure_version, "procedure_version")
            qualification_id = self._identifier(qualification_id, "qualification_id")
            participant_id = self._identifier(participant_id, "participant_id")
            screening_id = self._identifier(screening_id, "screening_id")
            loan_id = self._identifier(loan_id, "loan_id")
            precheck = self._precheck(
                connection, site_id=site_id, procedure_id=procedure_id,
                version=procedure_version, operator_actor_id=actor_id,
                qualification_id=qualification_id, participant_id=participant_id,
                screening_id=screening_id, loan_id=loan_id,
            )

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute(
                    "SELECT 1 FROM safety_sessions WHERE session_id=?", (session_id,)
                ).fetchone():
                    raise ConflictError("体验过程编号已经存在")
                now = self._now()
                connection.execute(
                    "INSERT INTO safety_sessions(session_id,site_id,procedure_id,"
                    "procedure_version,procedure_hash,operator_actor_id,qualification_id,"
                    "participant_id,screening_id,loan_id,batch_id,state,precheck_json,"
                    "sealing_incident_no,started_at,ended_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'running',?,NULL,?,NULL)",
                    (session_id, site_id, procedure_id, procedure_version,
                     precheck["procedure"]["content_hash"], actor_id, qualification_id,
                     participant_id, screening_id, loan_id,
                     precheck["equipment"]["batch_id"], canonical_json(precheck), now),
                )
                append_event(connection, actor_id=actor_id, action="safety_session.started",
                             resource_type="safety_session", resource_id=session_id,
                             detail={"site_id": site_id, "procedure_id": procedure_id,
                                     "procedure_version": procedure_version,
                                     "participant_id": participant_id,
                                     "batch_id": precheck["equipment"]["batch_id"],
                                     "precheck_hash": digest(precheck)},
                             occurred_at=now)
                return ("safety_session", session_id,
                        {"session_id": session_id, "state": "running", "precheck": precheck})

            return self._idempotent(connection, request_id=request_id,
                                    action="start_session", payload=payload, create=create)

    # -------------------------------------------------------------- 事实追加

    def _append_fact(self, connection, *, session_id: str, kind: str, payload: dict[str, Any],
                     recorded_by: str, effective: bool,
                     ineffective_reason: str | None = None, occurred_at: str | None = None) -> str:
        """追加一条带哈希链的过程事实；调用方负责状态判断。"""

        occurred_at = occurred_at or self._now()
        next_seq_row = connection.execute(
            "SELECT COALESCE(MAX(fact_seq), 0) + 1 AS next_seq FROM session_facts WHERE session_id=?",
            (session_id,),
        ).fetchone()
        fact_seq = next_seq_row["next_seq"]
        last = connection.execute(
            "SELECT fact_hash FROM session_facts WHERE session_id=? ORDER BY fact_seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        previous_hash = last["fact_hash"] if last else GENESIS_HASH
        fact_id = uuid.uuid4().hex
        material = {
            "fact_id": fact_id,
            "session_id": session_id,
            "fact_seq": fact_seq,
            "kind": kind,
            "payload": payload,
            "effective": effective,
            "ineffective_reason": ineffective_reason,
            "recorded_by": recorded_by,
            "occurred_at": occurred_at,
            "previous_hash": previous_hash,
        }
        fact_hash = digest(material)
        connection.execute(
            "INSERT INTO session_facts(fact_id,session_id,fact_seq,kind,payload_json,"
            "payload_hash,effective,ineffective_reason,recorded_by,occurred_at,"
            "previous_hash,fact_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (fact_id, session_id, fact_seq, kind, canonical_json(payload), digest(payload),
             1 if effective else 0, ineffective_reason, recorded_by, occurred_at,
             previous_hash, fact_hash),
        )
        return fact_id

    def append_session_fact(self, *, request_id: str, actor_id: str, session_id: str,
                            kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """在执行中的过程上追加观察事实；过程终结后事实仍可记录但不再生效。"""

        if kind not in APPENDABLE_FACT_KINDS:
            raise ValidationError("事实类别不受支持，系统事实只能由处置动作生成")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        payload = {"actor_id": actor_id, "session_id": session_id, "kind": kind, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            session_id = self._identifier(session_id, "session_id")
            session = connection.execute(
                "SELECT * FROM safety_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise NotFoundError("体验过程不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                effective = session["state"] == "running"
                reason = None if effective else f"过程已处于 {session['state']} 状态，事实仅留存不生效"
                fact_id = self._append_fact(
                    connection, session_id=session_id, kind=kind, payload=payload["payload"],
                    recorded_by=actor_id, effective=effective, ineffective_reason=reason,
                )
                return ("session_fact", fact_id,
                        {"fact_id": fact_id, "session_id": session_id, "state": session["state"],
                         "effective": effective})

            return self._idempotent(connection, request_id=request_id,
                                    action="append_session_fact", payload=payload, create=create)

    def complete_session(self, *, request_id: str, actor_id: str, session_id: str,
                         outcome: dict[str, Any] | None = None) -> dict[str, Any]:
        """登记正常完成回执；迟到的回执不能覆盖封存或暂停决定。"""

        outcome = outcome or {}
        if not isinstance(outcome, dict):
            raise ValidationError("outcome 必须是对象")
        payload = {"actor_id": actor_id, "session_id": session_id, "outcome": outcome}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            session_id = self._identifier(session_id, "session_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                session = connection.execute(
                    "SELECT * FROM safety_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise NotFoundError("体验过程不存在")
                now = self._now()
                if session["state"] == "running":
                    connection.execute(
                        "UPDATE safety_sessions SET state='completed', ended_at=? WHERE session_id=?",
                        (now, session_id),
                    )
                    fact_id = self._append_fact(
                        connection, session_id=session_id, kind="completion",
                        payload=outcome, recorded_by=actor_id, effective=True, occurred_at=now,
                    )
                    append_event(connection, actor_id=actor_id,
                                 action="safety_session.completed",
                                 resource_type="safety_session", resource_id=session_id,
                                 detail={"fact_id": fact_id}, occurred_at=now)
                    return ("safety_session", session_id,
                            {"session_id": session_id, "state": "completed",
                             "effective": True, "fact_id": fact_id})
                # 封存或暂停决定已经生效：回执只作为迟到事实留存
                fact_id = self._append_fact(
                    connection, session_id=session_id, kind="completion_receipt_late",
                    payload=outcome, recorded_by=actor_id, effective=False,
                    ineffective_reason=(
                        f"安全决定已生效（{session['state']}），迟到的正常回执不能改变过程状态"
                    ),
                    occurred_at=now,
                )
                append_event(connection, actor_id=actor_id,
                             action="safety_session.completion_receipt_ignored",
                             resource_type="safety_session", resource_id=session_id,
                             detail={"fact_id": fact_id, "state": session["state"]},
                             occurred_at=now)
                return ("safety_session", session_id,
                        {"session_id": session_id, "state": session["state"],
                         "effective": False, "fact_id": fact_id,
                         "reason": "late_receipt_cannot_override_safety_decision"})

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_session", payload=payload, create=create)

    # -------------------------------------------------------------- 异常与封存

    def report_incident(self, *, request_id: str, actor_id: str, incident_no: str,
                        session_id: str, summary: str, severity: str = "major",
                        facts: dict[str, Any] | None = None) -> dict[str, Any]:
        """原子地封存过程、封停同批器材及在施操作并生成待复核清单。"""

        facts = facts or {}
        if not isinstance(facts, dict):
            raise ValidationError("facts 必须是对象")
        summary = str(summary).strip()
        if not summary:
            raise ValidationError("summary 不能为空")
        if severity not in SEVERITIES:
            raise ValidationError("severity 只能是 minor、major 或 critical")
        fact_bundle = {"summary": summary, "severity": severity, "facts": facts}
        facts_hash = digest(fact_bundle)
        payload = {"actor_id": actor_id, "incident_no": incident_no,
                   "session_id": session_id, "facts_hash": facts_hash}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            request_id_value = self._identifier(request_id, "request_id")
            payload_hash = digest(payload)
            receipt_row = connection.execute(
                "SELECT * FROM request_receipts WHERE request_id=?", (request_id_value,)
            ).fetchone()
            if receipt_row:
                if receipt_row["action"] != "report_incident" or receipt_row["payload_hash"] != payload_hash:
                    raise ConflictError("request_id 已被不同内容使用")
                response = json.loads(receipt_row["response_json"])
                response["replayed"] = True
                return response
            incident_no = self._identifier(incident_no, "incident_no")
            session_id = self._identifier(session_id, "session_id")

            existing = connection.execute(
                "SELECT * FROM incidents WHERE incident_no=?", (incident_no,)
            ).fetchone()
            if existing:
                # 同一事件重放必须返回原结果；事件号相同而事实不同一律拒绝
                if existing["facts_hash"] != facts_hash:
                    raise ConflictError("事件号已存在但事实内容不同，拒绝重复上报")
                result = json.loads(existing["result_json"])
                connection.execute(
                    "INSERT INTO request_receipts(request_id,action,payload_hash,"
                    "resource_type,resource_id,response_json,created_at) "
                    "VALUES(?,?,?, 'incident',?,?,?)",
                    (request_id_value, "report_incident", payload_hash, incident_no,
                     canonical_json(result), self._now()),
                )
                result["replayed"] = True
                return result

            session = connection.execute(
                "SELECT * FROM safety_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise NotFoundError("体验过程不存在")
            if session["state"] == "sealed":
                raise PreconditionFailed(
                    f"过程已被事件 {session['sealing_incident_no']} 封存，不能重复上报异常"
                )
            if session["state"] == "completed":
                raise PreconditionFailed("过程已正常完成，不能再封存")

            now = self._now()
            batch_id = session["batch_id"]

            # 1) 封存本次过程（源头过程）
            connection.execute(
                "UPDATE safety_sessions SET state='sealed', sealing_incident_no=?, ended_at=? "
                "WHERE session_id=?",
                (incident_no, now, session_id),
            )
            incident_fact_id = self._append_fact(
                connection, session_id=session_id, kind="incident_declared",
                payload={"incident_no": incident_no, **fact_bundle},
                recorded_by=actor_id, effective=False,
                ineffective_reason="异常事件宣告，过程已当场封存", occurred_at=now,
            )

            # 2) 确定同批器材的其他在施操作并一并封停
            affected_sessions = [row["session_id"] for row in connection.execute(
                "SELECT session_id FROM safety_sessions WHERE batch_id=? AND state='running' "
                "ORDER BY started_at, session_id",
                (batch_id,),
            )]
            for other_id in affected_sessions:
                connection.execute(
                    "UPDATE safety_sessions SET state='suspended', ended_at=? WHERE session_id=?",
                    (now, other_id),
                )
                self._append_fact(
                    connection, session_id=other_id, kind="incident_declared",
                    payload={"incident_no": incident_no, "origin_session_id": session_id,
                             "reason": "同批器材发生异常，关联操作暂停"},
                    recorded_by=actor_id, effective=False,
                    ineffective_reason="同批器材安全暂停，过程挂起等待复核", occurred_at=now,
                )

            # 3) 封停器材批次并落下暂停边界
            batch = connection.execute(
                "SELECT * FROM equipment_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            batch_was_suspended = batch["status"] == "suspended"
            if not batch_was_suspended:
                connection.execute(
                    "UPDATE equipment_batches SET status='suspended' WHERE batch_id=?",
                    (batch_id,),
                )
            suspension_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO suspensions(suspension_id,incident_no,batch_id,status,"
                "suspended_at,lifted_at,lifted_by) VALUES(?,?,?,'active',?,NULL,NULL)",
                (suspension_id, incident_no, batch_id, now),
            )

            # 4) 生成待复核清单：源头过程、受影响在施操作、器材批次逐项列出
            review_targets = [("session", session_id)]
            review_targets += [("session", other) for other in affected_sessions]
            review_targets.append(("batch", batch_id))
            review_item_ids: list[str] = []
            for target_type, target_id in review_targets:
                item_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_items(item_id,incident_no,target_type,target_id,"
                    "status,evidence_ref,resolution_note,resolved_by,resolved_at) "
                    "VALUES(?,?,?,?,'pending',NULL,NULL,NULL,NULL)",
                    (item_id, incident_no, target_type, target_id),
                )
                connection.execute(
                    "INSERT INTO incident_affected(id,incident_no,target_type,target_id,action) "
                    "VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, incident_no, target_type, target_id,
                     "sealed" if target_id == session_id else
                     ("suspended" if target_type == "session" else "batch_suspended")),
                )
                review_item_ids.append(item_id)

            # 5) 异常事件落库（结果一并固化，供重放原样返回）
            result = {
                "incident_no": incident_no,
                "origin_session_id": session_id,
                "sealed_session_id": session_id,
                "suspended_batch_ids": [batch_id],
                "affected_session_ids": affected_sessions,
                "review_item_ids": review_item_ids,
                "facts_hash": facts_hash,
                "incident_fact_id": incident_fact_id,
                "suspension_id": suspension_id,
            }
            connection.execute(
                "INSERT INTO incidents(incident_no,facts_hash,origin_session_id,severity,"
                "summary,detail_json,recorded_by,occurred_at,created_at,result_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (incident_no, facts_hash, session_id, severity, summary,
                 canonical_json(fact_bundle), actor_id, now, now, canonical_json(result)),
            )
            append_event(connection, actor_id=actor_id, action="incident.reported",
                         resource_type="incident", resource_id=incident_no,
                         detail={"origin_session_id": session_id, "batch_id": batch_id,
                                 "affected_session_ids": affected_sessions,
                                 "review_item_count": len(review_item_ids),
                                 "facts_hash": facts_hash},
                         occurred_at=now)
            connection.execute(
                "INSERT INTO request_receipts(request_id,action,payload_hash,"
                "resource_type,resource_id,response_json,created_at) "
                "VALUES(?,?,?, 'incident',?,?,?)",
                (request_id_value, "report_incident", payload_hash, incident_no,
                 canonical_json(result), now),
            )
            result["replayed"] = False
            return result

    # -------------------------------------------------------------- 复核与解除

    def resolve_review_item(self, *, request_id: str, actor_id: str, item_id: str,
                            evidence_ref: str, resolution_note: str = "") -> dict[str, Any]:
        """复核人员为单个清单项关联处置证据；全部处置完才解除暂停。"""

        payload = {"actor_id": actor_id, "item_id": item_id,
                   "evidence_ref": evidence_ref, "resolution_note": resolution_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            item_id = self._identifier(item_id, "item_id")
            evidence_ref = self._text(evidence_ref, "evidence_ref", 300)
            resolution_note = str(resolution_note or "").strip()

            def create() -> tuple[str, str, dict[str, Any]]:
                item = connection.execute(
                    "SELECT * FROM review_items WHERE item_id=?", (item_id,)
                ).fetchone()
                if item is None:
                    raise NotFoundError("待复核项不存在")
                if item["status"] == "resolved":
                    raise ConflictError("该复核项已经关联证据并处置完成")
                now = self._now()
                # 触发器会再次强制：必须带证据、已解决结项不可改写
                connection.execute(
                    "UPDATE review_items SET status='resolved', evidence_ref=?, "
                    "resolution_note=?, resolved_by=?, resolved_at=? WHERE item_id=?",
                    (evidence_ref, resolution_note, actor_id, now, item_id),
                )
                append_event(connection, actor_id=actor_id, action="review_item.resolved",
                             resource_type="review_item", resource_id=item_id,
                             detail={"incident_no": item["incident_no"],
                                     "target_type": item["target_type"],
                                     "target_id": item["target_id"],
                                     "evidence_ref": evidence_ref},
                             occurred_at=now)

                lifted = self._maybe_lift_suspension(connection, incident_no=item["incident_no"],
                                                     actor_id=actor_id, now=now)
                return ("review_item", item_id,
                        {"item_id": item_id, "incident_no": item["incident_no"],
                         "status": "resolved", "suspension_lifted": lifted})

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_review_item", payload=payload, create=create)

    def _maybe_lift_suspension(self, connection, *, incident_no: str,
                               actor_id: str, now: str) -> bool:
        """同一事件清单逐项全部处置完成时，解除其暂停边界。"""

        pending = connection.execute(
            "SELECT COUNT(*) AS count FROM review_items WHERE incident_no=? AND status='pending'",
            (incident_no,),
        ).fetchone()["count"]
        if pending:
            return False
        suspensions = connection.execute(
            "SELECT * FROM suspensions WHERE incident_no=? AND status='active'",
            (incident_no,),
        ).fetchall()
        lifted_any = False
        for suspension in suspensions:
            batch_id = suspension["batch_id"]
            # 只有不存在任何其他事件的活跃暂停时，批次才恢复可用
            other_active = connection.execute(
                "SELECT 1 FROM suspensions WHERE batch_id=? AND status='active' "
                "AND incident_no!=? LIMIT 1",
                (batch_id, incident_no),
            ).fetchone()
            connection.execute(
                "UPDATE suspensions SET status='lifted', lifted_at=?, lifted_by=? "
                "WHERE suspension_id=?",
                (now, actor_id, suspension["suspension_id"]),
            )
            if not other_active:
                connection.execute(
                    "UPDATE equipment_batches SET status='active' WHERE batch_id=?",
                    (batch_id,),
                )
            append_event(connection, actor_id=actor_id, action="suspension.lifted",
                         resource_type="suspension",
                         resource_id=suspension["suspension_id"],
                         detail={"incident_no": incident_no, "batch_id": batch_id,
                                 "still_held_by_other_incident": bool(other_active)},
                         occurred_at=now)
            lifted_any = True
        return lifted_any

    # -------------------------------------------------------------- 查询与追溯

    def get_session(self, session_id: str) -> SafetySession:
        row = self.database.connection.execute(
            "SELECT * FROM safety_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("体验过程不存在")
        return SafetySession(
            row["session_id"], row["site_id"], row["procedure_id"], row["procedure_version"],
            row["procedure_hash"], row["operator_actor_id"], row["qualification_id"],
            row["participant_id"], row["screening_id"], row["loan_id"], row["batch_id"],
            row["state"], json.loads(row["precheck_json"]), row["sealing_incident_no"],
            row["started_at"], row["ended_at"],
        )

    def list_session_facts(self, session_id: str) -> list[SessionFact]:
        rows = self.database.connection.execute(
            "SELECT * FROM session_facts WHERE session_id=? ORDER BY fact_seq", (session_id,)
        ).fetchall()
        return [SessionFact(
            row["fact_id"], row["session_id"], row["fact_seq"], row["kind"],
            json.loads(row["payload_json"]), row["payload_hash"], bool(row["effective"]),
            row["ineffective_reason"], row["recorded_by"], row["occurred_at"],
            row["previous_hash"], row["fact_hash"],
        ) for row in rows]

    def verify_session_facts(self, session_id: str) -> tuple[bool, int]:
        """校验某一过程的事实哈希链，证明追加记录未被篡改。"""

        previous_hash = GENESIS_HASH
        count = 0
        for row in self.database.connection.execute(
            "SELECT * FROM session_facts WHERE session_id=? ORDER BY fact_seq", (session_id,)
        ):
            material = {
                "fact_id": row["fact_id"], "session_id": row["session_id"],
                "fact_seq": row["fact_seq"], "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "effective": bool(row["effective"]),
                "ineffective_reason": row["ineffective_reason"],
                "recorded_by": row["recorded_by"], "occurred_at": row["occurred_at"],
                "previous_hash": row["previous_hash"],
            }
            if row["previous_hash"] != previous_hash or digest(material) != row["fact_hash"]:
                return False, count
            previous_hash = row["fact_hash"]
            count += 1
        return True, count

    def list_review_items(self, incident_no: str | None = None,
                          status: str | None = None) -> list[ReviewItem]:
        query = "SELECT * FROM review_items WHERE 1=1"
        parameters: list[Any] = []
        if incident_no:
            query += " AND incident_no=?"
            parameters.append(incident_no)
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY incident_no, target_type, target_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [ReviewItem(
            row["item_id"], row["incident_no"], row["target_type"], row["target_id"],
            row["status"], row["evidence_ref"], row["resolution_note"],
            row["resolved_by"], row["resolved_at"],
        ) for row in rows]

    def list_suspensions(self, status: str | None = None) -> list[Suspension]:
        query = "SELECT * FROM suspensions"
        parameters: list[Any] = []
        if status:
            query += " WHERE status=?"
            parameters.append(status)
        query += " ORDER BY suspended_at, suspension_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [Suspension(
            row["suspension_id"], row["incident_no"], row["batch_id"], row["status"],
            row["suspended_at"], row["lifted_at"], row["lifted_by"],
        ) for row in rows]

    def get_incident(self, incident_no: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM incidents WHERE incident_no=?", (incident_no,)
        ).fetchone()
        if row is None:
            raise NotFoundError("异常事件不存在")
        return {
            "incident_no": row["incident_no"], "facts_hash": row["facts_hash"],
            "origin_session_id": row["origin_session_id"], "severity": row["severity"],
            "summary": row["summary"], "detail": json.loads(row["detail_json"]),
            "recorded_by": row["recorded_by"], "occurred_at": row["occurred_at"],
            "created_at": row["created_at"], "result": json.loads(row["result_json"]),
        }

    def session_trace(self, session_id: str) -> dict[str, Any]:
        """从任一次体验追溯规程、人员、器材与全部风险处置记录。"""

        connection = self.database.connection
        session_row = connection.execute(
            "SELECT * FROM safety_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if session_row is None:
            raise NotFoundError("体验过程不存在")

        procedure_row = connection.execute(
            "SELECT * FROM procedure_versions WHERE procedure_id=? AND version=?",
            (session_row["procedure_id"], session_row["procedure_version"]),
        ).fetchone()
        qualification_row = connection.execute(
            "SELECT * FROM qualifications WHERE qualification_id=?",
            (session_row["qualification_id"],),
        ).fetchone()
        screening_row = connection.execute(
            "SELECT * FROM screenings WHERE screening_id=?", (session_row["screening_id"],)
        ).fetchone()
        loan_row = connection.execute(
            "SELECT * FROM equipment_loans WHERE loan_id=?", (session_row["loan_id"],)
        ).fetchone()
        batch_row = connection.execute(
            "SELECT * FROM equipment_batches WHERE batch_id=?", (session_row["batch_id"],)
        ).fetchone()

        incident_rows = connection.execute(
            "SELECT i.* FROM incidents i WHERE i.origin_session_id=? "
            "UNION "
            "SELECT i.* FROM incidents i JOIN incident_affected a ON a.incident_no=i.incident_no "
            "WHERE a.target_type='session' AND a.target_id=? "
            "ORDER BY occurred_at",
            (session_id, session_id),
        ).fetchall()
        incidents = [{
            "incident_no": row["incident_no"],
            "facts_hash": row["facts_hash"],
            "severity": row["severity"],
            "summary": row["summary"],
            "origin_session_id": row["origin_session_id"],
            "occurred_at": row["occurred_at"],
        } for row in incident_rows]

        incident_nos = [row["incident_no"] for row in incident_rows]
        suspensions: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []
        if incident_nos:
            placeholders = ",".join("?" for _ in incident_nos)
            for row in connection.execute(
                f"SELECT * FROM suspensions WHERE incident_no IN ({placeholders}) "
                "ORDER BY suspended_at",
                incident_nos,
            ):
                suspensions.append({
                    "suspension_id": row["suspension_id"], "incident_no": row["incident_no"],
                    "batch_id": row["batch_id"], "status": row["status"],
                    "suspended_at": row["suspended_at"], "lifted_at": row["lifted_at"],
                    "lifted_by": row["lifted_by"],
                })
            for row in connection.execute(
                f"SELECT * FROM review_items WHERE incident_no IN ({placeholders}) "
                "ORDER BY target_type, target_id",
                incident_nos,
            ):
                review_items.append({
                    "item_id": row["item_id"], "incident_no": row["incident_no"],
                    "target_type": row["target_type"], "target_id": row["target_id"],
                    "status": row["status"], "evidence_ref": row["evidence_ref"],
                    "resolution_note": row["resolution_note"],
                    "resolved_by": row["resolved_by"], "resolved_at": row["resolved_at"],
                })

        facts_valid, fact_count = self.verify_session_facts(session_id)
        return {
            "session": {
                "session_id": session_row["session_id"], "site_id": session_row["site_id"],
                "procedure_id": session_row["procedure_id"],
                "procedure_version": session_row["procedure_version"],
                "procedure_hash": session_row["procedure_hash"],
                "operator_actor_id": session_row["operator_actor_id"],
                "qualification_id": session_row["qualification_id"],
                "participant_id": session_row["participant_id"],
                "screening_id": session_row["screening_id"],
                "loan_id": session_row["loan_id"], "batch_id": session_row["batch_id"],
                "state": session_row["state"],
                "sealing_incident_no": session_row["sealing_incident_no"],
                "started_at": session_row["started_at"], "ended_at": session_row["ended_at"],
                "precheck": json.loads(session_row["precheck_json"]),
            },
            "procedure": {
                "procedure_id": procedure_row["procedure_id"],
                "version": procedure_row["version"], "title": procedure_row["title"],
                "content": json.loads(procedure_row["content_json"]),
                "content_hash": procedure_row["content_hash"],
                "published_by": procedure_row["published_by"],
                "published_at": procedure_row["published_at"],
            } if procedure_row else None,
            "qualification": {
                "qualification_id": qualification_row["qualification_id"],
                "actor_id": qualification_row["actor_id"],
                "procedure_id": qualification_row["procedure_id"],
                "valid_from": qualification_row["valid_from"],
                "valid_until": qualification_row["valid_until"],
                "revoked": bool(qualification_row["revoked"]),
            } if qualification_row else None,
            "screening": {
                "screening_id": screening_row["screening_id"],
                "site_id": screening_row["site_id"],
                "participant_id": screening_row["participant_id"],
                "conclusion": screening_row["conclusion"],
                "detail": json.loads(screening_row["detail_json"]),
                "valid_until": screening_row["valid_until"],
            } if screening_row else None,
            "loan": {
                "loan_id": loan_row["loan_id"], "batch_id": loan_row["batch_id"],
                "site_id": loan_row["site_id"],
                "borrower_actor_id": loan_row["borrower_actor_id"],
                "loaned_at": loan_row["loaned_at"], "returned_at": loan_row["returned_at"],
            } if loan_row else None,
            "batch": {
                "batch_id": batch_row["batch_id"], "site_id": batch_row["site_id"],
                "name": batch_row["name"], "status": batch_row["status"],
            } if batch_row else None,
            "facts": [fact.__dict__ for fact in self.list_session_facts(session_id)],
            "facts_valid": facts_valid,
            "fact_count": fact_count,
            "incidents": incidents,
            "suspensions": suspensions,
            "review_items": review_items,
        }
