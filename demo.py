#!/usr/bin/env python3
"""Run the complete ProjectOps Workflow demo against synthetic local data."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from projectops import (
    get_project,
    ingest_message,
    initialize_database,
    list_audit_events,
    review_proposal,
)


ROOT = Path(__file__).resolve().parent


def show(title: str, value: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="projectops-demo-") as directory:
        database = Path(directory) / "projectops.sqlite3"
        projects = ROOT / "sample_data" / "projects.json"
        message = ROOT / "sample_data" / "inbox" / "acme_weekly_update.eml"
        correction = json.loads(
            (ROOT / "sample_data" / "correction.json").read_text(encoding="utf-8")
        )

        initialize_database(database, projects)
        before = get_project(database, "ACME-001")
        ingested = ingest_message(database, message)
        pending_project = get_project(database, "ACME-001")

        show("Initial project", before)
        show("Pending proposal", ingested.to_dict())
        show(
            "Pending safety check",
            {"project_unchanged": before == pending_project},
        )

        corrected = review_proposal(
            database,
            ingested.proposal_id,
            "correct",
            "Morgan, Operations Lead",
            reason="Use the client-confirmed review date",
            correction=correction,
        )
        show("Human correction", corrected)

        approved = review_proposal(
            database,
            ingested.proposal_id,
            "approve",
            "Morgan, Operations Lead",
            reason="Verified against the source email",
        )
        show("Approval and resulting project", approved)

        duplicate = ingest_message(database, message)
        show("Duplicate replay", duplicate.to_dict())
        show("Ordered audit trail", list_audit_events(database))


if __name__ == "__main__":
    main()
