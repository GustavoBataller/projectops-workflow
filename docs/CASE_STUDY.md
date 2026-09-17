# Case study: approval-first project updates

## Problem

Operational updates arrive in email, while the authoritative project record
lives elsewhere. Manual transfer is repetitive and error-prone; fully automatic
mutation makes a wrong project match or extraction immediately consequential.

## Requirements and scope

The MVP needed to demonstrate one bounded contract: ingest a local message,
identify one project, propose structured changes, keep the project unchanged
until an explicit human decision, apply an approved update once, and retain an
ordered audit trail. Synthetic data and a local runtime keep the evidence
reproducible and safe to publish.

The reproducible baseline excludes runtime AI, ProjectOps authentication,
multi-tenancy, hosting, and production operations. One optional Gmail
test-account adapter now exercises a real read-only intake seam without changing
the downstream authority model; it is not a production Gmail integration.

## Architecture and tradeoffs

One Python module contains the domain workflow and CLI. SQLite holds projects,
messages, proposals, and audit events. RFC 822 and JSON fixtures represent the
external boundaries.

The central tradeoff favors an explicit review boundary over unattended speed.
Deterministic extraction is less flexible than an LLM, but is transparent,
repeatable, and sufficient to test the harder control question: who may turn a
proposal into project state.

## Implementation and delivery

Messages receive stable identifiers, duplicate content resolves to the existing
proposal, and conflicting content under a reused message identifier is refused.
Project matching must produce exactly one result. Proposals have explicit
`pending`, `rejected`, and `applied` states; correction increments the proposal
version while leaving it pending.

The public artifact includes the implementation, focused tests, synthetic
fixtures, a one-command local demo, business-oriented documentation, and an MIT
license. The local path has no secret configuration or external runtime
dependency; Gmail dependencies and credentials are separate and opt-in.

## Acceptance evidence

The public acceptance command is:

```bash
python3 -m unittest discover -s tests -v
```

The seven original focused tests exercise pending-state safety, correction then approval,
exactly-once application within the local SQLite boundary, rejection,
ambiguity/unmatched/unstructured failures, conflicting replay, invalid
corrections, duplicate ingestion, and audit reconstruction.

Additional deterministic tests exercise Gmail raw-message conversion, the
read-only request surface, duplicate/conflict behavior, query ambiguity, and
failure-without-mutation. They do not depend on a live Google account.

The clean end-to-end command is:

```bash
python3 demo.py
```

It creates an isolated temporary database, demonstrates the complete corrected
approval path, confirms that pending state does not mutate the project, checks a
duplicate replay, prints the resulting project, and prints the ordered audit
trail.

## Limits

This is a working MVP/reference implementation with an optional Gmail sandbox
intake. Its evidence does not
establish production readiness, distributed exactly-once delivery, tamper-proof
audit storage, OAuth verification, production external-integration quality,
runtime AI capability, customer value, or client delivery.
