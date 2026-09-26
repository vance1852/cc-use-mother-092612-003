"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);

-- 适宜技术体验安全记录
CREATE TABLE IF NOT EXISTS procedure_versions (
    procedure_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (procedure_id, version)
);
CREATE TABLE IF NOT EXISTS qualifications (
    qualification_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    procedure_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    UNIQUE(actor_id, procedure_id, valid_from, valid_until)
);
CREATE TABLE IF NOT EXISTS screenings (
    screening_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    procedure_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    conclusion TEXT NOT NULL CHECK(conclusion IN ('fit', 'unfit', 'conditional')),
    detail_json TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    valid_until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equipment_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'suspended')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equipment_loans (
    loan_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES equipment_batches(batch_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    borrower_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    loaned_at TEXT NOT NULL,
    returned_at TEXT
);
CREATE TABLE IF NOT EXISTS safety_sessions (
    session_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    procedure_id TEXT NOT NULL,
    procedure_version TEXT NOT NULL,
    procedure_hash TEXT NOT NULL,
    operator_actor_id TEXT NOT NULL,
    qualification_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    screening_id TEXT NOT NULL,
    loan_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'sealed', 'suspended')),
    precheck_json TEXT NOT NULL,
    sealing_incident_no TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS session_facts (
    fact_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES safety_sessions(session_id),
    fact_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    effective INTEGER NOT NULL CHECK(effective IN (0, 1)),
    ineffective_reason TEXT,
    recorded_by TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    fact_hash TEXT NOT NULL,
    UNIQUE(session_id, fact_seq)
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_no TEXT PRIMARY KEY,
    facts_hash TEXT NOT NULL,
    origin_session_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_affected (
    id TEXT PRIMARY KEY,
    incident_no TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    UNIQUE(incident_no, target_type, target_id)
);
CREATE TABLE IF NOT EXISTS suspensions (
    suspension_id TEXT PRIMARY KEY,
    incident_no TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'lifted')),
    suspended_at TEXT NOT NULL,
    lifted_at TEXT,
    lifted_by TEXT,
    UNIQUE(incident_no, batch_id)
);
CREATE TABLE IF NOT EXISTS review_items (
    item_id TEXT PRIMARY KEY,
    incident_no TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'resolved')),
    evidence_ref TEXT,
    resolution_note TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    UNIQUE(incident_no, target_type, target_id)
);

-- 不可变边界：规程版本、过程事实、异常事件与影响范围只能追加
CREATE TRIGGER IF NOT EXISTS procedure_versions_no_update
BEFORE UPDATE ON procedure_versions
BEGIN SELECT RAISE(ABORT, '规程版本一经发布不可修改'); END;
CREATE TRIGGER IF NOT EXISTS procedure_versions_no_delete
BEFORE DELETE ON procedure_versions
BEGIN SELECT RAISE(ABORT, '规程版本一经发布不可删除'); END;
CREATE TRIGGER IF NOT EXISTS session_facts_no_update
BEFORE UPDATE ON session_facts
BEGIN SELECT RAISE(ABORT, '过程事实只允许追加，禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS session_facts_no_delete
BEFORE DELETE ON session_facts
BEGIN SELECT RAISE(ABORT, '过程事实只允许追加，禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS incidents_no_update
BEFORE UPDATE ON incidents
BEGIN SELECT RAISE(ABORT, '异常事件落库后不可修改'); END;
CREATE TRIGGER IF NOT EXISTS incidents_no_delete
BEFORE DELETE ON incidents
BEGIN SELECT RAISE(ABORT, '异常事件落库后不可删除'); END;
CREATE TRIGGER IF NOT EXISTS incident_affected_no_update
BEFORE UPDATE ON incident_affected
BEGIN SELECT RAISE(ABORT, '影响范围已经确定，不可修改'); END;
CREATE TRIGGER IF NOT EXISTS incident_affected_no_delete
BEFORE DELETE ON incident_affected
BEGIN SELECT RAISE(ABORT, '影响范围已经确定，不可删除'); END;
CREATE TRIGGER IF NOT EXISTS review_items_evidence_once
BEFORE UPDATE ON review_items
FOR EACH ROW WHEN OLD.status = 'resolved'
BEGIN SELECT RAISE(ABORT, '复核项已关联处置证据，不得重复修改'); END;
CREATE TRIGGER IF NOT EXISTS review_items_require_evidence
BEFORE UPDATE ON review_items
FOR EACH ROW WHEN NEW.status = 'resolved'
 AND (NEW.evidence_ref IS NULL OR length(NEW.evidence_ref) = 0)
BEGIN SELECT RAISE(ABORT, '复核结论必须关联处置证据'); END;
CREATE TRIGGER IF NOT EXISTS review_items_no_delete
BEFORE DELETE ON review_items
BEGIN SELECT RAISE(ABORT, '待复核清单只能逐项处置，不能删除'); END;
CREATE TRIGGER IF NOT EXISTS suspensions_no_reopen
BEFORE UPDATE ON suspensions
FOR EACH ROW WHEN OLD.status = 'lifted' AND NEW.status = 'active'
BEGIN SELECT RAISE(ABORT, '暂停解除后不能重新生效'); END;
CREATE TRIGGER IF NOT EXISTS suspensions_identity_no_change
BEFORE UPDATE ON suspensions
FOR EACH ROW WHEN NEW.incident_no != OLD.incident_no OR NEW.batch_id != OLD.batch_id
BEGIN SELECT RAISE(ABORT, '暂停边界关联的事件或批次不可变更'); END;
CREATE TRIGGER IF NOT EXISTS suspensions_no_delete
BEFORE DELETE ON suspensions
BEGIN SELECT RAISE(ABORT, '暂停边界只能解除，不能删除'); END;
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
