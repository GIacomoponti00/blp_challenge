"""Relational master-data storage and append-only matching-run persistence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MASTER_SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS master_data_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_sha256 TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_master_data (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS master_suppliers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    vat_id TEXT,
    normalized_vat_id TEXT,
    country TEXT,
    default_currency TEXT,
    payment_terms_days INTEGER,
    preferred INTEGER NOT NULL,
    status TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_master_suppliers_vat
    ON master_suppliers(normalized_vat_id);
CREATE INDEX IF NOT EXISTS idx_master_suppliers_name
    ON master_suppliers(normalized_name);

CREATE TABLE IF NOT EXISTS master_cost_centers (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_employee_id TEXT NOT NULL,
    department_code TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS master_departments (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    head_employee_id TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS master_employees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    normalized_email TEXT NOT NULL,
    role TEXT NOT NULL,
    deputy_for TEXT,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_master_employees_email
    ON master_employees(normalized_email);

CREATE TABLE IF NOT EXISTS master_gl_accounts (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS master_approval_limits_chf (
    sequence INTEGER PRIMARY KEY,
    lower_bound TEXT NOT NULL,
    upper_bound TEXT,
    required_roles_json TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS master_fx_rates_to_chf (
    currency TEXT PRIMARY KEY,
    rate TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS master_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS matching_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalization_run_id INTEGER NOT NULL,
    master_snapshot_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid', 'needs_review', 'failed')),
    matcher_version TEXT NOT NULL,
    matching_json TEXT,
    issues_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (normalization_run_id) REFERENCES normalization_runs(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (master_snapshot_id) REFERENCES master_data_snapshots(id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_matching_runs_normalization_created
    ON matching_runs(normalization_run_id, id DESC);
"""


class MasterDataStorageError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_vat_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return normalized or None


def normalize_supplier_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().casefold()
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = text.replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(character for character in text if not unicodedata.combining(character))
    normalized = re.sub(r"[^a-z0-9]", "", text)
    return normalized or None


def normalize_email(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def open_master_database(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise MasterDataStorageError(f"Database does not exist: {resolved}")
    try:
        connection = sqlite3.connect(resolved)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO schema_metadata(key, value)
            VALUES ('master_data_schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(MASTER_SCHEMA_VERSION),),
        )
        connection.commit()
        return connection
    except sqlite3.Error as exc:
        raise MasterDataStorageError(
            f"Could not initialize master-data tables in {resolved}: {exc}"
        ) from exc


def require_list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise MasterDataStorageError(f"master_data.json field {key!r} must be a list")
    return value


def validate_master_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MasterDataStorageError("master_data.json must contain one JSON object")
    for key in (
        "suppliers",
        "cost_centers",
        "departments",
        "employees",
        "gl_accounts",
        "approval_limits_chf",
    ):
        require_list(payload, key)
    meta = payload.get("_meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("fx_rates_to_chf"), dict):
        raise MasterDataStorageError("_meta.fx_rates_to_chf must be an object")
    if not isinstance(payload.get("rules"), dict):
        raise MasterDataStorageError("rules must be an object")

    employee_ids = {item.get("id") for item in payload["employees"] if isinstance(item, dict)}
    department_codes = {
        item.get("code") for item in payload["departments"] if isinstance(item, dict)
    }
    for center in payload["cost_centers"]:
        if not isinstance(center, dict):
            raise MasterDataStorageError("Every cost center must be an object")
        if center.get("owner_employee_id") not in employee_ids:
            raise MasterDataStorageError(
                f"Cost center {center.get('code')!r} references an unknown owner"
            )
        if center.get("department") not in department_codes:
            raise MasterDataStorageError(
                f"Cost center {center.get('code')!r} references an unknown department"
            )
    for department in payload["departments"]:
        if not isinstance(department, dict):
            raise MasterDataStorageError("Every department must be an object")
        if department.get("head_employee_id") not in employee_ids:
            raise MasterDataStorageError(
                f"Department {department.get('code')!r} references an unknown head"
            )
    return payload


def sync_master_data(db_path: Path, master_data_path: Path) -> dict[str, Any]:
    """Load the JSON source of truth into relational tables in one transaction."""
    resolved = master_data_path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise MasterDataStorageError(f"Could not read {resolved}: {exc}") from exc
    try:
        payload = validate_master_payload(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MasterDataStorageError(f"Invalid UTF-8 JSON in {resolved}: {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    connection = open_master_database(db_path)
    try:
        current = connection.execute(
            """
            SELECT s.id, s.source_sha256
            FROM current_master_data c
            JOIN master_data_snapshots s ON s.id = c.snapshot_id
            WHERE c.singleton_id = 1
            """
        ).fetchone()
        if current is not None and current["source_sha256"] == digest:
            return {
                "database": str(db_path.expanduser().resolve()),
                "snapshot_id": int(current["id"]),
                "source_sha256": digest,
                "status": "unchanged",
            }

        with connection:
            snapshot = connection.execute(
                "SELECT id FROM master_data_snapshots WHERE source_sha256 = ?",
                (digest,),
            ).fetchone()
            if snapshot is None:
                cursor = connection.execute(
                    """
                    INSERT INTO master_data_snapshots(
                        source_sha256, source_path, payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (digest, str(resolved), compact_json(payload), utc_now()),
                )
                snapshot_id = int(cursor.lastrowid)
            else:
                snapshot_id = int(snapshot["id"])

            for table in (
                "master_suppliers",
                "master_cost_centers",
                "master_departments",
                "master_employees",
                "master_gl_accounts",
                "master_approval_limits_chf",
                "master_fx_rates_to_chf",
                "master_settings",
            ):
                connection.execute(f"DELETE FROM {table}")

            connection.executemany(
                """
                INSERT INTO master_suppliers(
                    id, name, normalized_name, vat_id, normalized_vat_id,
                    country, default_currency, payment_terms_days, preferred,
                    status, snapshot_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["id"],
                        item["name"],
                        normalize_supplier_name(item["name"]),
                        item.get("vat_id"),
                        normalize_vat_id(item.get("vat_id")),
                        item.get("country"),
                        item.get("default_currency"),
                        item.get("payment_terms_days"),
                        int(bool(item.get("preferred", False))),
                        item.get("status", "active"),
                        snapshot_id,
                    )
                    for item in payload["suppliers"]
                ],
            )
            connection.executemany(
                """
                INSERT INTO master_employees(
                    id, name, email, normalized_email, role, deputy_for, snapshot_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["id"],
                        item["name"],
                        item["email"],
                        normalize_email(item["email"]),
                        item["role"],
                        item.get("deputy_for"),
                        snapshot_id,
                    )
                    for item in payload["employees"]
                ],
            )
            connection.executemany(
                """
                INSERT INTO master_departments(
                    code, name, head_employee_id, snapshot_id
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        item["code"],
                        item["name"],
                        item["head_employee_id"],
                        snapshot_id,
                    )
                    for item in payload["departments"]
                ],
            )
            connection.executemany(
                """
                INSERT INTO master_cost_centers(
                    code, name, owner_employee_id, department_code, snapshot_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["code"],
                        item["name"],
                        item["owner_employee_id"],
                        item["department"],
                        snapshot_id,
                    )
                    for item in payload["cost_centers"]
                ],
            )
            connection.executemany(
                "INSERT INTO master_gl_accounts(code, name, snapshot_id) VALUES (?, ?, ?)",
                [(item["code"], item["name"], snapshot_id) for item in payload["gl_accounts"]],
            )
            connection.executemany(
                """
                INSERT INTO master_approval_limits_chf(
                    sequence, lower_bound, upper_bound, required_roles_json, snapshot_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        index,
                        str(item["from"]),
                        str(item["to"]) if item.get("to") is not None else None,
                        compact_json(item["required_roles"]),
                        snapshot_id,
                    )
                    for index, item in enumerate(payload["approval_limits_chf"])
                ],
            )
            connection.executemany(
                "INSERT INTO master_fx_rates_to_chf(currency, rate, snapshot_id) VALUES (?, ?, ?)",
                [
                    (str(currency).upper(), str(rate), snapshot_id)
                    for currency, rate in payload["_meta"]["fx_rates_to_chf"].items()
                ],
            )
            settings = {
                **{f"meta.{key}": value for key, value in payload["_meta"].items() if key != "fx_rates_to_chf"},
                **{f"rule.{key}": value for key, value in payload["rules"].items()},
            }
            connection.executemany(
                "INSERT INTO master_settings(key, value_json, snapshot_id) VALUES (?, ?, ?)",
                [(key, compact_json(value), snapshot_id) for key, value in settings.items()],
            )
            connection.execute(
                """
                INSERT INTO current_master_data(singleton_id, snapshot_id)
                VALUES (1, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET snapshot_id = excluded.snapshot_id
                """,
                (snapshot_id,),
            )
    except (KeyError, TypeError, sqlite3.Error) as exc:
        raise MasterDataStorageError(f"Could not load master data: {exc}") from exc
    finally:
        connection.close()

    return {
        "database": str(db_path.expanduser().resolve()),
        "snapshot_id": snapshot_id,
        "source_sha256": digest,
        "status": "loaded",
        "counts": {
            key: len(payload[key])
            for key in (
                "suppliers",
                "cost_centers",
                "departments",
                "employees",
                "gl_accounts",
                "approval_limits_chf",
            )
        },
    }


def load_current_master_data(db_path: Path) -> dict[str, Any]:
    connection = open_master_database(db_path)
    try:
        snapshot = connection.execute(
            """
            SELECT s.id, s.source_sha256, s.source_path, s.created_at_utc
            FROM current_master_data c
            JOIN master_data_snapshots s ON s.id = c.snapshot_id
            WHERE c.singleton_id = 1
            """
        ).fetchone()
        if snapshot is None:
            raise MasterDataStorageError(
                "No master data is loaded; run with --master-data master_data.json first"
            )
        result = {
            "snapshot": dict(snapshot),
            "suppliers": [dict(row) for row in connection.execute("SELECT * FROM master_suppliers ORDER BY id")],
            "cost_centers": [dict(row) for row in connection.execute("SELECT * FROM master_cost_centers ORDER BY code")],
            "departments": [dict(row) for row in connection.execute("SELECT * FROM master_departments ORDER BY code")],
            "employees": [dict(row) for row in connection.execute("SELECT * FROM master_employees ORDER BY id")],
            "gl_accounts": [dict(row) for row in connection.execute("SELECT * FROM master_gl_accounts ORDER BY code")],
            "approval_limits_chf": [dict(row) for row in connection.execute("SELECT * FROM master_approval_limits_chf ORDER BY sequence")],
            "fx_rates_to_chf": [dict(row) for row in connection.execute("SELECT * FROM master_fx_rates_to_chf ORDER BY currency")],
        }
        return result
    except sqlite3.Error as exc:
        raise MasterDataStorageError(f"Could not load current master data: {exc}") from exc
    finally:
        connection.close()


def load_matching_input(
    db_path: Path,
    *,
    normalization_run_id: int | None = None,
    ingested_email_id: int | None = None,
) -> dict[str, Any]:
    if (normalization_run_id is None) == (ingested_email_id is None):
        raise MasterDataStorageError(
            "Choose exactly one of normalization_run_id or ingested_email_id"
        )
    connection = open_master_database(db_path)
    try:
        base_sql = """
            SELECT nr.id AS normalization_run_id, nr.status AS normalization_status,
                   nr.normalized_json, er.id AS extraction_run_id,
                   er.extraction_json, ie.id AS ingested_email_id,
                   ie.source_filename, ie.sender_email
            FROM normalization_runs nr
            JOIN extraction_runs er ON er.id = nr.extraction_run_id
            JOIN ingested_emails ie ON ie.id = er.ingested_email_id
        """
        if normalization_run_id is not None:
            row = connection.execute(
                base_sql + " WHERE nr.id = ?", (normalization_run_id,)
            ).fetchone()
            selector = f"normalization run ID {normalization_run_id}"
        else:
            row = connection.execute(
                base_sql
                + " WHERE ie.id = ? ORDER BY nr.id DESC LIMIT 1",
                (ingested_email_id,),
            ).fetchone()
            selector = f"ingested email ID {ingested_email_id}"
    except sqlite3.Error as exc:
        raise MasterDataStorageError(f"Could not load matching input: {exc}") from exc
    finally:
        connection.close()
    if row is None:
        raise MasterDataStorageError(f"No normalization found for {selector}")
    if row["normalized_json"] is None or row["extraction_json"] is None:
        raise MasterDataStorageError(f"{selector} has no usable normalized/extracted JSON")
    try:
        normalized = json.loads(row["normalized_json"])
        extraction = json.loads(row["extraction_json"])
    except json.JSONDecodeError as exc:
        raise MasterDataStorageError(f"Stored JSON for {selector} is invalid") from exc
    return {
        **{key: row[key] for key in row.keys() if not key.endswith("_json")},
        "normalized": normalized,
        "extraction": extraction,
    }


def list_normalized_email_ids(db_path: Path) -> list[int]:
    connection = open_master_database(db_path)
    try:
        return [
            int(row["ingested_email_id"])
            for row in connection.execute(
                """
                SELECT DISTINCT er.ingested_email_id
                FROM normalization_runs nr
                JOIN extraction_runs er ON er.id = nr.extraction_run_id
                ORDER BY er.ingested_email_id
                """
            )
        ]
    except sqlite3.Error as exc:
        raise MasterDataStorageError(f"Could not list normalized emails: {exc}") from exc
    finally:
        connection.close()


def save_matching_run(
    db_path: Path,
    *,
    normalization_run_id: int,
    master_snapshot_id: int,
    status: str,
    matcher_version: str,
    matching: Any = None,
    issues: Any = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    if status not in {"valid", "needs_review", "failed"}:
        raise MasterDataStorageError(f"Unsupported matching status: {status}")
    connection = open_master_database(db_path)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO matching_runs(
                    normalization_run_id, master_snapshot_id, status,
                    matcher_version, matching_json, issues_json,
                    error_code, error_message, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalization_run_id,
                    master_snapshot_id,
                    status,
                    matcher_version,
                    compact_json(matching) if matching is not None else None,
                    compact_json(issues) if issues is not None else None,
                    error_code,
                    error_message,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        raise MasterDataStorageError(f"Could not save matching run: {exc}") from exc
    finally:
        connection.close()
