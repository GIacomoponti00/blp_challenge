#!/usr/bin/env python3
"""Layered duplicate/correction candidate detection with explicit clerk resolution."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from .procurement_storage import DEFAULT_DB_PATH
except ImportError:
    from procurement_storage import DEFAULT_DB_PATH

try:
    from .master_data_storage import normalize_supplier_name, normalize_vat_id
    from .workflow_storage import compact_json, open_workflow_database, utc_now
except ImportError:
    from master_data_storage import normalize_supplier_name, normalize_vat_id
    from workflow_storage import compact_json, open_workflow_database, utc_now


CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2, "exact": 3}
CORRECTION_PATTERN = re.compile(
    r"\b(korrektur|correction|corrected|ignoriere|ignore|letztes mail|last (?:mail|email))\b",
    re.IGNORECASE,
)


class DuplicateError(RuntimeError):
    pass


def normalize_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"[^a-z0-9]", "", value.casefold()) or None


def load_features(db_path: Path) -> list[dict[str, Any]]:
    connection = open_workflow_database(db_path)
    try:
        rows = connection.execute(
            """
            WITH latest_extraction AS (
                SELECT *, ROW_NUMBER() OVER(
                    PARTITION BY ingested_email_id ORDER BY id DESC
                ) AS rn
                FROM extraction_runs
            ),
            latest_normalization AS (
                SELECT nr.*, er.ingested_email_id,
                       ROW_NUMBER() OVER(
                           PARTITION BY er.ingested_email_id ORDER BY nr.id DESC
                       ) AS rn
                FROM normalization_runs nr
                JOIN extraction_runs er ON er.id = nr.extraction_run_id
            )
            SELECT ie.*, le.extraction_json, ln.normalized_json
            FROM ingested_emails ie
            LEFT JOIN latest_extraction le
              ON le.ingested_email_id = ie.id AND le.rn = 1
            LEFT JOIN latest_normalization ln
              ON ln.ingested_email_id = ie.id AND ln.rn = 1
            ORDER BY ie.id
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise DuplicateError(f"Could not load duplicate features: {exc}") from exc
    finally:
        connection.close()

    features = []
    for row in rows:
        try:
            parsed = json.loads(row["parsed_payload_json"])
            extraction = json.loads(row["extraction_json"]) if row["extraction_json"] else {}
            normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else {}
        except json.JSONDecodeError as exc:
            raise DuplicateError(f"Invalid stored JSON for email ID {row['id']}") from exc
        attachments = {
            item.get("sha256")
            for item in parsed.get("attachments", [])
            if isinstance(item, dict) and item.get("sha256")
        }
        headers = parsed.get("headers", {})
        references = set(headers.get("references") or []) | set(headers.get("in_reply_to") or [])
        body = parsed.get("body", {}).get("selected", {}).get("text") or ""
        net = normalized.get("totals", {}).get("confirmed_net_total")
        if net is None:
            net = extraction.get("stated_net_total") or extraction.get("stated_unclassified_total")
        features.append(
            {
                "id": int(row["id"]),
                "message_id": row["message_id"],
                "source_sha256": row["source_sha256"],
                "sender_email": (row["sender_email"] or "").casefold(),
                "subject": row["subject"],
                "subject_normalized": normalize_text(row["subject"]),
                "date": row["email_date_utc"],
                "attachments": attachments,
                "references": references,
                "body": body,
                "is_correction": bool(CORRECTION_PATTERN.search(body + " " + (row["subject"] or ""))),
                "quote_number": normalize_text(extraction.get("quote_number")),
                "supplier_vat": normalize_vat_id(extraction.get("supplier_vat_id")),
                "supplier_name": normalize_supplier_name(extraction.get("supplier_name")),
                "amount": str(net) if net is not None else None,
                "currency": extraction.get("currency") or normalized.get("currency"),
            }
        )
    return features


def add_candidate(
    candidates: dict[tuple[int, int, str], dict[str, Any]],
    left: int,
    right: int,
    relation_kind: str,
    confidence: str,
    evidence: dict[str, Any],
) -> None:
    if left > right:
        left, right = right, left
    key = (left, right, relation_kind)
    current = candidates.get(key)
    if current is None:
        candidates[key] = {"confidence": confidence, "evidence": [evidence]}
    else:
        if CONFIDENCE_RANK[confidence] > CONFIDENCE_RANK[current["confidence"]]:
            current["confidence"] = confidence
        if evidence not in current["evidence"]:
            current["evidence"].append(evidence)


def generate_candidates(features: list[dict[str, Any]]) -> dict[tuple[int, int, str], dict[str, Any]]:
    candidates: dict[tuple[int, int, str], dict[str, Any]] = {}
    for index, left in enumerate(features):
        for right in features[index + 1 :]:
            if left["message_id"] and left["message_id"] == right["message_id"]:
                add_candidate(candidates, left["id"], right["id"], "exact_duplicate", "exact", {"rule": "message_id", "value": left["message_id"]})
            if left["source_sha256"] == right["source_sha256"]:
                add_candidate(candidates, left["id"], right["id"], "exact_duplicate", "exact", {"rule": "source_sha256", "value": left["source_sha256"]})
            shared_attachments = sorted(left["attachments"] & right["attachments"])
            if shared_attachments:
                add_candidate(candidates, left["id"], right["id"], "suspected_duplicate", "high", {"rule": "attachment_sha256", "values": shared_attachments})
            quote_left = (left["quote_number"], left["supplier_vat"], left["amount"])
            quote_right = (right["quote_number"], right["supplier_vat"], right["amount"])
            if all(quote_left) and quote_left == quote_right:
                add_candidate(candidates, left["id"], right["id"], "suspected_duplicate", "high", {"rule": "quote_vat_amount", "value": quote_left})
            fingerprint_left = (
                left["sender_email"], left["subject_normalized"],
                left["supplier_name"], left["amount"], left["currency"],
            )
            fingerprint_right = (
                right["sender_email"], right["subject_normalized"],
                right["supplier_name"], right["amount"], right["currency"],
            )
            if all(fingerprint_left) and fingerprint_left == fingerprint_right:
                add_candidate(candidates, left["id"], right["id"], "suspected_duplicate", "medium", {"rule": "normalized_business_fingerprint", "value": fingerprint_left})
            threaded = (
                left["message_id"] in right["references"] if left["message_id"] else False
            ) or (
                right["message_id"] in left["references"] if right["message_id"] else False
            )
            if threaded:
                kind = "correction_candidate" if left["is_correction"] or right["is_correction"] else "suspected_duplicate"
                add_candidate(candidates, left["id"], right["id"], kind, "high", {"rule": "email_thread_headers"})

    for correction in (item for item in features if item["is_correction"]):
        if any(key[1] == correction["id"] and key[2] == "correction_candidate" for key in candidates):
            continue
        prior_same_sender = [
            item for item in features
            if item["id"] < correction["id"] and item["sender_email"] == correction["sender_email"]
        ][-3:]
        for prior in prior_same_sender:
            add_candidate(
                candidates,
                prior["id"],
                correction["id"],
                "correction_candidate",
                "low",
                {"rule": "ambiguous_correction_phrase_same_sender", "text": correction["body"][:200]},
            )
    return candidates


def block_later_requisition(connection: sqlite3.Connection, right_email_id: int, link_id: int) -> None:
    req = connection.execute(
        "SELECT id, state, version FROM requisitions WHERE ingested_email_id = ?",
        (right_email_id,),
    ).fetchone()
    if req is None or req["state"] not in {"pending_approval", "approved"}:
        return
    connection.execute(
        "UPDATE requisitions SET state='needs_review', version=version+1, updated_at_utc=? WHERE id=?",
        (utc_now(), req["id"]),
    )
    connection.execute(
        "UPDATE workflow_approval_steps SET status='cancelled' WHERE requisition_id=? AND status IN ('active','pending','approved')",
        (req["id"],),
    )
    connection.execute(
        """
        INSERT INTO workflow_events(
            requisition_id,event_type,from_state,to_state,actor,reason,details_json,created_at_utc
        ) VALUES (?, 'duplicate_candidate_detected', ?, 'needs_review', 'system',
                  'Potential duplicate/correction requires clerk review', ?, ?)
        """,
        (req["id"], req["state"], compact_json({"duplicate_link_id": link_id}), utc_now()),
    )


def scan_duplicates(db_path: Path) -> dict[str, Any]:
    features = load_features(db_path)
    candidates = generate_candidates(features)
    connection = open_workflow_database(db_path)
    inserted = updated = 0
    try:
        with connection:
            for (left, right, kind), candidate in candidates.items():
                existing = connection.execute(
                    "SELECT id FROM duplicate_links WHERE left_ingested_email_id=? AND right_ingested_email_id=? AND relation_kind=?",
                    (left, right, kind),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO duplicate_links(
                            left_ingested_email_id,right_ingested_email_id,
                            relation_kind,confidence,evidence_json,resolution,created_at_utc
                        ) VALUES (?,?,?,?,?,'pending',?)
                        """,
                        (left, right, kind, candidate["confidence"], compact_json(candidate["evidence"]), utc_now()),
                    )
                    link_id = int(cursor.lastrowid); inserted += 1
                else:
                    link_id = int(existing["id"])
                    connection.execute(
                        "UPDATE duplicate_links SET confidence=?, evidence_json=? WHERE id=?",
                        (candidate["confidence"], compact_json(candidate["evidence"]), link_id),
                    )
                    updated += 1
                block_later_requisition(connection, right, link_id)
    except sqlite3.Error as exc:
        raise DuplicateError(f"Could not persist duplicate candidates: {exc}") from exc
    finally:
        connection.close()
    return {"emails_scanned": len(features), "candidates": len(candidates), "inserted": inserted, "updated": updated}


def list_links(db_path: Path, *, resolution: str | None = None) -> list[dict[str, Any]]:
    connection = open_workflow_database(db_path)
    try:
        sql = "SELECT * FROM duplicate_links"
        params: tuple[Any, ...] = ()
        if resolution:
            sql += " WHERE resolution = ?"; params = (resolution,)
        sql += " ORDER BY id"
        result = []
        for row in connection.execute(sql, params):
            item = dict(row); item["evidence"] = json.loads(item.pop("evidence_json")); result.append(item)
        return result
    finally:
        connection.close()


def resolve_link(
    db_path: Path, *, link_id: int, resolution: str, actor: str, reason: str
) -> dict[str, Any]:
    if resolution not in {"confirmed", "dismissed"}:
        raise DuplicateError("Resolution must be confirmed or dismissed")
    connection = open_workflow_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        link = connection.execute("SELECT * FROM duplicate_links WHERE id=?", (link_id,)).fetchone()
        if link is None or link["resolution"] != "pending":
            raise DuplicateError("Duplicate link does not exist or is already resolved")
        connection.execute(
            "UPDATE duplicate_links SET resolution=?,resolved_by=?,resolution_reason=?,resolved_at_utc=? WHERE id=?",
            (resolution, actor, reason, utc_now(), link_id),
        )
        if resolution == "confirmed":
            original = connection.execute("SELECT id FROM requisitions WHERE ingested_email_id=?", (link["left_ingested_email_id"],)).fetchone()
            duplicate = connection.execute("SELECT id,state FROM requisitions WHERE ingested_email_id=?", (link["right_ingested_email_id"],)).fetchone()
            if duplicate is not None:
                connection.execute(
                    "UPDATE requisitions SET state='duplicate',duplicate_of_requisition_id=?,version=version+1,updated_at_utc=? WHERE id=?",
                    (original["id"] if original else None, utc_now(), duplicate["id"]),
                )
                connection.execute("UPDATE workflow_approval_steps SET status='cancelled' WHERE requisition_id=? AND status IN ('draft','active','pending','approved')", (duplicate["id"],))
                connection.execute(
                    "INSERT INTO workflow_events(requisition_id,event_type,from_state,to_state,actor,reason,details_json,created_at_utc) VALUES (?,'duplicate_confirmed',?,'duplicate',?,?,?,?)",
                    (duplicate["id"], duplicate["state"], actor, reason, compact_json({"duplicate_link_id": link_id, "original_requisition_id": original["id"] if original else None}), utc_now()),
                )
        connection.commit()
        return {"link_id": link_id, "resolution": resolution}
    except sqlite3.Error as exc:
        connection.rollback(); raise DuplicateError(f"Could not resolve duplicate link: {exc}") from exc
    except DuplicateError:
        connection.rollback(); raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect and resolve duplicate/correction candidates")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan")
    listing = sub.add_parser("list"); listing.add_argument("--resolution", choices=("pending","confirmed","dismissed"))
    resolve = sub.add_parser("resolve"); resolve.add_argument("--id",type=int,required=True); resolve.add_argument("--resolution",choices=("confirmed","dismissed"),required=True); resolve.add_argument("--actor",required=True); resolve.add_argument("--reason",required=True)
    args = parser.parse_args(argv)
    if args.command == "scan": result = scan_duplicates(args.db)
    elif args.command == "list": result = list_links(args.db,resolution=args.resolution)
    else: result = resolve_link(args.db,link_id=args.id,resolution=args.resolution,actor=args.actor,reason=args.reason)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except DuplicateError as exc:
        print(f"Duplicate handling failed: {exc}",file=sys.stderr); raise SystemExit(2) from exc
