"""定义体验安全记录模块的读取模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExperienceSession:
    """一次适宜技术体验过程的状态快照。"""

    session_id: str
    site_id: str
    protocol_id: str
    protocol_version: int
    practitioner_id: str
    participant_id: str
    equipment_id: str
    checkout_id: str
    batch_no: str
    screening_id: str
    status: str
    started_at: str
    ended_at: str | None
    sealed_by_incident: str | None


@dataclass(frozen=True)
class SessionFact:
    """体验过程中追加的事实记录，只允许追加。"""

    fact_id: str
    session_id: str
    kind: str
    detail: dict[str, Any]
    recorded_by: str
    recorded_at: str


@dataclass(frozen=True)
class Suspension:
    """针对项目规程版本或器材批次的安全暂停。"""

    suspension_id: str
    scope_type: str
    scope_key: str
    status: str
    created_by_incident: str
    created_at: str
    lifted_at: str | None
    lifted_by: str | None


@dataclass(frozen=True)
class ReviewItem:
    """异常事件生成的待复核事项，解除暂停前必须逐项关联处置证据。"""

    review_id: str
    incident_id: str
    suspension_id: str
    subject_type: str
    subject_id: str
    status: str
    evidence_ref: str | None
    resolution_note: str | None
    resolved_by: str | None
    resolved_at: str | None
