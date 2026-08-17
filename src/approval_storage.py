"""Append-only persistence for calculated approval-route snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APPROVAL_STORAGE_SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approval_route_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matching_run_id INTEGER NOT NULL,
    normalization_run_id INTEGER NOT NULL,
    master_snapshot_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid', 'needs_review', 'failed')),
    route_version TEXT NOT NULL,
    route_json TEXT,
    issues_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (matching_run_id) REFERENCES matching_runs(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (normalization_run_id) REFERENCES normalization_runs(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (master_snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_approval_route_runs_matching_created
    ON approval_route_runs(matching_run_id, id DESC);

CREATE TABLE IF NOT EXISTS approval_route_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_route_run_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    employee_id TEXT,
    employee_name TEXT,
    employee_email TEXT,
    role_labels_json TEXT NOT NULL,
    assignment_source TEXT NOT NULL,
    requires_manual_assignment INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (approval_route_run_id) REFERENCES approval_route_runs(id)
        ON DELETE RESTRICT,
    UNIQUE (approval_route_run_id, sequence)
);
"""


class ApprovalStorageError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def open_approval_database(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ApprovalStorageError(f"Database does not exist: {resolved}")
    try:
        connection = sqlite3.connect(resolved)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO schema_metadata(key, value)
            VALUES ('approval_storage_schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(APPROVAL_STORAGE_SCHEMA_VERSION),),
        )
        connection.commit()
        return connection
    except sqlite3.Error as exc:
        raise ApprovalStorageError(
            f"Could not initialize approval-route tables in {resolved}: {exc}"
        ) from exc


def load_approval_input(
    db_path: Path,
    *,
    matching_run_id: int | None = None,
    ingested_email_id: int | None = None,
) -> dict[str, Any]:
    """Load one immutable matching result and its exact master-data snapshot."""
    if (matching_run_id is None) == (ingested_email_id is None):
        raise ApprovalStorageError(
            "Choose exactly one of matching_run_id or ingested_email_id"
        )
    connection = open_approval_database(db_path)
    try:
        base_sql = """
            SELECT mr.id AS matching_run_id, mr.status AS matching_status,
                   mr.matching_json, mr.issues_json AS matching_issues_json,
                   mr.master_snapshot_id, nr.id AS normalization_run_id,
                   nr.status AS normalization_status, nr.normalized_json,
                   er.ingested_email_id, ie.source_filename,
                   mds.payload_json AS master_payload_json,
                   mds.source_sha256 AS master_source_sha256
            FROM matching_runs mr
            JOIN normalization_runs nr ON nr.id = mr.normalization_run_id
            JOIN extraction_runs er ON er.id = nr.extraction_run_id
            JOIN ingested_emails ie ON ie.id = er.ingested_email_id
            JOIN master_data_snapshots mds ON mds.id = mr.master_snapshot_id
        """
        if matching_run_id is not None:
            row = connection.execute(
                base_sql + " WHERE mr.id = ?", (matching_run_id,)
            ).fetchone()
            selector = f"matching run ID {matching_run_id}"
        else:
            row = connection.execute(
                base_sql
                + " WHERE er.ingested_email_id = ? ORDER BY mr.id DESC LIMIT 1",
                (ingested_email_id,),
            ).fetchone()
            selector = f"ingested email ID {ingested_email_id}"
    except sqlite3.Error as exc:
        raise ApprovalStorageError(f"Could not load approval input: {exc}") from exc
    finally:
        connection.close()
    if row is None:
        raise ApprovalStorageError(f"No matching run found for {selector}")
    required_json = ("matching_json", "normalized_json", "master_payload_json")
    if any(row[key] is None for key in required_json):
        raise ApprovalStorageError(f"{selector} has no usable matching/normalization/master JSON")
    try:
        matching = json.loads(row["matching_json"])
        normalized = json.loads(row["normalized_json"])
        master_payload = json.loads(row["master_payload_json"])
        matching_issues = (
            json.loads(row["matching_issues_json"])
            if row["matching_issues_json"] is not None
            else []
        )
    except json.JSONDecodeError as exc:
        raise ApprovalStorageError(f"Stored JSON for {selector} is invalid") from exc
    return {
        "matching_run_id": int(row["matching_run_id"]),
        "matching_status": row["matching_status"],
        "matching": matching,
        "matching_issues": matching_issues,
        "master_snapshot_id": int(row["master_snapshot_id"]),
        "master_source_sha256": row["master_source_sha256"],
        "master_payload": master_payload,
        "normalization_run_id": int(row["normalization_run_id"]),
        "normalization_status": row["normalization_status"],
        "normalized": normalized,
        "ingested_email_id": int(row["ingested_email_id"]),
        "source_filename": row["source_filename"],
    }


def list_matched_email_ids(db_path: Path) -> list[int]:
    connection = open_approval_database(db_path)
    try:
        return [
            int(row["ingested_email_id"])
            for row in connection.execute(
                """
                SELECT DISTINCT er.ingested_email_id
                FROM matching_runs mr
                JOIN normalization_runs nr ON nr.id = mr.normalization_run_id
                JOIN extraction_runs er ON er.id = nr.extraction_run_id
                ORDER BY er.ingested_email_id
                """
            )
        ]
    except sqlite3.Error as exc:
        raise ApprovalStorageError(f"Could not list matched emails: {exc}") from exc
    finally:
        connection.close()


def save_approval_route_run(
    db_path: Path,
    *,
    matching_run_id: int,
    normalization_run_id: int,
    master_snapshot_id: int,
    status: str,
    route_version: str,
    route: Any = None,
    issues: Any = None,
    steps: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    if status not in {"valid", "needs_review", "failed"}:
        raise ApprovalStorageError(f"Unsupported approval-route status: {status}")
    connection = open_approval_database(db_path)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO approval_route_runs(
                    matching_run_id, normalization_run_id, master_snapshot_id,
                    status, route_version, route_json, issues_json,
                    error_code, error_message, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    matching_run_id,
                    normalization_run_id,
                    master_snapshot_id,
                    status,
                    route_version,
                    compact_json(route) if route is not None else None,
                    compact_json(issues) if issues is not None else None,
                    error_code,
                    error_message,
                    utc_now(),
                ),
            )
            route_run_id = int(cursor.lastrowid)
            for sequence, step in enumerate(steps or [], start=1):
                employee = step.get("employee") or {}
                connection.execute(
                    """
                    INSERT INTO approval_route_steps(
                        approval_route_run_id, sequence, employee_id,
                        employee_name, employee_email, role_labels_json,
                        assignment_source, requires_manual_assignment,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        route_run_id,
                        sequence,
                        employee.get("id"),
                        employee.get("name"),
                        employee.get("email"),
                        compact_json(step["role_labels"]),
                        step["assignment_source"],
                        int(bool(step.get("requires_manual_assignment"))),
                        utc_now(),
                    ),
                )
            return route_run_id
    except sqlite3.Error as exc:
        raise ApprovalStorageError(f"Could not save approval route: {exc}") from exc
    finally:
        connection.close()
