# Purchase requisition automation

This project turns German or English purchase-request emails and text-based PDF
quotes into reviewable requisitions, resolves master data and approval chains,
and sends fully approved requests to the supplied mock purchase-order API.

The implemented flow is:

```text
Email/PDF
  -> ingestion and PDF text extraction
  -> structured LLM fact extraction
  -> deterministic normalization and total reconciliation
  -> supplier/requester/cost-center matching
  -> CHF approval-route construction
  -> clerk review and sequential approval
  -> idempotent mock ERP submission
```

SQLite is used for ingested source packets, processing runs, master data,
workflow revisions, approval events, duplicate candidates, and PO submissions.

## Repository layout

```text
emails/                     48 supplied .eml files
quotes/                     supplied text-based PDF documents
src/ingestion.py            email parsing and PDF text extraction
src/llm_extraction.py       OpenAI structured extraction and validation
src/normalization.py        deterministic number/date/unit normalization
src/master_data_matching.py master-data synchronization and matching
src/approval_chain.py       CHF conversion and approval-route construction
src/duplicate_detection.py  repeat/correction candidate detection
src/workflow_service.py     workflow commands and state transitions
src/web_app.py              local clerk/approver web UI
src/erp_integration.py      idempotent PO submission
demo/procurements.db        populated demo database
master_data.json            supplied reference data
mock_po_api.py              supplied mock ERP
```

## Requirements

- Conda
- Python 3.12 recommended
- An OpenAI API key for fresh LLM extraction

The application uses one Conda environment. From the project root in Bash:

```bash
conda create -n blp-procurement python=3.12 -y
conda activate blp-procurement
python -m pip install -r requirements.txt
```

If `conda activate` is unavailable in Bash, run `conda init bash`, close and
reopen the terminal, and then activate the environment.

## Quick start with the populated demo database

The committed database under `demo/` contains the supplied emails, stored LLM
extractions, normalized records, master-data matches, routes, and workflow
records. Copy it to a local runtime database so using the UI does not modify the
committed snapshot:

```bash
cp -f demo/procurements.db procurements.db
```

Start the mock ERP in terminal 1:

```bash
conda activate blp-procurement
python mock_po_api.py
```

Start the web application in terminal 2:

```bash
conda activate blp-procurement
python src/web_app.py --db procurements.db
```

Open <http://127.0.0.1:8000>. The mock ERP is available at
<http://127.0.0.1:8080/purchase-orders>.

Requisition 1 is a useful clean-path demonstration. Its approval route contains
two sequential approvers. After both approvals, the UI enables the purchase
order action and records the ERP-assigned PO number.

## Build a fresh database from the supplied files

The following Bash commands create a separate database named
`fresh_procurements.db`. They do not depend on the populated demo database.

### 1. Configure the API key

For only the current Bash session:

```bash
export OPENAI_API_KEY="your-api-key"
```

To store it in the active Conda environment instead:

```bash
conda env config vars set OPENAI_API_KEY="your-api-key"
conda deactivate
conda activate blp-procurement
```

Never commit the API key. `OPENAI_MODEL` can optionally override the default
model used by `src/llm_extraction.py`.

### 2. Ingest all emails and referenced PDFs

```bash
DB="fresh_procurements.db"
python src/ingestion.py emails --sidecar-dir quotes --db "$DB"
```

Ingestion stores parsed email headers and bodies, attachment hashes, extracted
PDF text with page boundaries, warnings, and provenance. Scanned PDFs and OCR
are intentionally out of scope for this challenge.

### 3. Run structured LLM extraction for every stored email

First inspect the stored email IDs:

```bash
python src/llm_extraction.py --db "$DB" --list-emails
```

Then extract every email:

```bash
EMAIL_IDS=$(
  python src/llm_extraction.py --db "$DB" --list-emails |
  python -c 'import json, sys; data = json.load(sys.stdin); print(" ".join(str(email["id"]) for email in data["emails"]))'
)

for EMAIL_ID in $EMAIL_IDS; do
  if ! python src/llm_extraction.py --db "$DB" --email-id "$EMAIL_ID"; then
    echo "Warning: extraction failed for email ID $EMAIL_ID; it can be retried later." >&2
  fi
done
```

Each attempt is stored as a separate extraction run. Missing API credentials,
rate limits, schema failures, and other model errors are recorded as failures or
`needs_review`; they do not become silently trusted requisitions.

To inspect the prompt and source packet without making an API call:

```bash
python src/llm_extraction.py --db "$DB" --email-id 1 --dry-run
```

To inspect the response schema:

```bash
python src/llm_extraction.py --print-schema
```

### 4. Normalize and reconcile commercial values

```bash
python src/normalization.py --db "$DB" --all
```

This stage converts supported localized numeric/date/unit forms, preserves
quoted price bases, separates net/tax/gross values, and refuses to confirm a net
total when the source figures cannot be reconciled safely.

### 5. Load master data and match all requests

```bash
python src/master_data_matching.py --db "$DB" --master-data master_data.json --sync-only
python src/master_data_matching.py --db "$DB" --all
```

Supplier VAT is matched before supplier name. Name-only matches are lower
assurance. Unknown/blocked suppliers, missing cost centers, requester failures,
and identity conflicts stop automatic progression.

### 6. Construct approval routes

```bash
python src/approval_chain.py --db "$DB" --all
```

The route builder uses the approval limits and fixed CHF exchange rates stored
in the database. Approvals are sequential, consecutive roles held by one person
are collapsed, deputies are excluded, and requester self-approval is forbidden.

### 7. Detect repeats/corrections and initialize workflows

Duplicate/correction candidates are generated before workflow activation so a
later suspected repeat cannot silently become a second order:

```bash
python src/duplicate_detection.py --db "$DB" scan
python src/workflow_service.py --db "$DB" init --all
```

### 8. Start the application

In terminal 1:

```bash
python mock_po_api.py
```

In terminal 2:

```bash
DB="fresh_procurements.db"
python src/web_app.py --db "$DB"
```

Open <http://127.0.0.1:8000>.

## Workflow behavior

The UI displays the required state overview:

```text
Inbox -> Extracted -> Needs review -> Pending approval -> Approved -> Ordered
                                                       -> Rejected
```

- Clean requests can enter the first approval step directly.
- Uncertain or incomplete requests wait in `needs_review`.
- Material clerk edits create revisions and invalidate prior approvals.
- Changes to amount, currency, requester, or cost center require route rebuild.
- Only the current active approver can act; rejection stops the chain.
- The ERP submission is available only after the complete chain is approved.
- A durable idempotency key prevents a lost-response retry from creating a
  second purchase order for the same requisition version.

Useful CLI inspection commands:

```bash
DB="fresh_procurements.db"
python src/workflow_service.py --db "$DB" list
python src/workflow_service.py --db "$DB" show --id 1
python src/workflow_service.py --db "$DB" validate --id 1
python src/duplicate_detection.py --db "$DB" list --resolution pending
python src/erp_integration.py --db "$DB" status --id 1
```

## LLM safety boundary

The LLM extracts source-shaped facts only. It does not choose supplier IDs,
employees, cost centers, approvers, workflow states, GL accounts, or approval
bands. Those decisions remain deterministic and database-backed.

The prompt treats email and PDF content as untrusted data, preserves ambiguity,
requires source evidence for non-null values, separates net/tax/gross totals,
and sends schema or semantic failures to review. The prompt, Pydantic response
schema, candidate validation, request metadata, and failure persistence are in
`src/llm_extraction.py`.

Fresh extraction sends the minimized email/PDF text packet to OpenAI. In a real
deployment this requires an approved data-processing agreement, retention
policy, access controls, and an appropriate provider data-retention setting.

## Assumptions and known limitations

- Each supplied email contains at most one purchase request; several lines
  belong to the same requisition.
- PDFs are text PDFs. OCR, images, Word, Excel, real email delivery,
  notifications, holiday cover, multi-tenancy, and real authentication are out
  of scope in the challenge.
- The UI is a local demonstration and has no authentication or authorization.
  Production actors would come from SSO/RBAC and all state-changing requests
  would use CSRF protection.
- Fixed CHF rates come from the supplied master data. Production would load
  current daily rates from an approved feed such as the ECB.
- Duplicate/correction detection creates human-review candidates. A production
  version should add a field-level correction merge into the original request.
- The current clerk editor changes existing scalar and line fields; a fuller
  version should add line insertion/removal and a supplier-creation workflow.
- Existing workflow projections are not automatically replaced by later
  extraction/matching reruns. Production reconciliation should show a diff and
  require review before invalidating human work.
- SQLite is suitable for this local challenge. At higher volume, PostgreSQL,
  durable workers, per-requisition locking, migrations, structured PII-safe
  observability, and encrypted attachment retention would be appropriate.

## Suggested 5–10 minute walkthrough

1. Show the architecture above and the SQLite tables.
2. Open requisition 1 and explain its normalized values and approval route.
3. Approve the two sequential steps and create a PO in the mock ERP.
4. Open a messy request and demonstrate validation, a clerk edit, or duplicate
   evidence.
5. Show the LLM prompt/schema and explain how nonsense output is sent to review.
6. Close with the known limitations and production follow-ups.
