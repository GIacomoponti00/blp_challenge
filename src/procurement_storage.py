"""SQLite persistence shared by ingestion and LLM extraction.

The complete parsed email record is stored in one JSON column. Frequently used
identity fields are also projected into columns for safe, efficient lookup. LLM
extraction runs live in a separate append-only table and reference the source
email; rerunning extraction therefore never overwrites the parsed source.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(os.environ.get("PROCUREMENT_DB_PATH", "procurement.db"))
DB_SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_sha256 TEXT NOT NULL UNIQUE,
    source_filename TEXT NOT NULL,
    message_id TEXT,
    email_date_utc TEXT,
    subject TEXT,
    sender_email TEXT,
    ingestion_schema_version INTEGER NOT NULL,
    parsed_payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingested_emails_filename
    ON ingested_emails(source_filename);
CREATE INDEX IF NOT EXISTS idx_ingested_emails_message_id
    ON ingested_emails(message_id);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingested_email_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid', 'needs_review', 'failed')),
    prompt_version TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    response_id TEXT,
    latency_ms INTEGER,
    usage_json TEXT,
    extraction_json TEXT,
    validation_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (ingested_email_id) REFERENCES ingested_emails(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_extraction_runs_email_created
    ON extraction_runs(ingested_email_id, created_at_utc DESC);
    
    
CREATE TABLE IF NOT EXISTS normalization_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_run_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid', 'needs_review', 'failed')),
    normalizer_version TEXT NOT NULL,
    normalized_json TEXT,
    issues_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (extraction_run_id) REFERENCES extraction_runs(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_normalization_runs_extraction_created
    ON normalization_runs(extraction_run_id, created_at_utc DESC);
"""


class StorageError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def open_database(path: Path, *, create: bool) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not create and not path.is_file():
        raise StorageError(f"Database does not exist: {path}")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error as exc:
        raise StorageError(f"Could not open database {path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if create:
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                """
                INSERT INTO schema_metadata(key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(DB_SCHEMA_VERSION),),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.close()
            raise StorageError(f"Could not initialize database schema: {exc}") from exc
    return connection


def first_sender_email(record: dict[str, Any]) -> str | None:
    senders = record.get("headers", {}).get("from", [])
    if not isinstance(senders, list) or not senders or not isinstance(senders[0], dict):
        return None
    value = str(senders[0].get("address") or "").strip()
    return value or None


def upsert_ingested_records(
    db_path: Path, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Idempotently persist successfully parsed records by source SHA-256."""
    connection = open_database(db_path, create=True)
    inserted = 0
    updated = 0
    skipped = 0
    stored: list[dict[str, Any]] = []
    try:
        with connection:
            for record in records:
                source = record.get("source", {})
                sha256 = source.get("sha256")
                filename = source.get("filename")
                if record.get("error") or not sha256 or not filename:
                    skipped += 1
                    continue
                existing = connection.execute(
                    "SELECT id FROM ingested_emails WHERE source_sha256 = ?",
                    (sha256,),
                ).fetchone()
                now = utc_now()
                headers = record.get("headers", {})
                values = (
                    str(filename),
                    headers.get("message_id"),
                    headers.get("date_utc"),
                    headers.get("subject"),
                    first_sender_email(record),
                    int(record.get("schema_version", 1)),
                    compact_json(record),
                    now,
                    str(sha256),
                )
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO ingested_emails (
                            source_filename, message_id, email_date_utc, subject,
                            sender_email, ingestion_schema_version,
                            parsed_payload_json, created_at_utc, updated_at_utc,
                            source_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values[:-1] + (now, values[-1]),
                    )
                    email_id = int(cursor.lastrowid)
                    action = "inserted"
                    inserted += 1
                else:
                    email_id = int(existing["id"])
                    connection.execute(
                        """
                        UPDATE ingested_emails
                        SET source_filename = ?, message_id = ?, email_date_utc = ?,
                            subject = ?, sender_email = ?, ingestion_schema_version = ?,
                            parsed_payload_json = ?, updated_at_utc = ?
                        WHERE source_sha256 = ?
                        """,
                        values,
                    )
                    action = "updated"
                    updated += 1
                stored.append(
                    {"email_id": email_id, "filename": filename, "action": action}
                )
    except sqlite3.Error as exc:
        raise StorageError(f"Could not store parsed emails: {exc}") from exc
    finally:
        connection.close()
    return {
        "database": str(db_path.expanduser().resolve()),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "records": stored,
    }


def load_ingested_email(
    db_path: Path,
    *,
    email_id: int | None = None,
    filename: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if (email_id is None) == (filename is None):
        raise StorageError("Choose exactly one of email_id or filename")
    connection = open_database(db_path, create=False)
    try:
        if email_id is not None:
            rows = connection.execute(
                "SELECT id, parsed_payload_json FROM ingested_emails WHERE id = ?",
                (email_id,),
            ).fetchall()
            selector = f"email ID {email_id}"
        else:
            rows = connection.execute(
                """
                SELECT id, parsed_payload_json
                FROM ingested_emails
                WHERE source_filename = ?
                ORDER BY id
                """,
                (filename,),
            ).fetchall()
            selector = f"filename {filename!r}"
    except sqlite3.Error as exc:
        raise StorageError(f"Could not read parsed email: {exc}") from exc
    finally:
        connection.close()
    if len(rows) != 1:
        raise StorageError(f"Expected one row for {selector}; found {len(rows)}")
    try:
        record = json.loads(rows[0]["parsed_payload_json"])
    except json.JSONDecodeError as exc:
        raise StorageError(f"Stored payload for {selector} is invalid JSON") from exc
    if not isinstance(record, dict):
        raise StorageError(f"Stored payload for {selector} is not an object")
    return int(rows[0]["id"]), record


def list_ingested_emails(db_path: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    connection = open_database(db_path, create=False)
    try:
        rows = connection.execute(
            """
            SELECT id, source_filename, message_id, email_date_utc, subject,
                   sender_email, source_sha256, created_at_utc, updated_at_utc
            FROM ingested_emails
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise StorageError(f"Could not list parsed emails: {exc}") from exc
    finally:
        connection.close()
    return [dict(row) for row in rows]


def save_extraction_run(
    db_path: Path,
    *,
    ingested_email_id: int,
    status: str,
    prompt_version: str,
    prompt_sha256: str,
    provider: str | None = None,
    model: str | None = None,
    response_id: str | None = None,
    latency_ms: int | None = None,
    usage: Any = None,
    extraction: Any = None,
    validation: Any = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    if status not in {"valid", "needs_review", "failed"}:
        raise StorageError(f"Unsupported extraction status: {status}")
    connection = open_database(db_path, create=False)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO extraction_runs (
                    ingested_email_id, status, prompt_version, prompt_sha256,
                    provider, model, response_id, latency_ms, usage_json,
                    extraction_json, validation_json, error_code, error_message,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingested_email_id,
                    status,
                    prompt_version,
                    prompt_sha256,
                    provider,
                    model,
                    response_id,
                    latency_ms,
                    compact_json(usage) if usage is not None else None,
                    compact_json(extraction) if extraction is not None else None,
                    compact_json(validation) if validation is not None else None,
                    error_code,
                    error_message,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        raise StorageError(f"Could not save extraction run: {exc}") from exc
    finally:
        connection.close()
        
def load_extraction_for_normalization(
    db_path: Path,
    *,
    extraction_run_id: int | None = None,
    ingested_email_id: int | None = None,
) -> dict[str, Any]:
    """Load one extraction and its immutable parsed source record.

    Selecting by email deliberately uses the latest run even if it failed. This
    prevents normalization from silently falling back to stale extracted facts.
    """
    if (extraction_run_id is None) == (ingested_email_id is None):
        raise StorageError(
            "Choose exactly one of extraction_run_id or ingested_email_id"
        )
    connection = open_database(db_path, create=False)
    try:
        if extraction_run_id is not None:
            row = connection.execute(
                """
                SELECT er.id AS extraction_run_id, er.ingested_email_id,
                       er.status AS extraction_status, er.extraction_json,
                       ie.parsed_payload_json
                FROM extraction_runs AS er
                JOIN ingested_emails AS ie ON ie.id = er.ingested_email_id
                WHERE er.id = ?
                """,
                (extraction_run_id,),
            ).fetchone()
            selector = f"extraction run ID {extraction_run_id}"
        else:
            row = connection.execute(
                """
                SELECT er.id AS extraction_run_id, er.ingested_email_id,
                       er.status AS extraction_status, er.extraction_json,
                       ie.parsed_payload_json
                FROM extraction_runs AS er
                JOIN ingested_emails AS ie ON ie.id = er.ingested_email_id
                WHERE er.ingested_email_id = ?
                ORDER BY er.id DESC
                LIMIT 1
                """,
                (ingested_email_id,),
            ).fetchone()
            selector = f"ingested email ID {ingested_email_id}"
    except sqlite3.Error as exc:
        raise StorageError(f"Could not load extraction: {exc}") from exc
    finally:
        connection.close()
    if row is None:
        raise StorageError(f"No extraction found for {selector}")
    if row["extraction_json"] is None:
        raise StorageError(
            f"Latest extraction for {selector} has status "
            f"{row['extraction_status']!r} and no extracted object"
        )
    try:
        extraction = json.loads(row["extraction_json"])
        source_record = json.loads(row["parsed_payload_json"])
    except json.JSONDecodeError as exc:
        raise StorageError(f"Stored JSON for {selector} is invalid") from exc
    if not isinstance(extraction, dict) or not isinstance(source_record, dict):
        raise StorageError(f"Stored JSON for {selector} has the wrong shape")
    return {
        "extraction_run_id": int(row["extraction_run_id"]),
        "ingested_email_id": int(row["ingested_email_id"]),
        "extraction_status": row["extraction_status"],
        "extraction": extraction,
        "source_record": source_record,
    }


def save_normalization_run(
    db_path: Path,
    *,
    extraction_run_id: int,
    status: str,
    normalizer_version: str,
    normalized: Any = None,
    issues: Any = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    if status not in {"valid", "needs_review", "failed"}:
        raise StorageError(f"Unsupported normalization status: {status}")
    # create=True also applies additive CREATE TABLE IF NOT EXISTS migrations to
    # databases produced by an earlier pipeline stage.
    connection = open_database(db_path, create=True)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO normalization_runs (
                    extraction_run_id, status, normalizer_version,
                    normalized_json, issues_json, error_code, error_message,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extraction_run_id,
                    status,
                    normalizer_version,
                    compact_json(normalized) if normalized is not None else None,
                    compact_json(issues) if issues is not None else None,
                    error_code,
                    error_message,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        raise StorageError(f"Could not save normalization run: {exc}") from exc
    finally:
        connection.close()
