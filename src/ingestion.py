#!/usr/bin/env python3
"""Parse RFC 5322 email files and extract text from attached PDFs.

This is the ingestion boundary for the purchase-requisition challenge. It does
not call an LLM, match master data, calculate totals, or make workflow decisions.
Its job is to turn untrusted source files into provenance-rich records and store
one record per email in SQLite. JSON export remains available for debugging.

Dependencies:
    python -m pip install -r requirements.txt

Examples:
    python subproblem1_ingestion.py email.eml --sidecar-dir quotes
    python subproblem1_ingestion.py emails --sidecar-dir quotes --db procurement.db
    python subproblem1_ingestion.py emails --sidecar-dir quotes -o parsed-export.json

The optional sidecar directory supports the challenge fixture that writes
"[Attachment: quote.pdf]" in the body instead of embedding the PDF as MIME.
Sidecar resolution is exact-filename-only and confined to the supplied folder.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from datetime import timezone
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

try:  # Support both `python outputs/script.py` and package-style imports.
    from .procurement_storage import (
        DEFAULT_DB_PATH,
        StorageError,
        upsert_ingested_records,
    )
except ImportError:
    from procurement_storage import (
        DEFAULT_DB_PATH,
        StorageError,
        upsert_ingested_records,
    )

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - startup guard
    raise SystemExit(
        "Missing dependency 'pypdf'. Install it with: "
        "python -m pip install -r requirements.txt"
    ) from exc


SCHEMA_VERSION = 1
DEFAULT_MAX_EMAIL_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "dl",
    "dt",
    "dd",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}
IGNORED_HTML_TAGS = {"head", "script", "style", "svg", "template"}

SIDECAR_MARKER = re.compile(
    r"\[\s*Attachment\s*:\s*([^\]\r\n]+?\.pdf)\s*\]", re.IGNORECASE
)
MESSAGE_ID_TOKEN = re.compile(r"<[^<>\s]+>")
THREAD_BOUNDARY = re.compile(
    r"(?im)^(?:"
    r"-{4,}\s*(?:forwarded|original|weitergeleitete)[^\r\n]*-{2,}\s*$"
    r"|>\s*(?:from|von|de)\s*:"
    r"|on\s+.+\s+wrote\s*:"
    r"|am\s+.+\s+schrieb\s+.+\s*:"
    r")"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def clean_plain_text(value: str) -> str:
    """Normalize transport whitespace without flattening tables or threads."""
    value = (
        normalize_newlines(value)
        .replace("\x00", "")
        .replace("\u00a0", " ")
        .replace("\u200b", "")
    )
    lines = [line.rstrip() for line in value.split("\n")]
    output: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if output and not blank:
                output.append("")
            blank = True
        else:
            output.append(line)
            blank = False
    return "\n".join(output).strip()


class HTMLToText(HTMLParser):
    """Small, dependency-free HTML converter that preserves table columns."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0
        self._in_pre = False

    def _newline(self) -> None:
        if not self._chunks or not self._chunks[-1].endswith("\n"):
            self._chunks.append("\n")

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        tag = tag.lower()
        if tag in IGNORED_HTML_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "pre":
            self._in_pre = True
        if tag in BLOCK_TAGS:
            self._newline()
        if tag in {"td", "th"} and self._chunks:
            if not self._chunks[-1].endswith(("\n", "\t")):
                self._chunks.append("\t")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in IGNORED_HTML_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "pre":
            self._in_pre = False
        if tag in BLOCK_TAGS or tag in {"td", "th"}:
            self._newline() if tag in BLOCK_TAGS else self._chunks.append("\t")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not data:
            return
        if self._in_pre:
            self._chunks.append(data)
        else:
            self._chunks.append(re.sub(r"[ \f\v]+", " ", data))

    def text(self) -> str:
        value = "".join(self._chunks)
        value = re.sub(r"\t+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return clean_plain_text(value)


def html_to_text(value: str) -> str:
    parser = HTMLToText()
    parser.feed(value)
    parser.close()
    return parser.text()


def decode_text_part(part: Message) -> tuple[str, list[str]]:
    """Decode a text MIME part, returning text plus any warnings."""
    warnings: list[str] = []
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content, warnings
        if isinstance(content, bytes):
            charset = part.get_content_charset() or "utf-8"
            return content.decode(charset, errors="replace"), warnings
    except (LookupError, UnicodeError, AttributeError) as exc:
        warnings.append(f"get_content failed: {type(exc).__name__}: {exc}")

    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return (raw if isinstance(raw, str) else ""), warnings

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset), warnings
    except (LookupError, UnicodeDecodeError):
        warnings.append(
            f"could not decode text with charset {charset!r}; used UTF-8 replacement"
        )
        return payload.decode("utf-8", errors="replace"), warnings


def attachment_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, AttributeError):
        content = b""
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode(part.get_content_charset() or "utf-8", errors="replace")
    return b""


def safe_filename(value: str | None, fallback: str) -> tuple[str, str | None]:
    """Remove directory components; report when the supplied name was unsafe."""
    if not value:
        return fallback, None
    normalized = value.replace("\\", "/")
    name = Path(normalized).name
    if not name or name in {".", ".."}:
        return fallback, f"invalid attachment filename {value!r}; used {fallback!r}"
    if name != normalized:
        return name, f"removed path components from attachment filename {value!r}"
    return name, None


def extract_pdf(raw: bytes, source_name: str) -> dict[str, Any]:
    """Extract PDF text page by page. OCR is deliberately out of scope."""
    result: dict[str, Any] = {
        "status": "error",
        "source_name": source_name,
        "page_count": 0,
        "pages": [],
        "combined_text": "",
        "warnings": [],
    }
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as exc:  # pypdf raises different errors by encryption type
                result["status"] = "encrypted"
                result["warnings"].append(
                    f"encrypted PDF could not be opened: {type(exc).__name__}: {exc}"
                )
                return result
            if not unlocked:
                result["status"] = "encrypted"
                result["warnings"].append("encrypted PDF requires a password")
                return result

        pages: list[dict[str, Any]] = []
        combined: list[str] = []
        for page_number, page in enumerate(reader.pages, start=1):
            page_warnings: list[str] = []
            try:
                try:
                    text = page.extract_text(extraction_mode="layout") or ""
                except TypeError:  # compatibility with older pypdf versions
                    text = page.extract_text() or ""
            except Exception as exc:
                text = ""
                page_warnings.append(
                    f"page extraction failed: {type(exc).__name__}: {exc}"
                )
            text = clean_plain_text(text)
            pages.append(
                {
                    "page_number": page_number,
                    "text": text,
                    "character_count": len(text),
                    "has_text": bool(text),
                    "warnings": page_warnings,
                }
            )
            combined.append(f"--- PAGE {page_number} ---\n{text}")

        result["page_count"] = len(pages)
        result["pages"] = pages
        result["combined_text"] = "\n\n".join(combined)
        if any(page["has_text"] for page in pages):
            result["status"] = "extracted"
        else:
            result["status"] = "no_text"
            result["warnings"].append(
                "PDF contains no extractable text; OCR/scanned PDFs are out of scope"
            )
        return result
    except Exception as exc:  # keep one bad attachment from losing the email
        result["warnings"].append(
            f"PDF could not be parsed: {type(exc).__name__}: {exc}"
        )
        return result


def address_records(header_value: Any) -> list[dict[str, str]]:
    if header_value is None:
        return []
    if hasattr(header_value, "addresses"):
        records = []
        for address in header_value.addresses:
            if isinstance(address, Address):
                records.append(
                    {
                        "display_name": address.display_name or "",
                        "address": address.addr_spec or "",
                    }
                )
        if records:
            return records
    return [
        {"display_name": name or "", "address": address or ""}
        for name, address in getaddresses([str(header_value)])
    ]


def parsed_date(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    raw = str(value)
    try:
        date = parsedate_to_datetime(raw)
        if date.tzinfo is None:
            return raw, date.isoformat()
        return raw, date.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return raw, None


def message_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    matches = MESSAGE_ID_TOKEN.findall(str(value))
    return matches or [str(value).strip()]


def split_thread(text: str) -> dict[str, Any]:
    match = THREAD_BOUNDARY.search(text)
    if not match:
        return {
            "split_detected": False,
            "top_level_text": text,
            "quoted_or_forwarded_text": "",
        }
    return {
        "split_detected": True,
        "top_level_text": text[: match.start()].strip(),
        "quoted_or_forwarded_text": text[match.start() :].strip(),
    }


def collect_body_parts(message: EmailMessage) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    for part_number, part in enumerate(message.walk(), start=1):
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment" or part.get_filename():
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        text, warnings = decode_text_part(part)
        normalized = html_to_text(text) if content_type == "text/html" else clean_plain_text(text)
        bodies.append(
            {
                "part_number": part_number,
                "content_type": content_type,
                "charset": part.get_content_charset(),
                "content_id": part.get("Content-ID"),
                "text": normalized,
                "warnings": warnings,
            }
        )
    return bodies


def select_body(message: EmailMessage, bodies: list[dict[str, Any]]) -> dict[str, Any]:
    selected_part: Message | None = None
    try:
        selected_part = message.get_body(preferencelist=("plain", "html"))
    except AttributeError:
        selected_part = None

    selected: dict[str, Any] | None = None
    if selected_part is not None:
        content_type = selected_part.get_content_type().lower()
        text, warnings = decode_text_part(selected_part)
        text = html_to_text(text) if content_type == "text/html" else clean_plain_text(text)
        selected = {
            "content_type": content_type,
            "text": text,
            "warnings": warnings,
        }
    if selected is None:
        preferred = next(
            (body for body in bodies if body["content_type"] == "text/plain" and body["text"]),
            None,
        ) or next((body for body in bodies if body["text"]), None)
        selected = {
            "content_type": preferred["content_type"] if preferred else None,
            "text": preferred["text"] if preferred else "",
            "warnings": list(preferred["warnings"]) if preferred else ["no text body found"],
        }
    selected["thread"] = split_thread(selected["text"])
    return selected


def is_pdf_attachment(filename: str, content_type: str) -> bool:
    return content_type in PDF_CONTENT_TYPES or filename.lower().endswith(".pdf")


def collect_mime_attachments(
    message: EmailMessage, max_attachment_bytes: int
) -> tuple[list[dict[str, Any]], list[str]]:
    attachments: list[dict[str, Any]] = []
    warnings: list[str] = []
    sequence = 0
    for part_number, part in enumerate(message.walk(), start=1):
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = (part.get_content_disposition() or "").lower()
        original_filename = part.get_filename()
        if disposition != "attachment" and not original_filename and content_type not in PDF_CONTENT_TYPES:
            continue

        sequence += 1
        fallback = f"attachment-{sequence}"
        if content_type in PDF_CONTENT_TYPES:
            fallback += ".pdf"
        filename, filename_warning = safe_filename(original_filename, fallback)
        if filename_warning:
            warnings.append(filename_warning)

        raw = attachment_bytes(part)
        record: dict[str, Any] = {
            "source": "mime",
            "part_number": part_number,
            "filename": filename,
            "original_filename": original_filename,
            "content_type": content_type,
            "content_disposition": disposition or None,
            "content_id": part.get("Content-ID"),
            "size_bytes": len(raw),
            "sha256": sha256_bytes(raw),
            "extraction": {"status": "not_applicable"},
            "warnings": [],
        }
        if len(raw) > max_attachment_bytes:
            record["extraction"] = {"status": "too_large"}
            record["warnings"].append(
                f"attachment exceeds {max_attachment_bytes} byte limit"
            )
        elif is_pdf_attachment(filename, content_type):
            record["extraction"] = extract_pdf(raw, filename)
        else:
            record["extraction"] = {
                "status": "unsupported_type",
                "reason": "only text PDFs are extracted in this challenge",
            }
        attachments.append(record)
    return attachments, warnings


def sidecar_names(texts: Iterable[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in SIDECAR_MARKER.finditer(text):
            name = match.group(1).strip().strip("\"'")
            if name not in seen:
                names.append(name)
                seen.add(name)
    return names


def collect_sidecar_attachments(
    marker_names: list[str],
    sidecar_dir: Path | None,
    existing: list[dict[str, Any]],
    max_attachment_bytes: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not marker_names:
        return [], []
    if sidecar_dir is None:
        return [], [
            f"body references sidecar attachment {name!r}, but --sidecar-dir was not supplied"
            for name in marker_names
        ]

    root = sidecar_dir.resolve()
    existing_hashes = {item["sha256"] for item in existing}
    existing_names = {item["filename"].casefold() for item in existing}
    records: list[dict[str, Any]] = []
    warnings: list[str] = []

    for marker_name in marker_names:
        filename, filename_warning = safe_filename(marker_name, "invalid-sidecar.pdf")
        if filename_warning or filename != marker_name.replace("\\", "/"):
            warnings.append(
                f"refused unsafe sidecar filename {marker_name!r}; exact basenames only"
            )
            continue
        if not filename.lower().endswith(".pdf"):
            warnings.append(f"ignored non-PDF sidecar marker {marker_name!r}")
            continue

        candidate = (root / filename).resolve()
        if candidate.parent != root:
            warnings.append(f"refused sidecar path outside configured folder: {marker_name!r}")
            continue
        if not candidate.is_file():
            warnings.append(f"sidecar PDF not found: {filename!r}")
            continue

        raw = candidate.read_bytes()
        digest = sha256_bytes(raw)
        if digest in existing_hashes or filename.casefold() in existing_names:
            warnings.append(f"sidecar {filename!r} duplicates an embedded attachment; skipped")
            continue

        record: dict[str, Any] = {
            "source": "sidecar",
            "part_number": None,
            "filename": filename,
            "original_filename": marker_name,
            "content_type": "application/pdf",
            "content_disposition": "fixture-sidecar",
            "content_id": None,
            "size_bytes": len(raw),
            "sha256": digest,
            "extraction": {"status": "not_started"},
            "warnings": [],
        }
        if len(raw) > max_attachment_bytes:
            record["extraction"] = {"status": "too_large"}
            record["warnings"].append(
                f"attachment exceeds {max_attachment_bytes} byte limit"
            )
        else:
            record["extraction"] = extract_pdf(raw, filename)
        records.append(record)
        existing_hashes.add(digest)
        existing_names.add(filename.casefold())
    return records, warnings


def parse_email_file(
    path: Path,
    *,
    sidecar_dir: Path | None = None,
    max_email_bytes: int = DEFAULT_MAX_EMAIL_BYTES,
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > max_email_bytes:
        raise ValueError(f"email exceeds {max_email_bytes} byte limit")

    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not isinstance(message, EmailMessage):
        raise ValueError("parser did not return an EmailMessage")

    bodies = collect_body_parts(message)
    selected_body = select_body(message, bodies)
    attachments, warnings = collect_mime_attachments(message, max_attachment_bytes)

    marker_texts = [selected_body["text"], *(body["text"] for body in bodies)]
    sidecars, sidecar_warnings = collect_sidecar_attachments(
        sidecar_names(marker_texts),
        sidecar_dir,
        attachments,
        max_attachment_bytes,
    )
    attachments.extend(sidecars)
    warnings.extend(sidecar_warnings)

    date_raw, date_utc = parsed_date(message.get("Date"))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "kind": "email",
            "filename": path.name,
            "size_bytes": len(raw),
            "sha256": sha256_bytes(raw),
        },
        "headers": {
            "message_id": str(message.get("Message-ID") or "").strip() or None,
            "in_reply_to": message_id_list(message.get("In-Reply-To")),
            "references": message_id_list(message.get("References")),
            "date_raw": date_raw,
            "date_utc": date_utc,
            "subject": str(message.get("Subject") or ""),
            "from": address_records(message.get("From")),
            "to": address_records(message.get("To")),
            "cc": address_records(message.get("Cc")),
            "reply_to": address_records(message.get("Reply-To")),
        },
        "body": {
            "selected": selected_body,
            "alternatives": bodies,
        },
        "attachments": attachments,
        "warnings": warnings,
    }


def discover_emails(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.casefold() != ".eml":
            raise ValueError(f"input file is not an .eml file: {input_path}")
        return [input_path]
    if input_path.is_dir():
        iterator = input_path.rglob("*.eml") if recursive else input_path.glob("*.eml")
        return sorted(iterator, key=lambda item: item.name.casefold())
    raise ValueError(f"input path does not exist: {input_path}")


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def positive_megabytes(value: str) -> int:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return int(number * 1024 * 1024)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse .eml files and extract text from attached PDFs."
    )
    parser.add_argument("input", type=Path, help="An .eml file or directory")
    parser.add_argument(
        "--sidecar-dir",
        type=Path,
        help="Optional exact-filename PDF folder for [Attachment: file.pdf] markers",
    )
    parser.add_argument(
        "--recursive", action="store_true", help="Find .eml files recursively"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=(
            "SQLite database for parsed emails "
            "(default: PROCUREMENT_DB_PATH or ./procurement.db)"
        ),
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Do not write SQLite; emit/export JSON only (debug/migration mode)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optionally export the complete ingestion packet as JSON",
    )
    parser.add_argument(
        "--max-email-mb",
        type=positive_megabytes,
        default=DEFAULT_MAX_EMAIL_BYTES,
        metavar="MB",
        help="Maximum email size in MiB (default: 50)",
    )
    parser.add_argument(
        "--max-attachment-mb",
        type=positive_megabytes,
        default=DEFAULT_MAX_ATTACHMENT_BYTES,
        metavar="MB",
        help="Maximum decoded attachment size in MiB (default: 25)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first bad email instead of recording an error",
    )
    parser.add_argument(
        "--compact", action="store_true", help="Emit compact rather than indented JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        paths = discover_emails(args.input, args.recursive)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not paths:
        print("error: no .eml files found", file=sys.stderr)
        return 2

    records: list[dict[str, Any]] = []
    failures = 0
    for path in paths:
        try:
            records.append(
                parse_email_file(
                    path,
                    sidecar_dir=args.sidecar_dir,
                    max_email_bytes=args.max_email_mb,
                    max_attachment_bytes=args.max_attachment_mb,
                )
            )
        except Exception as exc:
            failures += 1
            if args.fail_fast:
                raise
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": {"kind": "email", "filename": path.name},
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    packet = {
        "schema_version": SCHEMA_VERSION,
        "record_count": len(records),
        "failure_count": failures,
        "records": records,
    }
    if args.json_only:
        storage_summary = None
    else:
        try:
            storage_summary = upsert_ingested_records(args.db, records)
        except StorageError as exc:
            print(f"error: database persistence failed: {exc}", file=sys.stderr)
            return 2

    if args.output or args.json_only:
        rendered = json.dumps(
            packet,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
        )
        if args.output:
            atomic_write_text(args.output, rendered + "\n")
        else:
            sys.stdout.write(rendered + "\n")
    else:
        assert storage_summary is not None
        sys.stdout.write(
            json.dumps(
                {
                    "status": "stored" if not failures else "stored_with_failures",
                    "parsed_records": len(records) - failures,
                    "parse_failures": failures,
                    "database": storage_summary["database"],
                    "inserted": storage_summary["inserted"],
                    "updated": storage_summary["updated"],
                    "skipped": storage_summary["skipped"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
