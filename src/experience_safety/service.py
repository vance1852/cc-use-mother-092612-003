"""提供适宜技术体验安全记录的领域规则。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from night_market_foundation.audit import append_event, canonical_json, digest
from night_market_foundation.clock import Clock
from night_market_foundation.errors import (ConflictError, NotFoundError, PermissionDenied,
                                            ValidationError)
from night_market_foundation.models import WriteReceipt
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from .models import ExperienceSession, ReviewItem, SessionFact, Suspension
from .storage import ensure_schema

CONCLUSIONS = frozenset({"cleared", "rejected"})
SEVERITIES = frozenset({"mild", "moderate", "severe"})
PROTOCOL_SCOPE = "protocol"
BATCH_SCOPE = "equipment_batch"
SUSPENSION_STATUSES = frozenset({"active", "lifted"})
REVIEW_STATUSES = frozenset({"pending", "resolved"})


class ExperienceSafetyService(DomainService):
    """在基础服务之上实现体验准入门禁、追加事实、异常封存与复核放行。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)
        ensure_schema(database)

    # ---------- 基础工具 ----------

    def _parse_time(self, value: str, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是合法的 ISO 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _session_model(row) -> ExperienceSession:
        return ExperienceSession(
            session_id=row["session_id"], site_id=row["site_id"], protocol_id=row["protocol_id"],
            protocol_version=row["protocol_version"], practitioner_id=row["practitioner_id"],
            participant_id=row["participant_id"], equipment_id=row["equipment_id"],
            checkout_id=row["checkout_id"], batch_no=row["batch_no"],
            screening_id=row["screening_id"], status=row["status"], started_at=row["started_at"],
            ended_at=row["ended_at"], sealed_by_incident=row["sealed_by_incident"])

    @staticmethod
    def _fact_model(row) -> SessionFact:
        return SessionFact(fact_id=row["fact_id"], session_id=row["session_id"], kind=row["kind"],
                           detail=json.loads(row["detail_json"]), recorded_by=row["recorded_by"],
                           recorded_at=row["recorded_at"])

    @staticmethod
    def _suspension_model(row) -> Suspension:
        return Suspension(suspension_id=row["suspension_id"], scope_type=row["scope_type"],
                          scope_key=row["scope_key"], status=row["status"],
                          created_by_incident=row["created_by_incident"], created_at=row["created_at"],
                          lifted_at=row["lifted_at"], lifted_by=row["lifted_by"])

    @staticmethod
    def _review_model(row) -> ReviewItem:
        return ReviewItem(review_id=row["review_id"], incident_id=row["incident_id"],
                          suspension_id=row["suspension_id"], subject_type=row["subject_type"],
                          subject_id=row["subject_id"], status=row["status"],
                          evidence_ref=row["evidence_ref"], resolution_note=row["resolution_note"],
                          resolved_by=row["resolved_by"], resolved_at=row["resolved_at"])

    @staticmethod
    def _protocol_scope_key(protocol_id: str, version: int) -> str:
        return f"{protocol_id}@{version}"

    # ---------- 档案登记 ----------

    def register_protocol(self, *, request_id: str, actor_id: str, protocol_id: str,
                          version: int, title: str, content: dict[str, Any]) -> WriteReceipt:
        """登记不可变的项目规程版本，同一版本号不能登记不同内容。"""

        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ValidationError("version 必须是正整数") from exc
        if version < 1:
            raise ValidationError("version 必须是正整数")
        payload = {"actor_id": actor_id, "protocol_id": protocol_id, "version": version,
                   "title": title, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            protocol_id = self._identifier(protocol_id, "protocol_id")
            title = self._text(title, "title")
            content_hash = digest({"title": title, "content": content})
            resource_id = self._protocol_scope_key(protocol_id, version)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM safety_protocols WHERE protocol_id=? AND version=?",
                    (protocol_id, version)).fetchone()
                if existing:
                    if existing["content_hash"] != content_hash:
                        raise ConflictError("规程版本不可变，同一版本不能登记不同内容")
                    return "safety_protocol", resource_id, {"protocol_id": protocol_id, "version": version}
                connection.execute(
                    "INSERT INTO safety_protocols(protocol_id,version,title,content_json,content_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (protocol_id, version, title, canonical_json(content), content_hash,
                     actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="safety.protocol_registered",
                             resource_type="safety_protocol", resource_id=resource_id,
                             detail={"title": title, "content_hash": content_hash},
                             occurred_at=self._now())
                return "safety_protocol", resource_id, {"protocol_id": protocol_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_protocol", payload=payload, create=create)

    def register_practitioner(self, *, request_id: str, actor_id: str, practitioner_id: str,
                              display_name: str, qualification_no: str,
                              valid_from: str, valid_until: str) -> WriteReceipt:
        """登记执行人员及其资格有效期限。"""

        payload = {"actor_id": actor_id, "practitioner_id": practitioner_id,
                   "display_name": display_name, "qualification_no": qualification_no,
                   "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            practitioner_id = self._identifier(practitioner_id, "practitioner_id")
            display_name = self._text(display_name, "display_name")
            qualification_no = self._text(qualification_no, "qualification_no", 80)
            start = self._parse_time(valid_from, "valid_from")
            end = self._parse_time(valid_until, "valid_until")
            if end < start:
                raise ValidationError("valid_until 不能早于 valid_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO safety_practitioners(practitioner_id,display_name,qualification_no,"
                        "valid_from,valid_until,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (practitioner_id, display_name, qualification_no, self._format_time(start),
                         self._format_time(end), actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("执行人员编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="safety.practitioner_registered",
                             resource_type="safety_practitioner", resource_id=practitioner_id,
                             detail={"display_name": display_name, "qualification_no": qualification_no,
                                     "valid_from": self._format_time(start),
                                     "valid_until": self._format_time(end)},
                             occurred_at=self._now())
                return "safety_practitioner", practitioner_id, {"practitioner_id": practitioner_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_practitioner", payload=payload, create=create)

    def register_participant(self, *, request_id: str, actor_id: str, participant_id: str,
                             display_name: str) -> WriteReceipt:
        """登记体验参与者。"""

        payload = {"actor_id": actor_id, "participant_id": participant_id, "display_name": display_name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            participant_id = self._identifier(participant_id, "participant_id")
            display_name = self._text(display_name, "display_name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO safety_participants(participant_id,display_name,created_by,created_at) "
                        "VALUES(?,?,?,?)",
                        (participant_id, display_name, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("参与者编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="safety.participant_registered",
                             resource_type="safety_participant", resource_id=participant_id,
                             detail={"display_name": display_name}, occurred_at=self._now())
                return "safety_participant", participant_id, {"participant_id": participant_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_participant", payload=payload, create=create)

    def register_screening(self, *, request_id: str, actor_id: str, screening_id: str,
                           participant_id: str, conclusion: str, valid_until: str) -> WriteReceipt:
        """登记参与者筛查结论及其有效期。"""

        payload = {"actor_id": actor_id, "screening_id": screening_id, "participant_id": participant_id,
                   "conclusion": conclusion, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            screening_id = self._identifier(screening_id, "screening_id")
            if conclusion not in CONCLUSIONS:
                raise ValidationError("conclusion 不在允许范围内")
            expires = self._parse_time(valid_until, "valid_until")
            if connection.execute("SELECT 1 FROM safety_participants WHERE participant_id=?",
                                  (participant_id,)).fetchone() is None:
                raise NotFoundError("参与者不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO safety_screenings(screening_id,participant_id,conclusion,valid_until,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (screening_id, participant_id, conclusion, self._format_time(expires),
                         actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("筛查记录编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="safety.screening_registered",
                             resource_type="safety_screening", resource_id=screening_id,
                             detail={"participant_id": participant_id, "conclusion": conclusion,
                                     "valid_until": self._format_time(expires)},
                             occurred_at=self._now())
                return "safety_screening", screening_id, {"screening_id": screening_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_screening", payload=payload, create=create)

    def register_equipment(self, *, request_id: str, actor_id: str, equipment_id: str,
                           name: str, batch_no: str) -> WriteReceipt:
        """登记器材及其所属批次。"""

        payload = {"actor_id": actor_id, "equipment_id": equipment_id,
                   "name": name, "batch_no": batch_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            equipment_id = self._identifier(equipment_id, "equipment_id")
            name = self._text(name, "name")
            batch_no = self._identifier(batch_no, "batch_no")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO safety_equipment(equipment_id,name,batch_no,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (equipment_id, name, batch_no, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("器材编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="safety.equipment_registered",
                             resource_type="safety_equipment", resource_id=equipment_id,
                             detail={"name": name, "batch_no": batch_no}, occurred_at=self._now())
                return "safety_equipment", equipment_id, {"equipment_id": equipment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_equipment", payload=payload, create=create)

    def issue_equipment(self, *, request_id: str, actor_id: str, checkout_id: str,
                        equipment_id: str, site_id: str) -> WriteReceipt:
        """登记器材领用到场所，一件器材同一时间只能有一条有效领用。"""

        payload = {"actor_id": actor_id, "checkout_id": checkout_id,
                   "equipment_id": equipment_id, "site_id": site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            checkout_id = self._identifier(checkout_id, "checkout_id")
            if connection.execute("SELECT 1 FROM safety_equipment WHERE equipment_id=?",
                                  (equipment_id,)).fetchone() is None:
                raise NotFoundError("器材不存在")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能操作其他组织的场所")

            def create() -> tuple[str, str, dict[str, Any]]:
                active = connection.execute(
                    "SELECT 1 FROM safety_checkouts WHERE equipment_id=? AND status='issued'",
                    (equipment_id,)).fetchone()
                if active:
                    raise ConflictError("器材存在未归还的领用记录")
                try:
                    connection.execute(
                        "INSERT INTO safety_checkouts(checkout_id,equipment_id,site_id,status,issued_at) "
                        "VALUES(?,?,?,'issued',?)",
                        (checkout_id, equipment_id, site_id, self._now()))
                except Exception as exc:
                    raise ConflictError("领用编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="safety.equipment_issued",
                             resource_type="safety_checkout", resource_id=checkout_id,
                             detail={"equipment_id": equipment_id, "site_id": site_id},
                             occurred_at=self._now())
                return "safety_checkout", checkout_id, {"checkout_id": checkout_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="issue_equipment", payload=payload, create=create)

    def return_equipment(self, *, request_id: str, actor_id: str, checkout_id: str) -> WriteReceipt:
        """登记器材归还，执行中的体验占用的器材不能归还。"""

        payload = {"actor_id": actor_id, "checkout_id": checkout_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            checkout = connection.execute("SELECT * FROM safety_checkouts WHERE checkout_id=?",
                                          (checkout_id,)).fetchone()
            if checkout is None:
                raise NotFoundError("领用记录不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if checkout["status"] != "issued":
                    raise ConflictError("领用记录已经归还")
                in_use = connection.execute(
                    "SELECT 1 FROM safety_sessions WHERE checkout_id=? AND status='executing'",
                    (checkout_id,)).fetchone()
                if in_use:
                    raise ConflictError("器材正在体验过程中使用，不能归还")
                connection.execute(
                    "UPDATE safety_checkouts SET status='returned', returned_at=? WHERE checkout_id=?",
                    (self._now(), checkout_id))
                append_event(connection, actor_id=actor_id, action="safety.equipment_returned",
                             resource_type="safety_checkout", resource_id=checkout_id,
                             detail={"equipment_id": checkout["equipment_id"]},
                             occurred_at=self._now())
                return "safety_checkout", checkout_id, {"checkout_id": checkout_id, "status": "returned"}

            return self._idempotent(connection, request_id=request_id,
                                    action="return_equipment", payload=payload, create=create)

    # ---------- 体验过程 ----------

    def start_session(self, *, request_id: str, actor_id: str, session_id: str, site_id: str,
                      protocol_id: str, protocol_version: int, practitioner_id: str,
                      participant_id: str, equipment_id: str) -> WriteReceipt:
        """在同一事务内核验全部准入门禁，任一条件失效都不能进入执行状态。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "site_id": site_id,
                   "protocol_id": protocol_id, "protocol_version": protocol_version,
                   "practitioner_id": practitioner_id, "participant_id": participant_id,
                   "equipment_id": equipment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            session_id = self._identifier(session_id, "session_id")
            try:
                protocol_version = int(protocol_version)
            except (TypeError, ValueError) as exc:
                raise ValidationError("protocol_version 必须是正整数") from exc
            if protocol_version < 1:
                raise ValidationError("protocol_version 必须是正整数")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能操作其他组织的场所")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self.clock.now()
                protocol = connection.execute(
                    "SELECT * FROM safety_protocols WHERE protocol_id=? AND version=?",
                    (protocol_id, protocol_version)).fetchone()
                if protocol is None:
                    raise NotFoundError("规程版本不存在")
                practitioner = connection.execute(
                    "SELECT * FROM safety_practitioners WHERE practitioner_id=?",
                    (practitioner_id,)).fetchone()
                if practitioner is None:
                    raise NotFoundError("执行人员不存在")
                if not (self._parse_time(practitioner["valid_from"], "valid_from") <= now
                        <= self._parse_time(practitioner["valid_until"], "valid_until")):
                    raise ValidationError("执行人员资格不在有效期内")
                if connection.execute("SELECT 1 FROM safety_participants WHERE participant_id=?",
                                      (participant_id,)).fetchone() is None:
                    raise NotFoundError("参与者不存在")
                screening = connection.execute(
                    "SELECT * FROM safety_screenings WHERE participant_id=? "
                    "ORDER BY created_at DESC, screening_id DESC LIMIT 1",
                    (participant_id,)).fetchone()
                if screening is None:
                    raise NotFoundError("参与者缺少筛查结论")
                if screening["conclusion"] != "cleared":
                    raise ValidationError("参与者筛查结论不适宜体验")
                if self._parse_time(screening["valid_until"], "valid_until") < now:
                    raise ValidationError("参与者筛查结论已过期")
                equipment = connection.execute(
                    "SELECT * FROM safety_equipment WHERE equipment_id=?",
                    (equipment_id,)).fetchone()
                if equipment is None:
                    raise NotFoundError("器材不存在")
                checkout = connection.execute(
                    "SELECT * FROM safety_checkouts WHERE equipment_id=? AND site_id=? AND status='issued'",
                    (equipment_id, site_id)).fetchone()
                if checkout is None:
                    raise ValidationError("器材未领用到本场所")
                in_use = connection.execute(
                    "SELECT 1 FROM safety_sessions WHERE equipment_id=? AND status='executing'",
                    (equipment_id,)).fetchone()
                if in_use:
                    raise ConflictError("器材正在其他体验过程中使用")
                batch_no = equipment["batch_no"]
                for scope_type, scope_key in (
                        (PROTOCOL_SCOPE, self._protocol_scope_key(protocol_id, protocol_version)),
                        (BATCH_SCOPE, batch_no)):
                    blocked = connection.execute(
                        "SELECT 1 FROM safety_suspensions WHERE scope_type=? AND scope_key=? "
                        "AND status='active'", (scope_type, scope_key)).fetchone()
                    if blocked:
                        raise ConflictError("相关项目或器材批次处于安全暂停中")
                try:
                    connection.execute(
                        "INSERT INTO safety_sessions(session_id,site_id,protocol_id,protocol_version,"
                        "practitioner_id,participant_id,equipment_id,checkout_id,batch_no,screening_id,"
                        "status,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,'executing',?)",
                        (session_id, site_id, protocol_id, protocol_version, practitioner_id,
                         participant_id, equipment_id, checkout["checkout_id"], batch_no,
                         screening["screening_id"], self._now()))
                except Exception as exc:
                    raise ConflictError("体验编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="safety.session_started",
                             resource_type="experience_session", resource_id=session_id,
                             detail={"site_id": site_id, "protocol_id": protocol_id,
                                     "protocol_version": protocol_version,
                                     "practitioner_id": practitioner_id,
                                     "participant_id": participant_id,
                                     "equipment_id": equipment_id, "batch_no": batch_no,
                                     "checkout_id": checkout["checkout_id"],
                                     "screening_id": screening["screening_id"]},
                             occurred_at=self._now())
                return "experience_session", session_id, {"session_id": session_id, "status": "executing"}

            return self._idempotent(connection, request_id=request_id,
                                    action="start_session", payload=payload, create=create)

    def append_fact(self, *, request_id: str, actor_id: str, session_id: str,
                    kind: str, detail: dict[str, Any]) -> WriteReceipt:
        """向执行中的体验追加事实，事实只增不改。"""

        if not isinstance(detail, dict) or not detail:
            raise ValidationError("detail 必须是非空对象")
        payload = {"actor_id": actor_id, "session_id": session_id, "kind": kind, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            session_id = self._identifier(session_id, "session_id")
            kind = self._text(kind, "kind", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                session = connection.execute("SELECT * FROM safety_sessions WHERE session_id=?",
                                             (session_id,)).fetchone()
                if session is None:
                    raise NotFoundError("体验过程不存在")
                if session["status"] != "executing":
                    raise ConflictError("体验过程不在执行状态，事实不能追加")
                fact_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO safety_facts(fact_id,session_id,kind,detail_json,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (fact_id, session_id, kind, canonical_json(detail), actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="safety.fact_appended",
                             resource_type="experience_session", resource_id=session_id,
                             detail={"fact_id": fact_id, "kind": kind}, occurred_at=self._now())
                return "session_fact", fact_id, {"fact_id": fact_id, "session_id": session_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="append_fact", payload=payload, create=create)

    def complete_session(self, *, request_id: str, actor_id: str, session_id: str,
                         summary: str) -> WriteReceipt:
        """登记正常回执并结束体验，安全封存生效后回执不能越过。"""

        payload = {"actor_id": actor_id, "session_id": session_id, "summary": summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            session_id = self._identifier(session_id, "session_id")
            summary = self._text(summary, "summary", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                session = connection.execute("SELECT * FROM safety_sessions WHERE session_id=?",
                                             (session_id,)).fetchone()
                if session is None:
                    raise NotFoundError("体验过程不存在")
                if session["status"] == "sealed":
                    raise ConflictError("安全封存已经生效，正常回执不能越过")
                if session["status"] != "executing":
                    raise ConflictError("体验过程已经结束")
                now = self._now()
                fact_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO safety_facts(fact_id,session_id,kind,detail_json,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (fact_id, session_id, "completion", canonical_json({"summary": summary}),
                     actor_id, now))
                connection.execute(
                    "UPDATE safety_sessions SET status='completed', ended_at=? WHERE session_id=?",
                    (now, session_id))
                append_event(connection, actor_id=actor_id, action="safety.session_completed",
                             resource_type="experience_session", resource_id=session_id,
                             detail={"fact_id": fact_id}, occurred_at=now)
                return "experience_session", session_id, {"session_id": session_id, "status": "completed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_session", payload=payload, create=create)

    # ---------- 异常事件与安全暂停 ----------

    def report_incident(self, *, actor_id: str, incident_id: str, session_id: str,
                        severity: str, description: str, occurred_at: str) -> dict[str, Any]:
        """原子地封存受影响过程、建立或复用安全暂停并生成待复核清单。

        事件号是幂等键：同一事件重放返回首次落库的结果，事件号相同而事实不同则拒绝。
        """

        incident_id = self._identifier(incident_id, "incident_id")
        session_id = str(session_id).strip()
        if not session_id:
            raise ValidationError("session_id 不能为空")
        if severity not in SEVERITIES:
            raise ValidationError("severity 不在允许范围内")
        description = self._text(description, "description", 500)
        occurred = self._parse_time(occurred_at, "occurred_at")
        payload = {"session_id": session_id, "severity": severity, "description": description,
                   "occurred_at": self._format_time(occurred)}
        payload_hash = digest(payload)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator", "reviewer")
            existing = connection.execute("SELECT * FROM safety_incidents WHERE incident_id=?",
                                          (incident_id,)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise ConflictError("事件号已被不同事实使用")
                result = json.loads(existing["result_json"])
                result["replayed"] = True
                return result
            session = connection.execute("SELECT * FROM safety_sessions WHERE session_id=?",
                                         (session_id,)).fetchone()
            if session is None:
                raise NotFoundError("体验过程不存在")
            now = self._now()
            protocol_scope = self._protocol_scope_key(session["protocol_id"],
                                                      session["protocol_version"])
            batch_scope = session["batch_no"]

            # 受影响范围：项目规程版本与器材批次，已有有效暂停则复用，不扩大停用范围。
            suspensions: list[dict[str, Any]] = []
            for scope_type, scope_key in ((PROTOCOL_SCOPE, protocol_scope), (BATCH_SCOPE, batch_scope)):
                row = connection.execute(
                    "SELECT * FROM safety_suspensions WHERE scope_type=? AND scope_key=? "
                    "AND status='active'", (scope_type, scope_key)).fetchone()
                if row:
                    suspensions.append({"suspension_id": row["suspension_id"], "scope_type": scope_type,
                                        "scope_key": scope_key, "status": "active", "reused": True})
                else:
                    suspensions.append({"suspension_id": uuid.uuid4().hex, "scope_type": scope_type,
                                        "scope_key": scope_key, "status": "active", "reused": False})

            # 封存范围内全部执行中的体验过程（含本次）。
            affected = connection.execute(
                "SELECT * FROM safety_sessions WHERE status='executing' AND "
                "(batch_no=? OR (protocol_id=? AND protocol_version=?))",
                (batch_scope, session["protocol_id"], session["protocol_version"])).fetchall()
            sealed = [row["session_id"] for row in affected]

            # 待复核清单：每个暂停的范围处置项加上每个被封存过程的复核项，已有待办不重复生成。
            reused_ids = [item["suspension_id"] for item in suspensions if item["reused"]]
            pending_keys: set[tuple[str, str, str]] = set()
            if reused_ids:
                marks = ",".join("?" for _ in reused_ids)
                for row in connection.execute(
                        f"SELECT suspension_id, subject_type, subject_id FROM safety_review_items "
                        f"WHERE status='pending' AND suspension_id IN ({marks})", reused_ids):
                    pending_keys.add((row["suspension_id"], row["subject_type"], row["subject_id"]))
            planned: list[dict[str, str]] = []
            planned_keys: set[tuple[str, str, str]] = set()

            def plan_item(suspension_id: str, subject_type: str, subject_id: str) -> None:
                key = (suspension_id, subject_type, subject_id)
                if key in pending_keys or key in planned_keys:
                    return
                planned_keys.add(key)
                planned.append({"review_id": uuid.uuid4().hex, "suspension_id": suspension_id,
                                "subject_type": subject_type, "subject_id": subject_id})

            for item in suspensions:
                plan_item(item["suspension_id"], item["scope_type"], item["scope_key"])
            for row in affected:
                for item in suspensions:
                    if item["scope_type"] == PROTOCOL_SCOPE and item["scope_key"] == \
                            self._protocol_scope_key(row["protocol_id"], row["protocol_version"]):
                        plan_item(item["suspension_id"], "session", row["session_id"])
                    if item["scope_type"] == BATCH_SCOPE and item["scope_key"] == row["batch_no"]:
                        plan_item(item["suspension_id"], "session", row["session_id"])

            result = {"incident_id": incident_id, "session_id": session_id,
                      "sealed_sessions": sealed, "suspensions": suspensions,
                      "review_items": planned, "replayed": False}

            connection.execute(
                "INSERT INTO safety_incidents(incident_id,payload_hash,session_id,severity,description,"
                "occurred_at,reported_by,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (incident_id, payload_hash, session_id, severity, description,
                 payload["occurred_at"], actor_id, canonical_json(result), now))
            for item in suspensions:
                if not item["reused"]:
                    connection.execute(
                        "INSERT INTO safety_suspensions(suspension_id,scope_type,scope_key,status,"
                        "created_by_incident,created_at) VALUES(?,?,?,'active',?,?)",
                        (item["suspension_id"], item["scope_type"], item["scope_key"],
                         incident_id, now))
                connection.execute(
                    "INSERT OR IGNORE INTO safety_incident_suspensions(incident_id,suspension_id) "
                    "VALUES(?,?)", (incident_id, item["suspension_id"]))
            for item in planned:
                connection.execute(
                    "INSERT INTO safety_review_items(review_id,incident_id,suspension_id,subject_type,"
                    "subject_id,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                    (item["review_id"], incident_id, item["suspension_id"],
                     item["subject_type"], item["subject_id"], now))
            for row in affected:
                connection.execute(
                    "UPDATE safety_sessions SET status='sealed', ended_at=?, sealed_by_incident=? "
                    "WHERE session_id=?", (now, incident_id, row["session_id"]))
                connection.execute(
                    "INSERT INTO safety_facts(fact_id,session_id,kind,detail_json,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, row["session_id"], "sealed",
                     canonical_json({"incident_id": incident_id, "severity": severity}),
                     actor_id, now))
            append_event(connection, actor_id=actor_id, action="safety.incident_reported",
                         resource_type="safety_incident", resource_id=incident_id,
                         detail={"session_id": session_id, "severity": severity,
                                 "sealed_sessions": sealed,
                                 "suspensions": [item["suspension_id"] for item in suspensions],
                                 "review_items": [item["review_id"] for item in planned]},
                         occurred_at=now)
            return result

    # ---------- 复核与放行 ----------

    def resolve_review_item(self, *, request_id: str, actor_id: str, review_id: str,
                            evidence_ref: str, note: str) -> WriteReceipt:
        """复核人员为待复核事项逐项关联处置证据。"""

        payload = {"actor_id": actor_id, "review_id": review_id,
                   "evidence_ref": evidence_ref, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            evidence_ref = self._text(evidence_ref, "evidence_ref")
            note = self._text(note, "note", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                item = connection.execute("SELECT * FROM safety_review_items WHERE review_id=?",
                                          (review_id,)).fetchone()
                if item is None:
                    raise NotFoundError("复核事项不存在")
                if item["status"] != "pending":
                    raise ConflictError("复核事项已经处理")
                connection.execute(
                    "UPDATE safety_review_items SET status='resolved', evidence_ref=?, "
                    "resolution_note=?, resolved_by=?, resolved_at=? WHERE review_id=?",
                    (evidence_ref, note, actor_id, self._now(), review_id))
                append_event(connection, actor_id=actor_id, action="safety.review_resolved",
                             resource_type="safety_review_item", resource_id=review_id,
                             detail={"suspension_id": item["suspension_id"],
                                     "subject_type": item["subject_type"],
                                     "subject_id": item["subject_id"],
                                     "evidence_ref": evidence_ref},
                             occurred_at=self._now())
                return "safety_review_item", review_id, {"review_id": review_id, "status": "resolved"}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_review_item", payload=payload, create=create)

    def lift_suspension(self, *, request_id: str, actor_id: str, suspension_id: str) -> WriteReceipt:
        """全部复核事项关联处置证据后，复核人员才能解除安全暂停。"""

        payload = {"actor_id": actor_id, "suspension_id": suspension_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")

            def create() -> tuple[str, str, dict[str, Any]]:
                suspension = connection.execute(
                    "SELECT * FROM safety_suspensions WHERE suspension_id=?",
                    (suspension_id,)).fetchone()
                if suspension is None:
                    raise NotFoundError("安全暂停不存在")
                if suspension["status"] != "active":
                    raise ConflictError("安全暂停已经解除")
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM safety_review_items "
                    "WHERE suspension_id=? AND status='pending'", (suspension_id,)).fetchone()["count"]
                if pending:
                    raise ConflictError("仍存在未关联处置证据的复核事项")
                connection.execute(
                    "UPDATE safety_suspensions SET status='lifted', lifted_at=?, lifted_by=? "
                    "WHERE suspension_id=?", (self._now(), actor_id, suspension_id))
                append_event(connection, actor_id=actor_id, action="safety.suspension_lifted",
                             resource_type="safety_suspension", resource_id=suspension_id,
                             detail={"scope_type": suspension["scope_type"],
                                     "scope_key": suspension["scope_key"]},
                             occurred_at=self._now())
                return "safety_suspension", suspension_id, {"suspension_id": suspension_id,
                                                            "status": "lifted"}

            return self._idempotent(connection, request_id=request_id,
                                    action="lift_suspension", payload=payload, create=create)

    # ---------- 查询与追溯 ----------

    def get_session(self, session_id: str) -> ExperienceSession:
        row = self.database.connection.execute("SELECT * FROM safety_sessions WHERE session_id=?",
                                               (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("体验过程不存在")
        return self._session_model(row)

    def list_facts(self, session_id: str) -> list[SessionFact]:
        rows = self.database.connection.execute(
            "SELECT * FROM safety_facts WHERE session_id=? ORDER BY recorded_at, fact_id",
            (session_id,)).fetchall()
        return [self._fact_model(row) for row in rows]

    def list_suspensions(self, status: str | None = None) -> list[Suspension]:
        if status is not None and status not in SUSPENSION_STATUSES:
            raise ValidationError("status 不在允许范围内")
        query = "SELECT * FROM safety_suspensions"
        parameters: list[Any] = []
        if status:
            query += " WHERE status=?"
            parameters.append(status)
        query += " ORDER BY created_at, suspension_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [self._suspension_model(row) for row in rows]

    def list_review_items(self, status: str | None = None,
                          suspension_id: str | None = None) -> list[ReviewItem]:
        if status is not None and status not in REVIEW_STATUSES:
            raise ValidationError("status 不在允许范围内")
        query = "SELECT * FROM safety_review_items"
        conditions: list[str] = []
        parameters: list[Any] = []
        if status:
            conditions.append("status=?")
            parameters.append(status)
        if suspension_id:
            conditions.append("suspension_id=?")
            parameters.append(suspension_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at, review_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [self._review_model(row) for row in rows]

    def trace_session(self, session_id: str) -> dict[str, Any]:
        """从一次体验追溯到规程、人员、器材、过程事实与风险处置。"""

        connection = self.database.connection
        session = connection.execute("SELECT * FROM safety_sessions WHERE session_id=?",
                                     (session_id,)).fetchone()
        if session is None:
            raise NotFoundError("体验过程不存在")
        protocol = connection.execute(
            "SELECT * FROM safety_protocols WHERE protocol_id=? AND version=?",
            (session["protocol_id"], session["protocol_version"])).fetchone()
        practitioner = connection.execute("SELECT * FROM safety_practitioners WHERE practitioner_id=?",
                                          (session["practitioner_id"],)).fetchone()
        participant = connection.execute("SELECT * FROM safety_participants WHERE participant_id=?",
                                         (session["participant_id"],)).fetchone()
        screening = connection.execute("SELECT * FROM safety_screenings WHERE screening_id=?",
                                       (session["screening_id"],)).fetchone()
        equipment = connection.execute("SELECT * FROM safety_equipment WHERE equipment_id=?",
                                       (session["equipment_id"],)).fetchone()
        checkout = connection.execute("SELECT * FROM safety_checkouts WHERE checkout_id=?",
                                      (session["checkout_id"],)).fetchone()
        incidents = connection.execute(
            "SELECT * FROM safety_incidents WHERE session_id=? ORDER BY created_at, incident_id",
            (session_id,)).fetchall()
        suspensions: list[Suspension] = []
        seen: set[str] = set()
        for scope_type, scope_key in (
                (PROTOCOL_SCOPE, self._protocol_scope_key(session["protocol_id"],
                                                          session["protocol_version"])),
                (BATCH_SCOPE, session["batch_no"])):
            for row in connection.execute(
                    "SELECT * FROM safety_suspensions WHERE scope_type=? AND scope_key=? "
                    "ORDER BY created_at, suspension_id", (scope_type, scope_key)):
                if row["suspension_id"] not in seen:
                    seen.add(row["suspension_id"])
                    suspensions.append(self._suspension_model(row))
        review_items: list[ReviewItem] = []
        if seen:
            marks = ",".join("?" for _ in seen)
            for row in connection.execute(
                    f"SELECT * FROM safety_review_items WHERE suspension_id IN ({marks}) "
                    "ORDER BY created_at, review_id", sorted(seen)):
                review_items.append(self._review_model(row))
        return {
            "session": self._session_model(session).__dict__,
            "protocol": {"protocol_id": protocol["protocol_id"], "version": protocol["version"],
                         "title": protocol["title"], "content": json.loads(protocol["content_json"])},
            "practitioner": {"practitioner_id": practitioner["practitioner_id"],
                             "display_name": practitioner["display_name"],
                             "qualification_no": practitioner["qualification_no"],
                             "valid_from": practitioner["valid_from"],
                             "valid_until": practitioner["valid_until"]},
            "participant": {"participant_id": participant["participant_id"],
                            "display_name": participant["display_name"]},
            "screening": {"screening_id": screening["screening_id"],
                          "conclusion": screening["conclusion"],
                          "valid_until": screening["valid_until"]},
            "equipment": {"equipment_id": equipment["equipment_id"], "name": equipment["name"],
                          "batch_no": equipment["batch_no"]},
            "checkout": {"checkout_id": checkout["checkout_id"], "status": checkout["status"],
                         "issued_at": checkout["issued_at"], "returned_at": checkout["returned_at"]},
            "facts": [fact.__dict__ for fact in self.list_facts(session_id)],
            "incidents": [{"incident_id": row["incident_id"], "severity": row["severity"],
                           "description": row["description"], "occurred_at": row["occurred_at"],
                           "reported_by": row["reported_by"], "created_at": row["created_at"]}
                          for row in incidents],
            "suspensions": [item.__dict__ for item in suspensions],
            "review_items": [item.__dict__ for item in review_items],
        }
