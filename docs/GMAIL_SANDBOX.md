# Gmail sandbox evidence and limits

## Boundary

`gmail_intake.py` is an opt-in adapter for one dedicated test/sandbox mailbox.
It uses only Gmail `users.messages.list` (when a query is supplied) and
`users.messages.get(format=raw)`. The exact OAuth scope is
`https://www.googleapis.com/auth/gmail.readonly`.

Gmail's narrower `gmail.metadata` scope cannot return message bodies or the raw
RFC 822 payload, so it cannot satisfy this adapter's ingestion contract. No
mailbox-mutating Gmail operation is implemented.

The Gmail immutable message ID becomes the external replay identity and is
retained in the `gmail://users/me/messages/<id>` source locator. The RFC 822
`Message-ID` and content hash remain preserved by the existing ingestion model.
Reusing the Gmail ID with different bytes fails closed.

## Unchanged authority model

Retrieval does not update a project. The raw message crosses the same boundary
as a local fixture:

`Gmail raw message -> exact project match -> deterministic proposal -> pending`

Only the existing explicit human-role `correct`, `reject`, or `approve`
transition can affect project state. Gmail authentication proves access only;
it is not approval or product acceptance. Extraction remains deterministic and
there is no live LLM.

## Automated evidence

The focused tests use a fake Gmail service that intentionally exposes only
`list` and `get`. They cover direct selection, query selection, raw RFC 822
conversion, pending/no-mutation behavior, explicit approval, duplicate replay,
conflicting replay, ambiguous/no query results, retrieval failure, and OAuth
failure. The original local acceptance suite remains credential-free.

These tests establish the adapter contract, not Google's live behavior, OAuth
verification, mailbox immutability at the provider, or production security.

## Real E2E evidence contract

A live sandbox run should use one synthetic email with a unique RFC 822
`Message-ID`, capture its Gmail message ID, ingest it, confirm a pending proposal
and unchanged project, perform an explicit human-role review, then inspect the
project and ordered audit trail. Compare Gmail metadata/labels before and after
as bounded evidence that the adapter did not mutate the mailbox.

No live Gmail result should be claimed unless the exact sandbox account,
message, execution time, code commit, and observed before/after provider state
were actually exercised. Credentials, tokens, account identifiers, message
content, and API responses must not be committed or included in public evidence.

## Security and policy limits

- OAuth client JSON and tokens are configured by paths and ignored by Git.
- Tokens are written with owner-only file permissions.
- The adapter requests no send/compose/modify/labels/settings scope.
- `gmail.readonly` is still a restricted scope with broad message visibility.
- This repository does not claim Google OAuth verification, restricted-scope
  security assessment, encrypted token storage, production operations, or
  suitability for personal or sensitive mail.
