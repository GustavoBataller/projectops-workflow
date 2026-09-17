# ProjectOps Workflow

Project updates often arrive as semi-structured email. Copying them into a
project system by hand is slow, but allowing automation to change project state
without review is risky.

ProjectOps Workflow is a local reference implementation of a safer pattern:
turn an incoming message into a structured proposal, require an explicit human
decision, and preserve an ordered audit trail of what happened.

> Automation may propose a change. It cannot approve its own work.

This repository contains a working MVP, not a production SaaS or
client-delivered product. Its default demo runs entirely on local synthetic data
and needs no secrets or third-party services. An optional Gmail sandbox adapter
can exercise one real read-only intake boundary.

## Solution

The workflow separates interpretation from authority:

1. Ingest an RFC 822 email from a local fixture or the optional Gmail adapter.
2. identify exactly one configured project.
3. extract a structured update deterministically.
4. store the proposal as `pending` without changing the project.
5. let a human correct, reject, or approve it.
6. apply an approved proposal once and record every material transition.

## Workflow

```mermaid
flowchart LR
    A[Incoming email] --> B[Exact project match]
    B --> C[Deterministic extraction]
    C --> D[Pending proposal]
    D -->|Correct| D
    D -->|Reject| E[No project mutation]
    D -->|Approve| F[Project updated once]
    B -. no or ambiguous match .-> G[Stop without mutation]
    C -. invalid structure .-> G
    A --> H[(Ordered audit trail)]
    C --> H
    D --> H
    F --> H
```

## Architectural decisions

- **Approval-first state changes.** Extraction and review are separate
  responsibilities; a proposal remains inert until approval.
- **Deterministic extraction.** The baseline parses labelled fields rather than
  calling an LLM, making the demo reproducible and secret-free.
- **Local, dependency-free runtime.** Python's standard library and SQLite are
  enough to exercise the default product contract. Gmail dependencies are
  separate and opt-in.
- **Stable identities and replay checks.** Message content and identifiers
  support duplicate detection and reject a reused `Message-ID` with different
  content.
- **Explicit failure over guessing.** No match, multiple matches, malformed
  updates, and invalid corrections stop before project mutation.

## Safety and reliability properties

The included acceptance suite checks the bounded local implementation for:

- no project mutation while a proposal is pending;
- safe correction and rejection;
- approval applied exactly once in the current SQLite persistence boundary;
- duplicate ingestion returning the existing proposal;
- fail-closed ambiguous, unmatched, unstructured, and conflicting messages;
- correction-schema validation; and
- reconstructable ordered audit events.

These are local application guarantees, not distributed exactly-once delivery,
tamper-proof logging, authentication, or production security claims.

## Demo

Requirements: Python 3.10 or newer. No package installation is needed.

Run the complete happy path from a clean temporary database:

```bash
python3 demo.py
```

The demo shows the initial project, a pending proposal with no mutation, a human
correction, approval, the resulting project, duplicate handling, and the audit
sequence. The temporary database is removed when the run finishes.

For individual CLI commands:

```bash
DB=/tmp/projectops-workflow.sqlite3
rm -f "$DB"

python3 projectops.py init --db "$DB" --projects sample_data/projects.json
python3 projectops.py ingest --db "$DB" \
  --source sample_data/inbox/acme_weekly_update.eml
python3 projectops.py proposals --db "$DB" --state pending
```

Use the emitted `proposal_id` with `projectops.py review --help` to correct,
reject, or approve the proposal. The automated demo is the shortest walkthrough.

## Optional Gmail sandbox intake

The optional adapter retrieves exactly one Gmail message with the
`https://www.googleapis.com/auth/gmail.readonly` scope and asks Gmail for its
raw RFC 822 representation. The message then enters the same deterministic
proposal/review/approval path as a local fixture. The adapter has no send,
modify, label, trash, archive, or read/unread operation.

Use a dedicated test/sandbox Gmail account and synthetic email only. Follow
Google's current desktop-app OAuth setup: enable the Gmail API in a Google Cloud
project, configure the OAuth consent screen/test audience, create a Desktop app
OAuth client, and keep its downloaded JSON outside this repository. Then:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-gmail.txt

DB=/tmp/projectops-gmail-sandbox.sqlite3
python3 projectops.py init --db "$DB" --projects sample_data/projects.json
.venv/bin/python gmail_intake.py --db "$DB" \
  --query 'rfc822msgid:<projectops-sandbox-001@example.test>' \
  --credentials /path/outside/repository/oauth-client.json \
  --token /path/outside/repository/projectops-gmail-token.json
```

The query must match exactly one message; alternatively, pass an exact immutable
Gmail message ID with `--message-id`. On first use, Google's installed-app flow
opens a browser for account selection and consent. The token path is explicit,
is written with owner-only permissions, and must remain outside Git. The local
fixture demo and tests never load Gmail credentials or contact Google.

`gmail.readonly` is a Google restricted scope because it exposes message
content. This adapter is intentionally a sandbox demonstration, not a claim of
OAuth verification, production data handling, or readiness for public users.
See [Gmail sandbox evidence and limits](docs/GMAIL_SANDBOX.md).

## Tests and evidence

Run the focused acceptance suite:

```bash
python3 -m unittest discover -s tests -v
```

The suite contains the seven original workflow acceptance tests plus focused
Gmail adapter tests. Gmail tests use deterministic fakes and do not require a
live mailbox.
See the concise [case study](docs/CASE_STUDY.md) for scope, tradeoffs, and the
public-repository acceptance record.

## Limitations

- Gmail support is an optional read-only sandbox adapter, not a production inbox
  integration. The default remains local fixtures.
- Deterministic labelled-field extraction only; there is no live LLM/provider.
- Human identity is an actor string, not an authenticated user or authorization
  system.
- SQLite audit records are inspectable but not tamper-proof.
- No hosting, multi-tenancy, RBAC/SSO, queues, external CRM/project-system
  adapter, or production operations layer.
- No external user, buyer, customer outcome, or production environment has been
  validated.

## Technology

Python standard library, SQLite, RFC 822 email parsing, JSON fixtures, and
`unittest`. The implementation intentionally has no runtime dependency on an AI
provider, workflow platform, or external service.

## License

[MIT](LICENSE) © 2026 Gustavo Bataller.
