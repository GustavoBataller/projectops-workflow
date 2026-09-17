#!/usr/bin/env python3
"""ProjectOps AI: a local, approval-first project update workflow.

The first delivery intentionally uses a deterministic extraction adapter.  The
approval, mutation, idempotency and audit boundaries remain the same if a later
delivery substitutes an AI extraction provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Callable, Iterable


PROJECT_FIELDS = ("summary", "status", "milestone", "risks", "next_actions")
LIST_FIELDS = {"risks", "next_actions"}


class ProjectOpsError(RuntimeError):
    """Base class for safe, user-visible workflow failures."""


class ProjectMatchError(ProjectOpsError):
    """Raised when the message cannot be linked to exactly one project."""


class ReviewError(ProjectOpsError):
    """Raised when a proposal review transition is invalid."""


@dataclass(frozen=True)
class IngestResult:
    status: str
    message_id: str
    proposal_id: str
    project_id: str
    proposal_state: str
    proposal: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message_id": self.message_id,
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "proposal_state": self.proposal_state,
            "proposal": self.proposal,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            status TEXT NOT NULL,
            milestone TEXT NOT NULL,
            risks_json TEXT NOT NULL,
            next_actions_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_message_id TEXT,
            subject TEXT NOT NULL,
            sender TEXT NOT NULL,
            body TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS message_sources (
            source_identity TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(message_id),
            content_hash TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proposals (
            proposal_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            state TEXT NOT NULL CHECK (state IN ('pending', 'rejected', 'applied')),
            version INTEGER NOT NULL,
            proposal_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            decided_by TEXT,
            decision_reason TEXT,
            applied_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )


def audit(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: str,
    details: dict[str, Any],
    created_at: str | None = None,
) -> None:
    timestamp = created_at or utc_now()
    seed = f"{entity_type}\n{entity_id}\n{action}\n{timestamp}\n{json_text(details)}"
    connection.execute(
        """
        INSERT INTO audit_events
            (event_id, entity_type, entity_id, action, actor, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (stable_id("evt", seed), entity_type, entity_id, action, actor, json_text(details), timestamp),
    )


def load_json_file(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectOpsError(f"Cannot load JSON file {path}: {exc}") from exc


def initialize_database(db_path: str | Path, projects_path: str | Path) -> dict[str, Any]:
    projects = load_json_file(projects_path)
    if not isinstance(projects, list) or not projects:
        raise ProjectOpsError("Project fixture must be a non-empty JSON array")

    connection = connect(db_path)
    try:
        create_schema(connection)
        initialized: list[str] = []
        with connection:
            for index, project in enumerate(projects):
                if not isinstance(project, dict):
                    raise ProjectOpsError(f"Project at index {index} must be an object")
                project_id = str(project.get("project_id", "")).strip()
                name = str(project.get("name", "")).strip()
                if not project_id or not name:
                    raise ProjectOpsError(f"Project at index {index} requires project_id and name")
                aliases = [str(item).strip() for item in project.get("aliases", []) if str(item).strip()]
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO projects
                        (project_id, name, aliases_json, summary, status, milestone,
                         risks_json, next_actions_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        name = excluded.name,
                        aliases_json = excluded.aliases_json,
                        summary = excluded.summary,
                        status = excluded.status,
                        milestone = excluded.milestone,
                        risks_json = excluded.risks_json,
                        next_actions_json = excluded.next_actions_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        project_id,
                        name,
                        json_text(aliases),
                        str(project.get("summary", "")),
                        str(project.get("status", "unknown")),
                        str(project.get("milestone", "")),
                        json_text(project.get("risks", [])),
                        json_text(project.get("next_actions", [])),
                        now,
                    ),
                )
                audit(
                    connection,
                    entity_type="project",
                    entity_id=project_id,
                    action="project.initialized",
                    actor="system",
                    details={"name": name, "source": str(projects_path)},
                    created_at=now,
                )
                initialized.append(project_id)
        return {"status": "initialized", "database": str(db_path), "projects": initialized}
    finally:
        connection.close()


def message_body(message: Any) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
                return str(part.get_content()).strip()
        return ""
    return str(message.get_content()).strip()


def parse_message_bytes(
    raw: bytes,
    *,
    source_path: str,
    source_identity: str | None = None,
) -> dict[str, Any]:
    """Normalize RFC 822 bytes at the shared ingestion boundary.

    ``source_identity`` is a provider-owned immutable identifier when one is
    available (for example, a Gmail message ID).  It participates in replay and
    conflict detection without changing the downstream workflow.
    """
    message = BytesParser(policy=policy.default).parsebytes(raw)
    content_hash = hashlib.sha256(raw).hexdigest()
    source_message_id = str(message.get("Message-ID", "")).strip()
    message_id = stable_id("msg", source_identity or source_message_id or content_hash)
    return {
        "message_id": message_id,
        "source_identity": source_identity,
        "source_message_id": source_message_id,
        "source_path": source_path,
        "subject": str(message.get("Subject", "")).strip(),
        "sender": str(message.get("From", "")).strip(),
        "body": message_body(message),
        "content_hash": content_hash,
    }


def parse_message(source_path: str | Path) -> dict[str, Any]:
    path = Path(source_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProjectOpsError(f"Cannot read message {path}: {exc}") from exc
    return parse_message_bytes(raw, source_path=str(path))


def identify_project(connection: sqlite3.Connection, text: str) -> str:
    haystack = text.casefold()
    matches: set[str] = set()
    for row in connection.execute("SELECT project_id, name, aliases_json FROM projects"):
        references = [row["project_id"], row["name"], *json.loads(row["aliases_json"])]
        if any(reference.casefold() in haystack for reference in references if reference.strip()):
            matches.add(row["project_id"])
    if not matches:
        raise ProjectMatchError("No project reference or configured alias matched the message")
    if len(matches) > 1:
        raise ProjectMatchError(f"Ambiguous project match: {', '.join(sorted(matches))}")
    return next(iter(matches))


def split_items(value: str) -> list[str]:
    cleaned = value.strip()
    if not cleaned or cleaned.casefold() in {"none", "n/a", "no risks"}:
        return []
    return [item.strip(" -") for item in cleaned.replace("|", ";").split(";") if item.strip(" -")]


def extract_update(subject: str, body: str) -> dict[str, Any]:
    labels = {
        "summary": "summary",
        "status": "status",
        "milestone": "milestone",
        "risks": "risks",
        "risk": "risks",
        "next actions": "next_actions",
        "next action": "next_actions",
    }
    proposal: dict[str, Any] = {}
    label_pattern = re.compile(
        r"(?<!\S)(summary|status|milestone|risks?|next actions?):\s*",
        re.IGNORECASE,
    )
    matches = list(label_pattern.finditer(body))
    for index, match in enumerate(matches):
        field = labels[match.group(1).casefold()]
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = " ".join(body[match.end() : end].split())
        proposal[field] = split_items(value) if field in LIST_FIELDS else value

    if not proposal.get("summary"):
        proposal["summary"] = subject.strip() or next(
            (line.strip() for line in body.splitlines() if line.strip()), "Project update"
        )
    if not any(field in proposal for field in ("status", "milestone", "risks", "next_actions")):
        raise ProjectOpsError("Message contains no structured update fields beyond the summary")
    return {field: proposal[field] for field in PROJECT_FIELDS if field in proposal}


def proposal_row(connection: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT proposals.*, messages.content_hash
        FROM proposals
        JOIN messages ON messages.message_id = proposals.message_id
        WHERE proposals.message_id = ?
        """,
        (message_id,),
    ).fetchone()


def ingest_parsed_message(db_path: str | Path, parsed: dict[str, Any]) -> IngestResult:
    connection = connect(db_path)
    try:
        create_schema(connection)
        source_identity = parsed.get("source_identity")
        if source_identity:
            source = connection.execute(
                "SELECT * FROM message_sources WHERE source_identity = ?",
                (source_identity,),
            ).fetchone()
            if source is not None:
                if source["content_hash"] != parsed["content_hash"]:
                    raise ProjectOpsError(
                        "Source message identity was already ingested with different content; "
                        "refusing ambiguous replay"
                    )
                existing = proposal_row(connection, source["message_id"])
                if existing is None:
                    raise ProjectOpsError("Source identity points to an incomplete ingestion")
                return IngestResult(
                    status="duplicate",
                    message_id=existing["message_id"],
                    proposal_id=existing["proposal_id"],
                    project_id=existing["project_id"],
                    proposal_state=existing["state"],
                    proposal=json.loads(existing["proposal_json"]),
                )

        existing = proposal_row(connection, parsed["message_id"])
        if existing is not None and existing["content_hash"] != parsed["content_hash"]:
            raise ProjectOpsError(
                "Message-ID was already ingested with different content; refusing ambiguous replay"
            )
        if existing is None:
            existing = connection.execute(
                """
                SELECT proposals.*, messages.content_hash FROM proposals
                JOIN messages ON messages.message_id = proposals.message_id
                WHERE messages.content_hash = ?
                """,
                (parsed["content_hash"],),
            ).fetchone()
        if existing is not None:
            if source_identity:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO message_sources (source_identity, message_id, content_hash)
                        VALUES (?, ?, ?)
                        """,
                        (source_identity, existing["message_id"], parsed["content_hash"]),
                    )
            return IngestResult(
                status="duplicate",
                message_id=existing["message_id"],
                proposal_id=existing["proposal_id"],
                project_id=existing["project_id"],
                proposal_state=existing["state"],
                proposal=json.loads(existing["proposal_json"]),
            )

        project_id = identify_project(connection, f"{parsed['subject']}\n{parsed['body']}")
        proposal = extract_update(parsed["subject"], parsed["body"])
        proposal_id = stable_id("prp", f"{parsed['message_id']}\n{project_id}")
        now = utc_now()
        with connection:
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, source_path, source_message_id, subject, sender, body,
                     content_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parsed["message_id"],
                    parsed["source_path"],
                    parsed["source_message_id"],
                    parsed["subject"],
                    parsed["sender"],
                    parsed["body"],
                    parsed["content_hash"],
                    now,
                ),
            )
            if source_identity:
                connection.execute(
                    """
                    INSERT INTO message_sources (source_identity, message_id, content_hash)
                    VALUES (?, ?, ?)
                    """,
                    (source_identity, parsed["message_id"], parsed["content_hash"]),
                )
            connection.execute(
                """
                INSERT INTO proposals
                    (proposal_id, message_id, project_id, state, version,
                     proposal_json, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', 1, ?, ?, ?)
                """,
                (proposal_id, parsed["message_id"], project_id, json_text(proposal), now, now),
            )
            audit(
                connection,
                entity_type="message",
                entity_id=parsed["message_id"],
                action="message.ingested",
                actor="system",
                details={"project_id": project_id, "source_path": parsed["source_path"]},
                created_at=now,
            )
            audit(
                connection,
                entity_type="proposal",
                entity_id=proposal_id,
                action="proposal.created",
                actor="deterministic-extractor",
                details={"project_id": project_id, "proposal": proposal, "version": 1},
                created_at=now,
            )
        return IngestResult(
            status="created",
            message_id=parsed["message_id"],
            proposal_id=proposal_id,
            project_id=project_id,
            proposal_state="pending",
            proposal=proposal,
        )
    finally:
        connection.close()


def ingest_message(db_path: str | Path, source_path: str | Path) -> IngestResult:
    """Ingest an RFC 822 message from the existing local-file boundary."""
    return ingest_parsed_message(db_path, parse_message(source_path))


def ingest_rfc822_bytes(
    db_path: str | Path,
    raw: bytes,
    *,
    source_path: str,
    source_identity: str | None = None,
) -> IngestResult:
    """Ingest externally retrieved RFC 822 bytes through the same workflow."""
    parsed = parse_message_bytes(
        raw,
        source_path=source_path,
        source_identity=source_identity,
    )
    return ingest_parsed_message(db_path, parsed)


def validate_correction(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ReviewError("Correction must be a non-empty JSON object")
    unknown = sorted(set(value) - set(PROJECT_FIELDS))
    if unknown:
        raise ReviewError(f"Unsupported correction fields: {', '.join(unknown)}")
    normalized: dict[str, Any] = {}
    for field, field_value in value.items():
        if field in LIST_FIELDS:
            if not isinstance(field_value, list) or not all(isinstance(item, str) for item in field_value):
                raise ReviewError(f"Correction field {field} must be an array of strings")
            normalized[field] = [item.strip() for item in field_value if item.strip()]
        elif not isinstance(field_value, str) or not field_value.strip():
            raise ReviewError(f"Correction field {field} must be a non-empty string")
        else:
            normalized[field] = field_value.strip()
    return normalized


def review_proposal(
    db_path: str | Path,
    proposal_id: str,
    decision: str,
    actor: str,
    *,
    reason: str = "",
    correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if decision not in {"approve", "reject", "correct"}:
        raise ReviewError("Decision must be approve, reject or correct")
    if not actor.strip():
        raise ReviewError("A review actor is required")

    connection = connect(db_path)
    try:
        row = connection.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ReviewError(f"Unknown proposal: {proposal_id}")
        if row["state"] != "pending":
            raise ReviewError(f"Proposal {proposal_id} is already {row['state']}")

        proposal = json.loads(row["proposal_json"])
        now = utc_now()
        with connection:
            if decision == "correct":
                normalized = validate_correction(correction)
                proposal.update(normalized)
                version = int(row["version"]) + 1
                connection.execute(
                    """
                    UPDATE proposals
                    SET proposal_json = ?, version = ?, updated_at = ?, decided_by = ?,
                        decision_reason = ?
                    WHERE proposal_id = ?
                    """,
                    (json_text(proposal), version, now, actor, reason, proposal_id),
                )
                audit(
                    connection,
                    entity_type="proposal",
                    entity_id=proposal_id,
                    action="proposal.corrected",
                    actor=actor,
                    details={"correction": normalized, "reason": reason, "version": version},
                    created_at=now,
                )
                return {
                    "status": "corrected",
                    "proposal_id": proposal_id,
                    "state": "pending",
                    "version": version,
                    "proposal": proposal,
                }

            if decision == "reject":
                connection.execute(
                    """
                    UPDATE proposals
                    SET state = 'rejected', updated_at = ?, decided_by = ?, decision_reason = ?
                    WHERE proposal_id = ?
                    """,
                    (now, actor, reason, proposal_id),
                )
                audit(
                    connection,
                    entity_type="proposal",
                    entity_id=proposal_id,
                    action="proposal.rejected",
                    actor=actor,
                    details={"reason": reason, "version": row["version"]},
                    created_at=now,
                )
                return {"status": "rejected", "proposal_id": proposal_id, "state": "rejected"}

            project = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (row["project_id"],)
            ).fetchone()
            if project is None:
                raise ReviewError(f"Proposal project no longer exists: {row['project_id']}")
            updated = {
                "summary": project["summary"],
                "status": project["status"],
                "milestone": project["milestone"],
                "risks": json.loads(project["risks_json"]),
                "next_actions": json.loads(project["next_actions_json"]),
            }
            updated.update(proposal)
            connection.execute(
                """
                UPDATE projects
                SET summary = ?, status = ?, milestone = ?, risks_json = ?,
                    next_actions_json = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (
                    updated["summary"],
                    updated["status"],
                    updated["milestone"],
                    json_text(updated["risks"]),
                    json_text(updated["next_actions"]),
                    now,
                    row["project_id"],
                ),
            )
            connection.execute(
                """
                UPDATE proposals
                SET state = 'applied', updated_at = ?, decided_by = ?,
                    decision_reason = ?, applied_at = ?
                WHERE proposal_id = ?
                """,
                (now, actor, reason, now, proposal_id),
            )
            audit(
                connection,
                entity_type="proposal",
                entity_id=proposal_id,
                action="proposal.approved",
                actor=actor,
                details={"project_id": row["project_id"], "reason": reason, "version": row["version"]},
                created_at=now,
            )
            audit(
                connection,
                entity_type="project",
                entity_id=row["project_id"],
                action="project.updated",
                actor=actor,
                details={"proposal_id": proposal_id, "changes": proposal},
                created_at=now,
            )
            return {
                "status": "applied",
                "proposal_id": proposal_id,
                "state": "applied",
                "project_id": row["project_id"],
                "project": updated,
            }
    finally:
        connection.close()


def decode_project(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "project_id": row["project_id"],
        "name": row["name"],
        "aliases": json.loads(row["aliases_json"]),
        "summary": row["summary"],
        "status": row["status"],
        "milestone": row["milestone"],
        "risks": json.loads(row["risks_json"]),
        "next_actions": json.loads(row["next_actions_json"]),
        "updated_at": row["updated_at"],
    }


def get_project(db_path: str | Path, project_id: str) -> dict[str, Any]:
    connection = connect(db_path)
    try:
        row = connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        if row is None:
            raise ProjectOpsError(f"Unknown project: {project_id}")
        return decode_project(row)
    finally:
        connection.close()


def list_proposals(db_path: str | Path, state: str | None = None) -> list[dict[str, Any]]:
    connection = connect(db_path)
    try:
        query = "SELECT * FROM proposals"
        parameters: tuple[Any, ...] = ()
        if state:
            query += " WHERE state = ?"
            parameters = (state,)
        query += " ORDER BY created_at, proposal_id"
        return [
            {
                "proposal_id": row["proposal_id"],
                "message_id": row["message_id"],
                "project_id": row["project_id"],
                "state": row["state"],
                "version": row["version"],
                "proposal": json.loads(row["proposal_json"]),
                "decided_by": row["decided_by"],
                "decision_reason": row["decision_reason"],
            }
            for row in connection.execute(query, parameters)
        ]
    finally:
        connection.close()


def list_audit_events(db_path: str | Path) -> list[dict[str, Any]]:
    connection = connect(db_path)
    try:
        return [
            {
                "sequence": row["sequence"],
                "event_id": row["event_id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "action": row["action"],
                "actor": row["actor"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence")
        ]
    finally:
        connection.close()


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local approval-first project update workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize a local project database")
    init_parser.add_argument("--db", required=True)
    init_parser.add_argument("--projects", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest one RFC 822 email and propose an update")
    ingest_parser.add_argument("--db", required=True)
    ingest_parser.add_argument("--source", required=True)

    proposals_parser = subparsers.add_parser("proposals", help="List proposals")
    proposals_parser.add_argument("--db", required=True)
    proposals_parser.add_argument("--state", choices=("pending", "rejected", "applied"))

    review_parser = subparsers.add_parser("review", help="Approve, reject or correct a pending proposal")
    review_parser.add_argument("--db", required=True)
    review_parser.add_argument("--proposal-id", required=True)
    review_parser.add_argument("--decision", required=True, choices=("approve", "reject", "correct"))
    review_parser.add_argument("--actor", required=True)
    review_parser.add_argument("--reason", default="")
    review_parser.add_argument("--correction-file")

    project_parser = subparsers.add_parser("project", help="Show current project state")
    project_parser.add_argument("--db", required=True)
    project_parser.add_argument("--project-id", required=True)

    audit_parser = subparsers.add_parser("audit", help="Show the ordered audit trail")
    audit_parser.add_argument("--db", required=True)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "init":
            result = initialize_database(args.db, args.projects)
        elif args.command == "ingest":
            result = ingest_message(args.db, args.source).to_dict()
        elif args.command == "proposals":
            result = list_proposals(args.db, args.state)
        elif args.command == "review":
            correction = load_json_file(args.correction_file) if args.correction_file else None
            if args.decision == "correct" and correction is None:
                raise ReviewError("--correction-file is required for a correct decision")
            if args.decision != "correct" and correction is not None:
                raise ReviewError("--correction-file is only valid for a correct decision")
            result = review_proposal(
                args.db,
                args.proposal_id,
                args.decision,
                args.actor,
                reason=args.reason,
                correction=correction,
            )
        elif args.command == "project":
            result = get_project(args.db, args.project_id)
        elif args.command == "audit":
            result = list_audit_events(args.db)
        else:  # pragma: no cover - argparse keeps this unreachable.
            raise ProjectOpsError(f"Unsupported command: {args.command}")
        print_json(result)
        return 0
    except ProjectOpsError as exc:
        print_json({"status": "blocked", "error": str(exc), "mutation_performed": False})
        return 2


if __name__ == "__main__":
    sys.exit(main())
