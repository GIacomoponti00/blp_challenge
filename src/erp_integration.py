#!/usr/bin/env python3
"""Submit approved requisitions to the mock ERP exactly once.

The database record is written before the HTTP request.  A retry therefore uses
the same Idempotency-Key even when the first response was lost after the ERP had
already booked the order.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from .procurement_storage import DEFAULT_DB_PATH
    from .workflow_service import WorkflowError, add_event, validate_data
    from .workflow_storage import (
        WorkflowStorageError,
        compact_json,
        get_requisition,
        open_workflow_database,
        utc_now,
    )
except ImportError:
    from procurement_storage import DEFAULT_DB_PATH
    from workflow_service import WorkflowError, add_event, validate_data
    from workflow_storage import (
        WorkflowStorageError,
        compact_json,
        get_requisition,
        open_workflow_database,
        utc_now,
    )


DEFAULT_ERP_ENDPOINT = "http://127.0.0.1:8080/purchase-orders"


class ERPIntegrationError(RuntimeError):
    pass


def json_number(value: Any, *, field: str) -> int | float:
    """Convert an internal decimal string to a JSON number at the HTTP edge."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ERPIntegrationError(f"{field} is not a valid decimal: {value!r}") from exc
    if not number.is_finite():
        raise ERPIntegrationError(f"{field} must be finite")
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def build_po_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Map the canonical requisition projection to the mock ERP contract."""
    payload: dict[str, Any] = {
        "supplier_id": data.get("supplier_id"),
        "currency": data.get("currency"),
        "total": json_number(data.get("total"), field="total"),
        "requested_delivery_date": data.get("requested_delivery_date"),
        "line_items": [],
    }
    for index, line in enumerate(data.get("line_items") or []):
        mapped = {
            "item_name": line.get("item_name"),
            "quantity": json_number(line.get("quantity"), field=f"line_items[{index}].quantity"),
            "unit_price": json_number(line.get("unit_price"), field=f"line_items[{index}].unit_price"),
            "unit": line.get("unit"),
        }
        if line.get("item_code") not in (None, ""):
            mapped["item_code"] = line["item_code"]
        payload["line_items"].append(mapped)
    for field in ("cost_centre", "gl_account", "requisition_reference"):
        if data.get(field) not in (None, ""):
            payload[field] = data[field]
    if data.get("tax_amount") not in (None, ""):
        payload["tax_amount"] = json_number(data["tax_amount"], field="tax_amount")
    return payload


def decode_response(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_body": text}


def post_purchase_order(
    endpoint: str,
    *,
    payload: dict[str, Any],
    idempotency_key: str,
    timeout_seconds: float = 15.0,
) -> tuple[int, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return int(response.status), decode_response(response.read())
    except HTTPError as exc:
        return int(exc.code), decode_response(exc.read())
    except (URLError, TimeoutError, OSError) as exc:
        raise ERPIntegrationError(f"ERP request failed: {exc}") from exc


def _prepare_submission(
    db_path: Path,
    *,
    requisition_id: int,
    expected_version: int,
    endpoint: str,
    actor: str,
) -> dict[str, Any]:
    """Durably reserve the idempotency key before any network I/O."""
    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM requisitions WHERE id = ?", (requisition_id,)
        ).fetchone()
        if row is None:
            raise ERPIntegrationError(f"Requisition ID {requisition_id} does not exist")
        current_version = int(row["version"])
        if current_version != expected_version:
            raise ERPIntegrationError(
                f"Stale version: expected {expected_version}, current is {current_version}"
            )

        succeeded = connection.execute(
            """
            SELECT * FROM po_submissions
            WHERE requisition_id = ? AND requisition_version = ? AND status = 'succeeded'
            ORDER BY id DESC LIMIT 1
            """,
            (requisition_id, current_version),
        ).fetchone()
        if succeeded is not None:
            connection.rollback()
            return {"already_succeeded": True, **dict(succeeded)}

        if row["state"] not in {"approved", "ordering"}:
            raise ERPIntegrationError(
                f"Only an approved requisition can be submitted; current state is {row['state']!r}"
            )
        requisition = dict(row)
        requisition["data"] = json.loads(requisition.pop("data_json"))
        requisition["route_basis"] = json.loads(requisition.pop("route_basis_json"))
        requisition["requires_route_rebuild"] = bool(requisition["requires_route_rebuild"])
        issues = validate_data(connection, requisition)
        if issues:
            rendered = "; ".join(f"{item['field_path']}: {item['message']}" for item in issues)
            raise ERPIntegrationError(f"Requisition is not ERP-ready: {rendered}")

        payload = build_po_payload(requisition["data"])
        idempotency_key = f"requisition-{requisition_id}-v{current_version}"
        existing = connection.execute(
            "SELECT * FROM po_submissions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        now = utc_now()
        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO po_submissions(
                    requisition_id, requisition_version, idempotency_key,
                    endpoint, request_json, status, attempt_count,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                """,
                (
                    requisition_id,
                    current_version,
                    idempotency_key,
                    endpoint,
                    compact_json(payload),
                    now,
                    now,
                ),
            )
            submission_id = int(cursor.lastrowid)
        else:
            if existing["endpoint"] != endpoint:
                raise ERPIntegrationError(
                    "This requisition version was already reserved for endpoint "
                    f"{existing['endpoint']!r}; retry that endpoint"
                )
            if existing["request_json"] != compact_json(payload):
                raise ERPIntegrationError(
                    "Requisition data changed without a version change; submission refused"
                )
            submission_id = int(existing["id"])
            connection.execute(
                """
                UPDATE po_submissions
                SET status = 'pending', attempt_count = attempt_count + 1,
                    http_status = NULL, error_message = NULL, updated_at_utc = ?
                WHERE id = ?
                """,
                (now, submission_id),
            )
        if row["state"] == "approved":
            connection.execute(
                "UPDATE requisitions SET state = 'ordering', updated_at_utc = ? WHERE id = ?",
                (now, requisition_id),
            )
            add_event(
                connection,
                requisition_id=requisition_id,
                event_type="po_submission_started",
                actor=actor,
                from_state="approved",
                to_state="ordering",
                details={"idempotency_key": idempotency_key, "endpoint": endpoint},
            )
        connection.commit()
        return {
            "already_succeeded": False,
            "submission_id": submission_id,
            "idempotency_key": idempotency_key,
            "payload": payload,
        }
    except (sqlite3.Error, json.JSONDecodeError, WorkflowError) as exc:
        connection.rollback()
        raise ERPIntegrationError(f"Could not prepare ERP submission: {exc}") from exc
    except ERPIntegrationError:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish_submission(
    db_path: Path,
    *,
    requisition_id: int,
    submission_id: int,
    status: str,
    http_status: int | None,
    response: Any,
    error_message: str | None,
    actor: str,
) -> dict[str, Any]:
    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        submission = connection.execute(
            "SELECT * FROM po_submissions WHERE id = ? AND requisition_id = ?",
            (submission_id, requisition_id),
        ).fetchone()
        if submission is None:
            raise ERPIntegrationError(f"PO submission ID {submission_id} does not exist")
        now = utc_now()
        connection.execute(
            """
            UPDATE po_submissions
            SET status = ?, http_status = ?, response_json = ?,
                error_message = ?, updated_at_utc = ?
            WHERE id = ?
            """,
            (
                status,
                http_status,
                compact_json(response) if response is not None else None,
                error_message,
                now,
                submission_id,
            ),
        )
        if status == "succeeded":
            po_number = response.get("po_number") if isinstance(response, dict) else None
            connection.execute(
                """
                UPDATE requisitions
                SET state = 'ordered', po_number = ?, updated_at_utc = ?
                WHERE id = ?
                """,
                (po_number, now, requisition_id),
            )
            add_event(
                connection,
                requisition_id=requisition_id,
                event_type="po_ordered",
                actor=actor,
                from_state="ordering",
                to_state="ordered",
                details={
                    "submission_id": submission_id,
                    "http_status": http_status,
                    "po_number": po_number,
                },
            )
            state = "ordered"
        else:
            connection.execute(
                "UPDATE requisitions SET state = 'approved', updated_at_utc = ? WHERE id = ?",
                (now, requisition_id),
            )
            add_event(
                connection,
                requisition_id=requisition_id,
                event_type="po_submission_rejected" if status == "rejected" else "po_submission_failed",
                actor=actor,
                from_state="ordering",
                to_state="approved",
                reason=error_message,
                details={"submission_id": submission_id, "http_status": http_status, "response": response},
            )
            state = "approved"
        connection.commit()
        return {
            "requisition_id": requisition_id,
            "submission_id": submission_id,
            "submission_status": status,
            "state": state,
            "http_status": http_status,
            "response": response,
            "error": error_message,
        }
    except sqlite3.Error as exc:
        connection.rollback()
        raise ERPIntegrationError(f"Could not finalize ERP submission: {exc}") from exc
    except ERPIntegrationError:
        connection.rollback()
        raise
    finally:
        connection.close()


def submit_purchase_order(
    db_path: Path,
    *,
    requisition_id: int,
    expected_version: int,
    endpoint: str = DEFAULT_ERP_ENDPOINT,
    actor: str = "system",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    prepared = _prepare_submission(
        db_path,
        requisition_id=requisition_id,
        expected_version=expected_version,
        endpoint=endpoint,
        actor=actor,
    )
    if prepared["already_succeeded"]:
        response = json.loads(prepared["response_json"]) if prepared.get("response_json") else None
        return {
            "requisition_id": requisition_id,
            "submission_id": int(prepared["id"]),
            "submission_status": "succeeded",
            "state": "ordered",
            "http_status": prepared["http_status"],
            "response": response,
            "idempotent_replay": True,
        }
    try:
        http_status, response = post_purchase_order(
            endpoint,
            payload=prepared["payload"],
            idempotency_key=prepared["idempotency_key"],
            timeout_seconds=timeout_seconds,
        )
    except ERPIntegrationError as exc:
        return _finish_submission(
            db_path,
            requisition_id=requisition_id,
            submission_id=prepared["submission_id"],
            status="failed",
            http_status=None,
            response=None,
            error_message=str(exc),
            actor=actor,
        )
    if http_status in {200, 201}:
        if isinstance(response, dict) and response.get("po_number"):
            status = "succeeded"
            error = None
        else:
            status = "failed"
            error = "ERP success response did not contain po_number"
    elif http_status == 422:
        status = "rejected"
        error = "ERP rejected the payload"
    else:
        status = "failed"
        error = f"ERP returned HTTP {http_status}"
    result = _finish_submission(
        db_path,
        requisition_id=requisition_id,
        submission_id=prepared["submission_id"],
        status=status,
        http_status=http_status,
        response=response,
        error_message=error,
        actor=actor,
    )
    result["idempotency_key"] = prepared["idempotency_key"]
    result["idempotent_replay"] = http_status == 200 and status == "succeeded"
    return result


def submission_status(db_path: Path, *, requisition_id: int) -> list[dict[str, Any]]:
    return get_requisition(db_path, requisition_id)["po_submissions"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit approved requisitions to the mock ERP")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit")
    submit.add_argument("--id", type=int, required=True)
    submit.add_argument("--version", type=int, required=True)
    submit.add_argument("--endpoint", default=DEFAULT_ERP_ENDPOINT)
    submit.add_argument("--actor", default="system")
    submit.add_argument("--timeout", type=float, default=15.0)
    status = subparsers.add_parser("status")
    status.add_argument("--id", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "submit":
        result = submit_purchase_order(
            args.db,
            requisition_id=args.id,
            expected_version=args.version,
            endpoint=args.endpoint,
            actor=args.actor,
            timeout_seconds=args.timeout,
        )
    else:
        result = submission_status(args.db, requisition_id=args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ERPIntegrationError, WorkflowStorageError) as exc:
        print(f"ERP integration failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
