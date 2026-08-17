# Purchase Requisition Automation — Take-Home Challenge

This brief sets out the process you are automating, the rules that apply, what to
build and what to hand in. If something is still unclear once you are underway, email
us and ask. Questions during the work are normal and welcome.

Anything you would want for a production version, or that you think should happen
but is not asked for here, write up in a markdown file in the repository rather
than building it.

Estimated effort: about 4 hours.



## 1. The situation

Our customers are mid-sized industrial and manufacturing companies. Most of them
still run procurement through email, PDF attachments and ad-hoc spreadsheets.

Here is the customer in this challenge. When somebody in the company needs to buy
something, they email the purchasing department. A clerk reads the email, works out
what is actually being requested, copies the details into the ERP, and routes it to
the right managers for approval. Once approved, it becomes a purchase order.

Today one clerk does all of this by hand, for 100 to 200 requests a day. The company
is growing and wants to handle ten times that volume. Your job is to take the manual
work out of the middle.

**The one thing that must never happen:** the system presenting a guess as a fact. A
requisition that is wrong but looks correct is worse than one that is obviously
incomplete. When the system cannot establish something, it says so and a human
decides. Nothing is ever approved automatically.



## 2. What you will build

A small web application that:

1. takes in an email, with or without a PDF attachment,
2. uses an LLM to pull out the structured details of the request,
3. checks those details against the master data we give you,
4. works out who has to approve it, and in what order,
5. lets a human review, correct, approve or reject it,
6. and once it is fully approved, sends it to the purchase order endpoint we
   give you.

**Out of scope:** connecting to a real ERP, scanned PDFs and OCR, images, Word,
Excel, languages other than German and English, email delivery and notifications,
approver holiday cover, multi-tenancy, and real authentication. Mention any of
these in your README if you want to say how you would handle them, but do not
build them.



## 3. What you get

| File | What it is |
| --- | --- |
| `emails/*.eml` (48) | Real raw email files. Some have PDF attachments |
| `quotes/*.pdf` (21) | Supplier quotes and offers |
| `master_data.json` | The customer's reference data. See below |
| `mock_po_api.py` | A stand-in for the customer's ERP. See Section 8 |

### What is in the master data

| Block | Count | Notable fields |
| --- | --- | --- |
| `suppliers` | 25 | `id`, `name`, `vat_id`, `country`, `default_currency`, `status` |
| `cost_centers` | 15 | `code`, `name`, `owner_employee_id`, `department` |
| `departments` | 6 | `code`, `name`, `head_employee_id` |
| `employees` | 33 | `id`, `name`, `email`, `role`, sometimes `deputy_for` |
| `gl_accounts` | 12 | `code`, `name` |
| `approval_limits_chf` | 5 | `from`, `to`, `required_roles` |
| `_meta.fx_rates_to_chf` | 4 | `CHF: 1.0`, `EUR: 0.96`, `USD: 0.88`, `GBP: 1.13` |

This is your source of truth. Load it into a database. Reading the JSON file
directly at request time **does not** count as having a database.



## 4. The approval rules

### 4.1 Who has to approve

Approval requirements are driven by the total value of the requisition in CHF.

| Total value (CHF) | Required approvers                                         |
| ----------------- | ---------------------------------------------------------- |
| ≤ 1,000           | Cost Center Owner                                          |
| 1,001 – 10,000    | Cost Center Owner → Department Head                        |
| 10,001 – 50,000   | Cost Center Owner → Department Head → Finance              |
| 50,001 – 250,000  | Cost Center Owner → Department Head → Finance → CFO        |
| > 250,000         | Cost Center Owner → Department Head → Finance → CFO → CEO  |

Each band adds to the one before it. The same table is in
`master_data.json` under `approval_limits_chf`, use that rather than retyping it.

### 4.2 Who fills each role

| Role | Where it comes from |
| --- | --- |
| Cost Center Owner | the cost center's `owner_employee_id` |
| Department Head | the cost center's `department`, then that department's `head_employee_id` |
| Finance, CFO, CEO | company-wide, matched on `employees[].role` |

Finance has two people. **Daniela Huber (EMP-701) is the approver.** Thomas Egger
(EMP-702) carries `deputy_for: "EMP-701"`, which makes him her stand-in, and
stand-ins are out of scope. As a general rule, where a company-wide role has more
than one holder, the one without `deputy_for` is the approver.

### 4.3 Order, and people holding two roles

Approvals are **sequential**. The next approver is only asked once the previous
one has approved. A rejection stops the chain immediately.

If the same person fills two roles that follow each other in the chain, those two
steps collapse into one: they approve once and both count as done. Collapsing
applies to consecutive steps only. If the same person appears again later in the
chain with another approver in between, they approve at each of those steps.

### 4.4 A requester can never approve their own requisition

This holds at any seniority, including the CEO. When the person who raised the
request turns out to be a required approver:

1. that step passes to the next distinct role in the chain,
2. and if the chain offers no one else, the requisition is flagged for the clerk
   to assign an approver by hand.

It is never approved with a step missing, and it is never quietly skipped.

### 4.5 Currency

Approval limits are in CHF. Convert other currencies with
`_meta.fx_rates_to_chf` before deciding the band. Keep the original currency and
amount as well: the requisition is placed in the currency the supplier quoted.

In production these rates would come from a daily ECB feed. Mention that in your
README, but use the fixed table here.

### 4.6 Which amount counts

Documents state amounts in different ways. Some show a net total, some add VAT,
some show both.

**Use the net amount, excluding VAT, for the approval band and for the purchase
order total.** Record the VAT if the document states it, but do not include it in
the figure that decides who approves.



## 5. Reading the emails

### Formats you must handle

- an email whose body is plain text,
- an email whose body is HTML,
- an email with the request only in an attached PDF, where the body is one line
  such as "please order this",
- a forwarded supplier offer with internal comments added on top.

Bodies are in **German and English**. Do not build language detection into your
prompt as a hard branch, and do not assume one or the other.

### What to pull out

Requester, supplier, line items (description, quantity, unit price, unit),
currency, total, requested delivery date, cost center. Item codes where the
document gives them.

The figures on a document are not always internally consistent. A total may not
match the lines it is made of, and a model reading the document will happily
report both. Decide how much you trust each number, and make sure a requisition
never routes on a figure the document itself contradicts.

### One email, one request

Each email carries a single purchase request, so create one requisition from it. Several line items within that request are normal and belong to the same requisition.


### Repeats and corrections

A message that repeats or corrects an earlier request is linked to the original
and flagged as a suspected duplicate or correction. The clerk resolves it. Do not
silently create a second requisition, and do not silently overwrite the first.

If a rejected request is corrected and comes back, the approval chain **starts
again from the beginning.**



## 6. Matching against the master data

### Suppliers

Match on **VAT ID first**, because it is unambiguous. Fall back to matching on the
name, and treat a name-only match as less certain.

- A supplier you cannot find in the master data **stops automatic progression.**
  Show it to the clerk with the option to create it. Never invent a supplier
  record from what you extracted.
- A supplier whose `status` is `blocked` also stops progression. Nothing may be
  ordered from a blocked supplier. One supplier in the data is blocked.
- Where the name and the VAT ID point at two different suppliers, that is a
  conflict, and the clerk decides.

### Cost center

Take the cost center code from the email when it is stated. It determines both
the Cost Center Owner and, via its department, the Department Head, so without
one you cannot build a chain and the requisition goes to the clerk.

A code that looks right but is not in the master data is not a match. Do not fall
back to the closest-looking one.

### GL account

Optional. You may leave it empty. Suggesting one from the item description and
letting the clerk confirm it earns extra credit; guessing one and presenting it as
confirmed does not.

### Item codes, delivery dates, missing prices

- A **missing item code does not block anything.** Record it when the document
  gives you one.
- A **missing requested delivery date** does block the purchase order, because the
  ERP requires it. The clerk supplies it before the order is placed. Most emails
  do not state one, so expect this to be the normal case rather than the exception.
- A **line with no price** cannot be totalled, so it cannot be routed. That is a
  case for the clerk.

The rule behind all of these: if you cannot establish a value from the document or
the master data, leave it empty and say why. Do not fill it in with a guess.



## 7. The states a requisition moves through

At a minimum:

```
Inbox -> Extracted -> Needs review -> Pending approval -> Approved -> Ordered
                                                       -> Rejected
```

- **Needs review** is where anything uncertain waits for the clerk. A clean
  requisition skips it and goes straight to the first approver.
- **Pending approval** walks the chain from Section 4, one approver at a time.
- **Rejected** keeps a reason and stays in the system as a record.
- **Ordered** is reached by sending it to the endpoint in Section 8.

You need to show this flow in your app or as a diagram, and a given requisition's
current position in it.



## 8. After approval: the purchase order endpoint

`mock_po_api.py` stands in for the customer's ERP. It needs no installation and no
third-party packages.

```bash
python3 mock_po_api.py          # listens on 127.0.0.1:8080
```

| Call | What it does |
| --- | --- |
| `POST /purchase-orders` | Books an order. Returns `201` and the booked order |
| `GET /purchase-orders` | Every order booked so far |
| `GET /purchase-orders/PO-...` | One order, or `404` |
| `GET /health` | Liveness check |

Send it a requisition **only once the approval chain is complete.** The ERP assigns
the purchase order number, you never supply one.

**Rejected with `422`**, meaning the ERP cannot book it at all:
`supplier_id`, `currency`, `total`, `requested_delivery_date`, and on every line
`item_name`, `quantity`, `unit_price`, `unit`.

**Accepted with `201` plus `warnings`**, meaning it is booked but a human should
look: a missing `cost_centre` or `gl_account`, a missing `item_code` on a line, an
ID that is not in the master data, a date that is not `YYYY-MM-DD`, or a total
that does not equal the sum of the lines.



## 9. What you must deliver


| | Deliverable | What we look for |
| --- | --- | --- |
| D1 | A working web application | The clerk or approver can see incoming requisitions, see what was extracted, edit fields, approve or reject. It does not need to be pretty |
| D2 | A workflow overview | The flow from Section 7, as a diagram or a view in the app |
| D3 | At least one real LLM call | Extraction from an email or a PDF. We want to see the prompt, the output schema, the validation, and what happens when the model returns nonsense |
| D4 | A database | Suppliers, cost centers, approvers, limits. SQLite is fine. Reading the JSON file at request time is not |
| D5 | A video walkthrough, 5 to 10 minutes | A screen recording. Show one clean case and one messy case. Briefly cover how it is built and what you would do with another week |
| D6 | A README | How to run it, what you assumed, what you left out, what you would build next |



## 10. Ground rules

- Any programming language. We do not score the choice.
- Use of Claude Code, Cursor or similar AI coding tools is explicitly encouraged. We do this in our day-to-day.
- Use any libraries, frameworks or hosted services you want.
- You must understand every part of your solution. We will ask things like "why did you pick X over Y?", "what happens if the LLM returns garbage?", "how would you scale this to 500 requisitions a day?". "The AI wrote it" is not an acceptable answer.
- The data is synthetic, but treat it as if it were not. Keep personal data out of
  your logs, and think before sending it to a third-party service.
- Document the assumptions you made wherever the spec was ambiguous. A short README is fine.
- Commit your work to a Git repository. GitHub, GitLab, or a zip with a `.git`
  folder are all fine.



## 11. Timeline and submission

You have **7 calendar days** from receiving this. We do not track hours, and we do
not expect the whole week to be spent on it.

Email us with questions at any point.

To submit, send:

1. a link to your Git repository, public or private with access shared to us,
2. a link to your video.

Then a **30-minute** conversation: a short summary from you, a live walkthrough
where we hand your running system an email it has not seen before, a technical
discussion, and your questions for us. Have it running before the call starts, on
the machine you will be sharing.


Good luck. We are looking forward to seeing how you think.
