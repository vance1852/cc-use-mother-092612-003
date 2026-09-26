"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示中医药文化活动组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class ProcedureVersion:
    """不可变的适宜技术项目规程版本。"""

    procedure_id: str
    version: str
    title: str
    content: dict[str, Any]
    content_hash: str
    published_by: str
    published_at: str


@dataclass(frozen=True)
class Qualification:
    """执行人员对某项目的资格及其有效期限。"""

    qualification_id: str
    actor_id: str
    procedure_id: str
    valid_from: str
    valid_until: str
    revoked: bool
    granted_by: str
    granted_at: str


@dataclass(frozen=True)
class Screening:
    """参与者对某项目的筛查结论及其有效期。"""

    screening_id: str
    site_id: str
    procedure_id: str
    participant_id: str
    conclusion: str
    detail: dict[str, Any]
    decided_by: str
    decided_at: str
    valid_until: str


@dataclass(frozen=True)
class EquipmentBatch:
    """可被整体封停的器材批次。"""

    batch_id: str
    site_id: str
    name: str
    status: str
    created_at: str


@dataclass(frozen=True)
class EquipmentLoan:
    """器材批次对执行人员的领用关系。"""

    loan_id: str
    batch_id: str
    site_id: str
    borrower_actor_id: str
    loaned_at: str
    returned_at: str | None


@dataclass(frozen=True)
class SafetySession:
    """一次适宜技术体验过程及其安全状态。"""

    session_id: str
    site_id: str
    procedure_id: str
    procedure_version: str
    procedure_hash: str
    operator_actor_id: str
    qualification_id: str
    participant_id: str
    screening_id: str
    loan_id: str
    batch_id: str
    state: str
    precheck: dict[str, Any]
    sealing_incident_no: str | None
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class SessionFact:
    """过程开始后只能追加的事实。"""

    fact_id: str
    session_id: str
    fact_seq: int
    kind: str
    payload: dict[str, Any]
    payload_hash: str
    effective: bool
    ineffective_reason: str | None
    recorded_by: str
    occurred_at: str
    previous_hash: str
    fact_hash: str


@dataclass(frozen=True)
class ReviewItem:
    """异常事件生成的待复核清单项。"""

    item_id: str
    incident_no: str
    target_type: str
    target_id: str
    status: str
    evidence_ref: str | None
    resolution_note: str | None
    resolved_by: str | None
    resolved_at: str | None


@dataclass(frozen=True)
class Suspension:
    """器材批次的暂停边界。"""

    suspension_id: str
    incident_no: str
    batch_id: str
    status: str
    suspended_at: str
    lifted_at: str | None
    lifted_by: str | None
