"""在基础数据库之上扩展体验安全记录所需的表结构。"""

from __future__ import annotations

from night_market_foundation.storage import Database


SAFETY_SCHEMA = """
CREATE TABLE IF NOT EXISTS safety_protocols (
    protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (protocol_id, version)
);
CREATE TABLE IF NOT EXISTS safety_practitioners (
    practitioner_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    qualification_no TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_participants (
    participant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_screenings (
    screening_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES safety_participants(participant_id),
    conclusion TEXT NOT NULL CHECK(conclusion IN ('cleared', 'rejected')),
    valid_until TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_equipment (
    equipment_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_checkouts (
    checkout_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES safety_equipment(equipment_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    status TEXT NOT NULL CHECK(status IN ('issued', 'returned')),
    issued_at TEXT NOT NULL,
    returned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_safety_checkouts_active
    ON safety_checkouts(equipment_id, status);
CREATE TABLE IF NOT EXISTS safety_sessions (
    session_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    protocol_id TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    practitioner_id TEXT NOT NULL REFERENCES safety_practitioners(practitioner_id),
    participant_id TEXT NOT NULL REFERENCES safety_participants(participant_id),
    equipment_id TEXT NOT NULL REFERENCES safety_equipment(equipment_id),
    checkout_id TEXT NOT NULL REFERENCES safety_checkouts(checkout_id),
    batch_no TEXT NOT NULL,
    screening_id TEXT NOT NULL REFERENCES safety_screenings(screening_id),
    status TEXT NOT NULL CHECK(status IN ('executing', 'completed', 'sealed')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    sealed_by_incident TEXT,
    FOREIGN KEY (protocol_id, protocol_version)
        REFERENCES safety_protocols(protocol_id, version)
);
CREATE INDEX IF NOT EXISTS idx_safety_sessions_scope
    ON safety_sessions(status, batch_no);
CREATE TABLE IF NOT EXISTS safety_facts (
    fact_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES safety_sessions(session_id),
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_safety_facts_session
    ON safety_facts(session_id);
CREATE TRIGGER IF NOT EXISTS safety_facts_append_only_update
BEFORE UPDATE ON safety_facts
BEGIN SELECT RAISE(ABORT, 'safety_facts 只允许追加'); END;
CREATE TRIGGER IF NOT EXISTS safety_facts_append_only_delete
BEFORE DELETE ON safety_facts
BEGIN SELECT RAISE(ABORT, 'safety_facts 只允许追加'); END;
CREATE TRIGGER IF NOT EXISTS safety_protocols_immutable_update
BEFORE UPDATE ON safety_protocols
BEGIN SELECT RAISE(ABORT, 'safety_protocols 版本不可变'); END;
CREATE TRIGGER IF NOT EXISTS safety_protocols_immutable_delete
BEFORE DELETE ON safety_protocols
BEGIN SELECT RAISE(ABORT, 'safety_protocols 版本不可变'); END;
CREATE TABLE IF NOT EXISTS safety_incidents (
    incident_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES safety_sessions(session_id),
    severity TEXT NOT NULL CHECK(severity IN ('mild', 'moderate', 'severe')),
    description TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_suspensions (
    suspension_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('protocol', 'equipment_batch')),
    scope_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'lifted')),
    created_by_incident TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_at TEXT,
    lifted_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_safety_suspensions_active_scope
    ON safety_suspensions(scope_type, scope_key) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS safety_incident_suspensions (
    incident_id TEXT NOT NULL REFERENCES safety_incidents(incident_id),
    suspension_id TEXT NOT NULL REFERENCES safety_suspensions(suspension_id),
    PRIMARY KEY (incident_id, suspension_id)
);
CREATE TABLE IF NOT EXISTS safety_review_items (
    review_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES safety_incidents(incident_id),
    suspension_id TEXT NOT NULL REFERENCES safety_suspensions(suspension_id),
    subject_type TEXT NOT NULL CHECK(subject_type IN ('session', 'protocol', 'equipment_batch')),
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'resolved')),
    evidence_ref TEXT,
    resolution_note TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_safety_review_items_suspension
    ON safety_review_items(suspension_id, status);
"""


def ensure_schema(database: Database) -> None:
    """在既有数据库连接上幂等地建立安全记录表结构。"""

    database.connection.executescript(SAFETY_SCHEMA)
