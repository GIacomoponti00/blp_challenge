#!/usr/bin/env python3
"""Small standard-library review UI for the procurement workflow demo.

This intentionally has no authentication.  It is a local take-home demo of the
workflow and audit behavior, not a production identity or authorization layer.
"""

from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from typing import Any, Callable

try:
    from .duplicate_detection import DuplicateError, resolve_link, scan_duplicates
    from .erp_integration import (
        DEFAULT_ERP_ENDPOINT,
        ERPIntegrationError,
        submit_purchase_order,
    )
    from .procurement_storage import DEFAULT_DB_PATH
    from .workflow_service import (
        WorkflowError,
        act_on_approval,
        assign_manual_approver,
        edit_requisition,
        initialize_all,
        rebuild_approval_route,
        resolve_review,
        validate_requisition,
    )
    from .workflow_storage import (
        WorkflowStorageError,
        get_requisition,
        list_requisitions,
    )
except ImportError:
    from duplicate_detection import DuplicateError, resolve_link, scan_duplicates
    from erp_integration import DEFAULT_ERP_ENDPOINT, ERPIntegrationError, submit_purchase_order
    from procurement_storage import DEFAULT_DB_PATH
    from workflow_service import (
        WorkflowError,
        act_on_approval,
        assign_manual_approver,
        edit_requisition,
        initialize_all,
        rebuild_approval_route,
        resolve_review,
        validate_requisition,
    )
    from workflow_storage import WorkflowStorageError, get_requisition, list_requisitions


FLOW = ["inbox", "extracted", "needs_review", "pending_approval", "approved", "ordering", "ordered"]
HANDLED_ERRORS = (WorkflowError, WorkflowStorageError, DuplicateError, ERPIntegrationError, ValueError)


def h(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def state_flow(current: str) -> str:
    if current in {"rejected", "duplicate"}:
        states = FLOW[:4] + [current]
    else:
        states = FLOW
    return '<div class="flow">' + "".join(
        f'<span class="state {"current" if state == current else ""}">{h(state.replace("_", " "))}</span>'
        for state in states
    ) + "</div>"


def hidden(name: str, value: Any) -> str:
    return f'<input type="hidden" name="{h(name)}" value="{h(value)}">'


def post_form(action: str, content: str, *, requisition_id: int | None = None, css: str = "") -> str:
    fields = hidden("action", action)
    if requisition_id is not None:
        fields += hidden("requisition_id", requisition_id)
    return f'<form method="post" action="/action" class="{h(css)}">{fields}{content}</form>'


def requisition_list(items: list[dict[str, Any]], selected_id: int | None) -> str:
    rows = []
    for item in items:
        selected = "selected" if item["id"] == selected_id else ""
        total = item["data"].get("total") or "?"
        currency = item["data"].get("currency") or ""
        rows.append(
            f'<a class="req {selected}" href="/?id={item["id"]}">'
            f'<strong>#{item["id"]} · {h(item["source_filename"])}</strong>'
            f'<span>{h(item["state"])} · {h(total)} {h(currency)}</span></a>'
        )
    return "".join(rows) or '<p class="muted">No requisitions yet.</p>'


def render_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return '<p class="ok">All workflow checks pass.</p>'
    return '<ul class="issues">' + "".join(
        f'<li><code>{h(item["field_path"])}</code> — {h(item["message"])}</li>' for item in issues
    ) + "</ul>"


def data_table(data: dict[str, Any]) -> str:
    fields = (
        "supplier_id", "currency", "total", "tax_amount", "requested_delivery_date",
        "cost_centre", "gl_account", "requester_id", "requisition_reference",
    )
    rows = "".join(f"<tr><th>{h(field)}</th><td>{h(data.get(field))}</td></tr>" for field in fields)
    line_rows = "".join(
        "<tr>" + "".join(f"<td>{h(line.get(field))}</td>" for field in ("item_name", "item_code", "quantity", "unit_price", "unit")) + "</tr>"
        for line in data.get("line_items", [])
    )
    return (
        f'<table class="kv">{rows}</table><h3>Line items</h3>'
        '<table><thead><tr><th>Name</th><th>Code</th><th>Qty</th><th>Unit price</th><th>Unit</th></tr></thead>'
        f'<tbody>{line_rows}</tbody></table>'
    )


def workflow_actions(req: dict[str, Any], endpoint: str) -> str:
    req_id, version, state = req["id"], req["version"], req["state"]
    cards: list[str] = []
    if state not in {"ordered", "duplicate", "rejected", "ordering"}:
        fields = (
            hidden("version", version)
            + '<label>Field path<input name="field" placeholder="total or line_items[0].quantity" required></label>'
            + '<label>New value<input name="value" placeholder="Leave blank to clear"></label>'
            + '<label>Your name<input name="actor" required></label>'
            + '<label>Reason<input name="reason" required></label>'
            + '<button>Save revision</button>'
        )
        cards.append('<section><h2>Edit extracted data</h2>' + post_form("edit", fields, requisition_id=req_id) + '</section>')

    if state == "needs_review":
        rebuild = ""
        if req["requires_route_rebuild"]:
            rebuild_fields = (
                hidden("version", version)
                + '<label>Your name<input name="actor" required></label>'
                + '<label>Reason<input name="reason" value="Rebuild after reviewed material edit" required></label>'
                + '<button>Rebuild approval route</button>'
            )
            rebuild = '<h3>Route-affecting edit detected</h3>' + post_form(
                "rebuild_route", rebuild_fields, requisition_id=req_id
            )
        manual = (
            hidden("version", version)
            + '<label>Step number<input name="sequence" type="number" min="1" required></label>'
            + '<label>Employee ID<input name="employee_id" required></label>'
            + '<label>Your name<input name="actor" required></label>'
            + '<label>Reason<input name="reason" required></label><button>Assign approver</button>'
        )
        resolve = hidden("version", version) + '<label>Your name<input name="actor" required></label><button>Send to approval</button>'
        cards.append(
            '<section><h2>Complete review</h2>'
            + rebuild
            + post_form("resolve_review", resolve, requisition_id=req_id, css="inline")
            + '<details><summary>Assign a missing approver</summary>'
            + post_form("assign_approver", manual, requisition_id=req_id)
            + '</details></section>'
        )
    if state == "pending_approval":
        active = next((step for step in req["steps"] if step["status"] == "active"), None)
        if active:
            actor = active.get("employee_id") or ""
            common = hidden("version", version) + hidden("actor_employee_id", actor)
            approve = common + f'<p>Acting as <code>{h(actor)}</code> · {h(active.get("employee_name"))}</p><button>Approve</button>'
            reject = common + '<label>Rejection reason<input name="reason" required></label><button class="danger">Reject</button>'
            cards.append('<section><h2>Active approval</h2><div class="actions">' + post_form("approve", approve, requisition_id=req_id) + post_form("reject", reject, requisition_id=req_id) + '</div></section>')
    if state == "approved":
        content = hidden("version", version) + '<label>Your name<input name="actor" value="system" required></label>' + hidden("endpoint", endpoint) + f'<p class="muted">Endpoint: {h(endpoint)}</p><button>Create purchase order</button>'
        cards.append('<section><h2>ERP submission</h2>' + post_form("submit_po", content, requisition_id=req_id) + '</section>')
    return "".join(cards)


def approval_table(steps: list[dict[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td>{step["route_step_sequence"]}</td><td>{h(", ".join(step["role_labels"]))}</td>'
        f'<td>{h(step.get("employee_name"))}<br><code>{h(step.get("employee_id"))}</code></td>'
        f'<td><span class="pill">{h(step["status"])}</span></td><td>{h(step.get("action_reason"))}</td></tr>'
        for step in steps
    )
    return '<table><thead><tr><th>#</th><th>Role</th><th>Approver</th><th>Status</th><th>Reason</th></tr></thead><tbody>' + rows + '</tbody></table>'


def duplicates_panel(req: dict[str, Any]) -> str:
    if not req["duplicate_links"]:
        return '<p class="muted">No duplicate candidates.</p>'
    blocks = []
    for link in req["duplicate_links"]:
        action = ""
        if link["resolution"] == "pending":
            common = hidden("link_id", link["id"]) + '<label>Your name<input name="actor" required></label><label>Reason<input name="reason" required></label>'
            action = '<div class="actions">' + post_form("confirm_duplicate", common + '<button class="danger">Confirm duplicate</button>', requisition_id=req["id"]) + post_form("dismiss_duplicate", common + '<button>Dismiss candidate</button>', requisition_id=req["id"]) + '</div>'
        blocks.append(
            f'<article><strong>Link #{link["id"]}: email {link["left_ingested_email_id"]} ↔ {link["right_ingested_email_id"]}</strong>'
            f'<p>{h(link["relation_kind"])} · {h(link["confidence"])} · {h(link["resolution"])}</p>'
            f'<pre>{h(pretty(link["evidence"]))}</pre>{action}</article>'
        )
    return "".join(blocks)


def audit_panel(req: dict[str, Any]) -> str:
    rows = "".join(
        f'<tr><td>{h(event["created_at_utc"])}</td><td>{h(event["event_type"])}</td>'
        f'<td>{h(event["actor"])}</td><td>{h(event.get("from_state"))} → {h(event.get("to_state"))}</td>'
        f'<td>{h(event.get("reason"))}</td></tr>' for event in req["events"]
    )
    return '<table><thead><tr><th>Time (UTC)</th><th>Event</th><th>Actor</th><th>Transition</th><th>Reason</th></tr></thead><tbody>' + rows + '</tbody></table>'


def detail(req: dict[str, Any], endpoint: str) -> str:
    validation = validate_requisition(APP_CONFIG["db"], req["id"])
    po = req["po_submissions"]
    return (
        f'<main><header><div><p class="eyebrow">Requisition #{req["id"]} · version {req["version"]}</p>'
        f'<h1>{h(req["subject"] or req["source_filename"])}</h1></div><span class="bigpill">{h(req["state"])}</span></header>'
        + state_flow(req["state"])
        + '<div class="grid"><section><h2>Canonical requisition</h2>' + data_table(req["data"]) + '</section>'
        + '<section><h2>Validation</h2>' + render_issues(validation["issues"]) + '</section></div>'
        + workflow_actions(req, endpoint)
        + '<section><h2>Approval route</h2>' + approval_table(req["steps"]) + '</section>'
        + '<section><h2>Duplicate review</h2>' + duplicates_panel(req) + '</section>'
        + '<section><h2>PO attempts</h2><pre>' + h(pretty(po)) + '</pre></section>'
        + '<section><h2>Audit trail</h2>' + audit_panel(req) + '</section></main>'
    )


CSS = """
:root{font-family:Inter,ui-sans-serif,system-ui;color:#15211b;background:#f4f7f5}*{box-sizing:border-box}
body{margin:0}.layout{display:grid;grid-template-columns:310px 1fr;min-height:100vh}.sidebar{padding:24px;background:#10271d;color:white;position:sticky;top:0;height:100vh;overflow:auto}.sidebar h1{font-size:20px}.sidebar p{color:#a9c1b5}.req{display:block;color:white;text-decoration:none;padding:12px;border-radius:9px;margin:5px 0;background:#18382a}.req span{display:block;color:#b9d2c5;font-size:12px;margin-top:4px}.req.selected{background:#2c6d4e}.sidebar form{margin-top:12px}main{padding:32px;max-width:1250px;width:100%;margin:auto}header{display:flex;justify-content:space-between;gap:20px;align-items:start}h1{margin:3px 0 18px}h2{font-size:18px}h3{font-size:15px}.eyebrow{color:#577064;text-transform:uppercase;font-size:12px;letter-spacing:.08em}.bigpill,.pill{display:inline-block;background:#dceee4;color:#15452f;padding:7px 11px;border-radius:999px}.pill{padding:3px 8px;font-size:12px}.flow{display:flex;gap:4px;flex-wrap:wrap;margin:10px 0 24px}.state{padding:8px 12px;background:#e3e9e6;color:#64736c}.state:first-child{border-radius:8px 0 0 8px}.state:last-child{border-radius:0 8px 8px 0}.state.current{background:#1e6848;color:white}.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px}section{background:white;border:1px solid #dce5e0;border-radius:12px;padding:20px;margin:0 0 18px;box-shadow:0 2px 8px #193d2b0a}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;border-bottom:1px solid #e5ebe8;padding:9px;vertical-align:top}.kv th{width:220px;color:#56685f}form{display:grid;gap:10px}form.inline{display:flex;align-items:end}.actions{display:flex;gap:14px;align-items:start}.actions form{flex:1}label{display:grid;gap:4px;font-size:13px;color:#42564c}input{font:inherit;padding:9px;border:1px solid #b8c8c0;border-radius:7px}button{font:inherit;border:0;border-radius:7px;padding:10px 14px;background:#1d6a48;color:white;cursor:pointer}.danger{background:#a63838}.muted{color:#6b7e74}.ok{color:#17643f}.issues{color:#8a2d2d;padding-left:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f5;padding:12px;border-radius:8px;font-size:12px}article{border-top:1px solid #e5ebe8;padding:14px 0}.flash{padding:12px 32px;background:#dff2e7;color:#154d32}.flash.error{background:#f8dddd;color:#7b2929}details{margin-top:14px}code{font-size:12px}@media(max-width:850px){.layout,.grid{display:block}.sidebar{height:auto;position:static}.actions{display:block}.actions form{margin-bottom:10px}}
"""


APP_CONFIG: dict[str, Any] = {}


def full_page(*, message: str | None = None, error: bool = False, selected_id: int | None = None) -> str:
    items = list_requisitions(APP_CONFIG["db"])
    if selected_id is None and items:
        selected_id = items[0]["id"]
    selected = get_requisition(APP_CONFIG["db"], selected_id) if selected_id is not None else None
    flash = f'<div class="flash {"error" if error else ""}">{h(message)}</div>' if message else ""
    controls = post_form("scan_duplicates", '<button>Rescan duplicates</button>') + post_form("initialize", '<button>Initialize routed emails</button>')
    body = '<div class="layout"><aside class="sidebar"><h1>Procurement Inbox</h1><p>Local review console</p>' + requisition_list(items, selected_id) + controls + '</aside>'
    body += detail(selected, APP_CONFIG["endpoint"]) if selected else '<main><h1>No workflow records</h1><p>Run matching and route construction, then initialize.</p></main>'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Procurement workflow</title><style>' + CSS + '</style></head><body>' + flash + body + '</div></body></html>'


def one(form: dict[str, list[str]], key: str, *, required: bool = True) -> str:
    value = form.get(key, [""])[0].strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value


def handle_action(form: dict[str, list[str]]) -> tuple[str, int | None]:
    action = one(form, "action")
    req_id = int(one(form, "requisition_id")) if form.get("requisition_id") else None
    if action == "initialize":
        result = initialize_all(APP_CONFIG["db"])
    elif action == "scan_duplicates":
        result = scan_duplicates(APP_CONFIG["db"])
    elif action == "edit":
        raw = one(form, "value", required=False)
        result = edit_requisition(APP_CONFIG["db"], requisition_id=req_id, expected_version=int(one(form, "version")), field_path=one(form, "field"), value=raw if raw != "" else None, actor=one(form, "actor"), reason=one(form, "reason"))
    elif action == "resolve_review":
        result = resolve_review(APP_CONFIG["db"], requisition_id=req_id, expected_version=int(one(form, "version")), actor=one(form, "actor"))
    elif action == "rebuild_route":
        result = rebuild_approval_route(APP_CONFIG["db"], requisition_id=req_id, expected_version=int(one(form, "version")), actor=one(form, "actor"), reason=one(form, "reason"))
    elif action == "assign_approver":
        result = assign_manual_approver(APP_CONFIG["db"], requisition_id=req_id, expected_version=int(one(form, "version")), sequence=int(one(form, "sequence")), employee_id=one(form, "employee_id"), actor=one(form, "actor"), reason=one(form, "reason"))
    elif action in {"approve", "reject"}:
        result = act_on_approval(APP_CONFIG["db"], requisition_id=req_id, expected_version=int(one(form, "version")), actor_employee_id=one(form, "actor_employee_id"), action=action, reason=one(form, "reason", required=False))
    elif action in {"confirm_duplicate", "dismiss_duplicate"}:
        result = resolve_link(APP_CONFIG["db"], link_id=int(one(form, "link_id")), resolution="confirmed" if action == "confirm_duplicate" else "dismissed", actor=one(form, "actor"), reason=one(form, "reason"))
    elif action == "submit_po":
        result = submit_purchase_order(APP_CONFIG["db"], requisition_id=req_id, expected_version=int(one(form, "version")), endpoint=one(form, "endpoint"), actor=one(form, "actor"))
    else:
        raise ValueError(f"Unknown action: {action}")
    return pretty(result), req_id


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        try:
            selected_id = int(query["id"][0]) if query.get("id") else None
            html = full_page(message=query.get("message", [None])[0], error=query.get("error", ["0"])[0] == "1", selected_id=selected_id)
            self._send(200, html)
        except HANDLED_ERRORS as exc:
            self._send(500, f"<h1>Could not render workflow</h1><pre>{h(exc)}</pre>")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/action":
            self.send_error(404); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("Form is too large")
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            message, req_id = handle_action(form)
            params = {"message": message}
            if req_id is not None:
                params["id"] = str(req_id)
        except HANDLED_ERRORS as exc:
            req_id = None
            try:
                req_id = int(form.get("requisition_id", [""])[0])
            except (ValueError, UnboundLocalError):
                pass
            params = {"message": str(exc), "error": "1"}
            if req_id is not None:
                params["id"] = str(req_id)
        self.send_response(303)
        self.send_header("Location", "/?" + urlencode(params))
        self.end_headers()

    def _send(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local procurement review UI")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--erp-endpoint", default=DEFAULT_ERP_ENDPOINT)
    args = parser.parse_args()
    APP_CONFIG.update(db=args.db.expanduser().resolve(), endpoint=args.erp_endpoint)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Review UI: http://{args.host}:{args.port}")
    print(f"Database:  {APP_CONFIG['db']}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
