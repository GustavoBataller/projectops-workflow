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

Live integrations, runtime AI, authentication, multi-tenancy, hosting, and
production operations were deliberately excluded. They are materially different
product and security commitments, not prerequisites for testing this contract.

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
fixtures, a one-command demo, business-oriented documentation, and an MIT
license. It has no secret configuration or external runtime dependency.

## Acceptance evidence

The public acceptance command is:

```bash
python3 -m unittest discover -s tests -v
```

Seven focused tests exercise pending-state safety, correction then approval,
exactly-once application within the local SQLite boundary, rejection,
ambiguity/unmatched/unstructured failures, conflicting replay, invalid
corrections, duplicate ingestion, and audit reconstruction.

The clean end-to-end command is:

```bash
python3 demo.py
```

It creates an isolated temporary database, demonstrates the complete corrected
approval path, confirms that pending state does not mutate the project, checks a
duplicate replay, prints the resulting project, and prints the ordered audit
trail.

## Limits

This is a local working MVP/reference implementation. Its evidence does not
establish production readiness, distributed exactly-once delivery, tamper-proof
audit storage, authenticated authorization, external integration quality,
runtime AI capability, customer value, or client delivery.
