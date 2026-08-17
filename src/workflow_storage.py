"""SQLite schema and read helpers for requisitions, workflow, duplicates, and POs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKFLOW_SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS requisitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingested_email_id INTEGER NOT NULL UNIQUE,
    approval_route_run_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'inbox', 'extracted', 'needs_review', 'pending_approval',
        'approved', 'rejected', 'ordering', 'ordered', 'duplicate'
    )),
    version INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    route_basis_json TEXT NOT NULL,
    requires_route_rebuild INTEGER NOT NULL,
    duplicate_of_requisition_id INTEGER,
    po_number TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (ingested_email_id) REFERENCES ingested_emails(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (approval_route_run_id) REFERENCES approval_route_runs(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (duplicate_of_requisition_id) REFERENCES requisitions(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_requisitions_state ON requisitions(state);

CREATE TABLE IF NOT EXISTS requisition_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requisition_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    changed_fields_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (requisition_id) REFERENCES requisitions(id)
        ON DELETE RESTRICT,
    UNIQUE (requisition_id, version)
);

CREATE TABLE IF NOT EXISTS requisition_route_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requisition_id INTEGER NOT NULL,
    requisition_version INTEGER NOT NULL,
    route_json TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (requisition_id) REFERENCES requisitions(id)
        ON DELETE RESTRICT,
    UNIQUE (requisition_id, requisition_version)
);

CREATE TABLE IF NOT EXISTS workflow_approval_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requisition_id INTEGER NOT NULL,
    route_step_sequence INTEGER NOT NULL,
    employee_id TEXT,
    employee_name TEXT,
    employee_email TEXT,
    role_labels_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'pending', 'active', 'approved', 'rejected', 'cancelled'
    )),
    acted_by TEXT,
    action_reason TEXT,
    acted_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (requisition_id) REFERENCES requisitions(id)
        ON DELETE RESTRICT,
    UNIQUE (requisition_id, route_step_sequence)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_requisition_status
    ON workflow_approval_steps(requisition_id, status, route_step_sequence);

CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requisition_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT NOT NULL,
    reason TEXT,
    details_json TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (requisition_id) REFERENCES requisitions(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_requisition
    ON workflow_events(requisition_id, id);

CREATE TABLE IF NOT EXISTS duplicate_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_ingested_email_id INTEGER NOT NULL,
    right_ingested_email_id INTEGER NOT NULL,
    relation_kind TEXT NOT NULL CHECK(relation_kind IN (
        'exact_duplicate', 'suspected_duplicate', 'correction_candidate'
    )),
    confidence TEXT NOT NULL CHECK(confidence IN ('exact', 'high', 'medium', 'low')),
    evidence_json TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK(resolution IN ('pending', 'confirmed', 'dismissed')),
    resolved_by TEXT,
    resolution_reason TEXT,
    created_at_utc TEXT NOT NULL,
    resolved_at_utc TEXT,
    FOREIGN KEY (left_ingested_email_id) REFERENCES ingested_emails(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (right_ingested_email_id) REFERENCES ingested_emails(id)
        ON DELETE RESTRICT,
    UNIQUE (left_ingested_email_id, right_ingested_email_id, relation_kind)
);

CREATE INDEX IF NOT EXISTS idx_duplicate_links_resolution
    ON duplicate_links(resolution, left_ingested_email_id, right_ingested_email_id);

CREATE TABLE IF NOT EXISTS po_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requisition_id INTEGER NOT NULL,
    requisition_version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    endpoint TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'succeeded', 'rejected', 'failed'
    )),
    attempt_count INTEGER NOT NULL,
    http_status INTEGER,
    response_json TEXT,
    error_message TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (requisition_id) REFERENCES requisitions(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_po_submissions_requisition
    ON po_submissions(requisition_id, id);
"""


class WorkflowStorageError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_json(value: str | None, *, context: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise WorkflowStorageError(f"Invalid stored JSON for {context}") from exc


def open_workflow_database(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise WorkflowStorageError(f"Database does not exist: {resolved}")
    try:
        connection = sqlite3.connect(resolved)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO schema_metadata(key, value)
            VALUES ('workflow_schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(WORKFLOW_SCHEMA_VERSION),),
        )
        connection.commit()
        return connection
    except sqlite3.Error as exc:
        raise WorkflowStorageError(
            f"Could not initialize workflow tables in {resolved}: {exc}"
        ) from exc


def load_route_bundle(
    db_path: Path, *, ingested_email_id: int | None = None, route_run_id: int | None = None
) -> dict[str, Any]:
    if (ingested_email_id is None) == (route_run_id is None):
        raise WorkflowStorageError("Choose exactly one of ingested_email_id or route_run_id")
    connection = open_workflow_database(db_path)
    try:
        base = """
            SELECT arr.id AS approval_route_run_id, arr.status AS route_status,
                   arr.route_json, ars.sequence, ars.employee_id,
                   ars.employee_name, ars.employee_email, ars.role_labels_json,
                   ars.requires_manual_assignment,
                   mr.id AS matching_run_id, mr.status AS matching_status,
                   mr.matching_json, nr.id AS normalization_run_id,
                   nr.status AS normalization_status, nr.normalized_json,
                   er.ingested_email_id, er.extraction_json,
                   ie.source_filename, ie.subject, ie.sender_email, ie.message_id
            FROM approval_route_runs arr
            JOIN matching_runs mr ON mr.id = arr.matching_run_id
            JOIN normalization_runs nr ON nr.id = arr.normalization_run_id
            JOIN extraction_runs er ON er.id = nr.extraction_run_id
            JOIN ingested_emails ie ON ie.id = er.ingested_email_id
            LEFT JOIN approval_route_steps ars ON ars.approval_route_run_id = arr.id
        """
        if route_run_id is not None:
            rows = connection.execute(
                base + " WHERE arr.id = ? ORDER BY ars.sequence", (route_run_id,)
            ).fetchall()
            selector = f"approval route run ID {route_run_id}"
        else:
            latest = connection.execute(
                """
                SELECT arr.id
                FROM approval_route_runs arr
                JOIN normalization_runs nr ON nr.id = arr.normalization_run_id
                JOIN extraction_runs er ON er.id = nr.extraction_run_id
                WHERE er.ingested_email_id = ?
                ORDER BY arr.id DESC LIMIT 1
                """,
                (ingested_email_id,),
            ).fetchone()
            if latest is None:
                rows = []
            else:
                rows = connection.execute(
                    base + " WHERE arr.id = ? ORDER BY ars.sequence", (latest["id"],)
                ).fetchall()
            selector = f"ingested email ID {ingested_email_id}"
    except sqlite3.Error as exc:
        raise WorkflowStorageError(f"Could not load route bundle: {exc}") from exc
    finally:
        connection.close()
    if not rows:
        raise WorkflowStorageError(f"No approval route found for {selector}")
    first = rows[0]
    try:
        steps = [
            {
                "sequence": int(row["sequence"]),
                "employee": {
                    "id": row["employee_id"],
                    "name": row["employee_name"],
                    "email": row["employee_email"],
                }
                if row["employee_id"] is not None
                else None,
                "role_labels": json.loads(row["role_labels_json"]),
                "requires_manual_assignment": bool(row["requires_manual_assignment"]),
            }
            for row in rows
            if row["sequence"] is not None
        ]
        return {
            "approval_route_run_id": int(first["approval_route_run_id"]),
            "route_status": first["route_status"],
            "route": json.loads(first["route_json"]),
            "route_steps": steps,
            "matching_run_id": int(first["matching_run_id"]),
            "matching_status": first["matching_status"],
            "matching": json.loads(first["matching_json"]),
            "normalization_run_id": int(first["normalization_run_id"]),
            "normalization_status": first["normalization_status"],
            "normalized": json.loads(first["normalized_json"]),
            "extraction": json.loads(first["extraction_json"]),
            "ingested_email_id": int(first["ingested_email_id"]),
            "source_filename": first["source_filename"],
            "subject": first["subject"],
            "sender_email": first["sender_email"],
            "message_id": first["message_id"],
        }
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkflowStorageError(f"Stored route bundle for {selector} is invalid") from exc


def list_route_email_ids(db_path: Path) -> list[int]:
    connection = open_workflow_database(db_path)
    try:
        return [
            int(row["ingested_email_id"])
            for row in connection.execute(
                """
                SELECT DISTINCT er.ingested_email_id
                FROM approval_route_runs arr
                JOIN normalization_runs nr ON nr.id = arr.normalization_run_id
                JOIN extraction_runs er ON er.id = nr.extraction_run_id
                ORDER BY er.ingested_email_id
                """
            )
        ]
    except sqlite3.Error as exc:
        raise WorkflowStorageError(f"Could not list routed emails: {exc}") from exc
    finally:
        connection.close()


def decode_requisition(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["data"] = parse_json(item.pop("data_json"), context=f"requisition {item['id']} data")
    item["route_basis"] = parse_json(
        item.pop("route_basis_json"), context=f"requisition {item['id']} route basis"
    )
    item["requires_route_rebuild"] = bool(item["requires_route_rebuild"])
    return item


def list_requisitions(db_path: Path) -> list[dict[str, Any]]:
    connection = open_workflow_database(db_path)
    try:
        rows = connection.execute(
            """
            SELECT r.*, ie.source_filename, ie.subject
            FROM requisitions r
            JOIN ingested_emails ie ON ie.id = r.ingested_email_id
            ORDER BY r.id
            """
        ).fetchall()
        return [decode_requisition(row) for row in rows]
    except sqlite3.Error as exc:
        raise WorkflowStorageError(f"Could not list requisitions: {exc}") from exc
    finally:
        connection.close()


def get_requisition(db_path: Path, requisition_id: int) -> dict[str, Any]:
    connection = open_workflow_database(db_path)
    try:
        row = connection.execute(
            """
            SELECT r.*, ie.source_filename, ie.subject
            FROM requisitions r
            JOIN ingested_emails ie ON ie.id = r.ingested_email_id
            WHERE r.id = ?
            """,
            (requisition_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStorageError(f"Requisition ID {requisition_id} does not exist")
        requisition = decode_requisition(row)
        requisition["steps"] = []
        for step in connection.execute(
            "SELECT * FROM workflow_approval_steps WHERE requisition_id = ? ORDER BY route_step_sequence",
            (requisition_id,),
        ):
            value = dict(step)
            value["role_labels"] = parse_json(
                value.pop("role_labels_json"), context=f"workflow step {value['id']} roles"
            )
            requisition["steps"].append(value)
        requisition["events"] = []
        for event in connection.execute(
            "SELECT * FROM workflow_events WHERE requisition_id = ? ORDER BY id",
            (requisition_id,),
        ):
            value = dict(event)
            value["details"] = parse_json(
                value.pop("details_json"), context=f"workflow event {value['id']} details"
            )
            requisition["events"].append(value)
        requisition["route_revisions"] = []
        for revision in connection.execute(
            "SELECT * FROM requisition_route_revisions WHERE requisition_id = ? ORDER BY id",
            (requisition_id,),
        ):
            value = dict(revision)
            value["route"] = parse_json(
                value.pop("route_json"), context=f"route revision {value['id']} route"
            )
            value["steps"] = parse_json(
                value.pop("steps_json"), context=f"route revision {value['id']} steps"
            )
            requisition["route_revisions"].append(value)
        requisition["duplicate_links"] = [
            {
                **dict(link),
                "evidence": parse_json(
                    link["evidence_json"], context=f"duplicate link {link['id']} evidence"
                ),
            }
            for link in connection.execute(
                """
                SELECT * FROM duplicate_links
                WHERE left_ingested_email_id = ? OR right_ingested_email_id = ?
                ORDER BY id
                """,
                (requisition["ingested_email_id"], requisition["ingested_email_id"]),
            )
        ]
        requisition["po_submissions"] = []
        for submission in connection.execute(
            "SELECT * FROM po_submissions WHERE requisition_id = ? ORDER BY id",
            (requisition_id,),
        ):
            value = dict(submission)
            value["request"] = parse_json(
                value.pop("request_json"), context=f"PO submission {value['id']} request"
            )
            value["response"] = parse_json(
                value.pop("response_json"), context=f"PO submission {value['id']} response"
            )
            requisition["po_submissions"].append(value)
        return requisition
    except sqlite3.Error as exc:
        raise WorkflowStorageError(f"Could not load requisition: {exc}") from exc
    finally:
        connection.close()
