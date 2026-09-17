#!/usr/bin/env python3
"""Optional read-only Gmail intake for ProjectOps Workflow.

The adapter retrieves one message as RFC 822 bytes and hands those bytes to the
same ingestion boundary used by local fixtures.  Google dependencies are loaded
only when live Gmail access is requested.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from projectops import IngestResult, ProjectOpsError, ingest_rfc822_bytes


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = [GMAIL_READONLY_SCOPE]


class GmailIntakeError(ProjectOpsError):
    """Raised when Gmail retrieval or authorization cannot safely continue."""


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str
    raw: bytes

    @property
    def source_path(self) -> str:
        return f"gmail://users/me/messages/{self.message_id}"


def _execute(request: Any) -> dict[str, Any]:
    try:
        result = request.execute()
    except Exception as exc:
        raise GmailIntakeError(
            f"Gmail API request failed ({type(exc).__name__}); no message was ingested"
        ) from exc
    if not isinstance(result, dict):
        raise GmailIntakeError("Gmail API returned an invalid response; no message was ingested")
    return result


def _select_message_id(service: Any, *, message_id: str | None, query: str | None) -> str:
    if bool(message_id) == bool(query):
        raise GmailIntakeError("Select exactly one Gmail message with message_id or query")
    if message_id:
        return message_id.strip()

    response = _execute(
        service.users()
        .messages()
        .list(userId="me", q=query.strip(), maxResults=2, includeSpamTrash=False)
    )
    matches = response.get("messages", [])
    if not isinstance(matches, list) or not matches:
        raise GmailIntakeError("Gmail query matched no messages")
    if len(matches) != 1:
        raise GmailIntakeError("Gmail query matched more than one message; refine the query")
    selected = matches[0]
    if not isinstance(selected, dict) or not str(selected.get("id", "")).strip():
        raise GmailIntakeError("Gmail query returned a message without an ID")
    return str(selected["id"]).strip()


def retrieve_gmail_message(
    service: Any,
    *,
    message_id: str | None = None,
    query: str | None = None,
) -> GmailMessage:
    """Retrieve exactly one message using Gmail read operations only."""
    selected_id = _select_message_id(service, message_id=message_id, query=query)
    response = _execute(
        service.users().messages().get(userId="me", id=selected_id, format="raw")
    )
    returned_id = str(response.get("id", "")).strip()
    encoded = response.get("raw")
    if returned_id != selected_id:
        raise GmailIntakeError("Gmail returned a different message than the selected ID")
    if not isinstance(encoded, str) or not encoded:
        raise GmailIntakeError("Gmail message did not include an RFC 822 raw payload")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            (encoded + padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise GmailIntakeError("Gmail message raw payload is not valid base64url") from exc
    return GmailMessage(
        message_id=returned_id,
        thread_id=str(response.get("threadId", "")).strip(),
        raw=raw,
    )


def ingest_gmail_message(
    db_path: str | Path,
    service: Any,
    *,
    message_id: str | None = None,
    query: str | None = None,
) -> IngestResult:
    """Retrieve one Gmail message and feed the unchanged domain workflow."""
    message = retrieve_gmail_message(service, message_id=message_id, query=query)
    return ingest_rfc822_bytes(
        db_path,
        message.raw,
        source_path=message.source_path,
        source_identity=f"gmail:{message.message_id}",
    )


def _save_token(credentials: Any, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write(credentials.to_json())
    token_path.chmod(0o600)


def build_gmail_service(credentials_path: str | Path, token_path: str | Path) -> Any:
    """Authorize a desktop test client with the exact read-only Gmail scope."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailIntakeError(
            "Gmail dependencies are not installed; install requirements-gmail.txt"
        ) from exc

    credentials_file = Path(credentials_path).expanduser()
    token_file = Path(token_path).expanduser()
    if not credentials_file.is_file():
        raise GmailIntakeError(f"OAuth client file not found: {credentials_file}")

    credentials = None
    if token_file.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)
        except (OSError, ValueError) as exc:
            raise GmailIntakeError("Cannot load the configured OAuth token file") from exc

    try:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            _save_token(credentials, token_file)
        elif not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), GMAIL_SCOPES)
            credentials = flow.run_local_server(port=0)
            _save_token(credentials, token_file)
    except Exception as exc:
        raise GmailIntakeError(
            f"Gmail OAuth authorization failed ({type(exc).__name__})"
        ) from exc

    if not credentials.has_scopes(GMAIL_SCOPES):
        raise GmailIntakeError("OAuth token does not grant the required Gmail read-only scope")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read one Gmail message into ProjectOps")
    parser.add_argument("--db", required=True, help="Existing ProjectOps SQLite database")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--message-id", help="Exact immutable Gmail message ID")
    selection.add_argument("--query", help="Gmail search query that must match exactly one message")
    parser.add_argument(
        "--credentials",
        default=os.environ.get("PROJECTOPS_GMAIL_CREDENTIALS"),
        help="OAuth desktop-client JSON path (or PROJECTOPS_GMAIL_CREDENTIALS)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PROJECTOPS_GMAIL_TOKEN"),
        help="OAuth token JSON path outside the repository (or PROJECTOPS_GMAIL_TOKEN)",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if not args.credentials or not args.token:
            raise GmailIntakeError("Both OAuth credentials and token paths are required")
        service = build_gmail_service(args.credentials, args.token)
        result = ingest_gmail_message(
            args.db,
            service,
            message_id=args.message_id,
            query=args.query,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except ProjectOpsError as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": str(exc), "mutation_performed": False},
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
