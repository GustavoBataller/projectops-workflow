import base64
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECTOPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECTOPS_ROOT))

from gmail_intake import (  # noqa: E402
    GmailIntakeError,
    ingest_gmail_message,
    main as gmail_main,
)
from projectops import (  # noqa: E402
    ProjectOpsError,
    get_project,
    ingest_message,
    initialize_database,
    list_audit_events,
    list_proposals,
    review_proposal,
)


class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class ReadOnlyMessages:
    """A fake surface intentionally exposing only Gmail list/get operations."""

    def __init__(self, raw, *, message_id="gmail-message-001", matches=None, get_error=None):
        self.raw = raw
        self.message_id = message_id
        self.matches = matches
        self.get_error = get_error
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        matches = self.matches if self.matches is not None else [{"id": self.message_id}]
        return FakeRequest({"messages": matches})

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.get_error:
            return FakeRequest(error=self.get_error)
        encoded = base64.urlsafe_b64encode(self.raw).decode("ascii").rstrip("=")
        return FakeRequest(
            {"id": kwargs["id"], "threadId": "gmail-thread-001", "raw": encoded}
        )


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, messages):
        self._users = FakeUsers(messages)

    def users(self):
        return self._users


class GmailIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "projectops.sqlite3"
        self.projects = PROJECTOPS_ROOT / "sample_data" / "projects.json"
        self.email = PROJECTOPS_ROOT / "sample_data" / "inbox" / "acme_weekly_update.eml"
        self.raw = self.email.read_bytes()
        initialize_database(self.db, self.projects)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_exact_message_uses_only_get_and_preserves_approval_boundary(self):
        messages = ReadOnlyMessages(self.raw)
        before = get_project(self.db, "ACME-001")

        result = ingest_gmail_message(
            self.db, FakeService(messages), message_id="gmail-message-001"
        )

        self.assertEqual(result.status, "created")
        self.assertEqual(result.proposal_state, "pending")
        self.assertEqual(before, get_project(self.db, "ACME-001"))
        self.assertEqual([call[0] for call in messages.calls], ["get"])
        self.assertEqual(messages.calls[0][1]["format"], "raw")
        with sqlite3.connect(self.db) as connection:
            source_path = connection.execute("SELECT source_path FROM messages").fetchone()[0]
        self.assertEqual(source_path, "gmail://users/me/messages/gmail-message-001")

        review_proposal(self.db, result.proposal_id, "approve", "Test reviewer")
        self.assertEqual(get_project(self.db, "ACME-001")["status"], "at_risk")

    def test_gmail_composed_inline_labels_keep_existing_ingestion_semantics(self):
        headers, body = self.raw.split(b"\n\n", 1)
        inline_body = b" ".join(body.splitlines()).replace(
            b"Milestone: Solution design", b"Milestone: Solution\r\n design"
        )
        inline_raw = headers + b"\n\n" + inline_body + b"\n"

        result = ingest_gmail_message(
            self.db,
            FakeService(ReadOnlyMessages(inline_raw)),
            message_id="gmail-inline-message-001",
        )

        self.assertEqual(result.status, "created")
        self.assertEqual(result.proposal_state, "pending")
        self.assertEqual(result.proposal["status"], "at_risk")
        self.assertEqual(result.proposal["milestone"], "Solution design")
        self.assertEqual(result.proposal["risks"], [
            "Client data export delayed",
            "Design review availability",
        ])

    def test_query_must_select_one_message_and_duplicate_replay_is_idempotent(self):
        messages = ReadOnlyMessages(self.raw)
        service = FakeService(messages)

        first = ingest_gmail_message(self.db, service, query="subject:ProjectOps-Sandbox")
        duplicate = ingest_gmail_message(self.db, service, query="subject:ProjectOps-Sandbox")

        self.assertEqual(first.status, "created")
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(duplicate.proposal_id, first.proposal_id)
        self.assertEqual([call[0] for call in messages.calls], ["list", "get", "list", "get"])
        self.assertEqual(len(list_proposals(self.db)), 1)

    def test_same_gmail_id_with_different_content_stops_as_conflicting_replay(self):
        ingest_gmail_message(
            self.db, FakeService(ReadOnlyMessages(self.raw)), message_id="gmail-message-001"
        )
        conflicting = self.raw.replace(b"Status: at_risk", b"Status: blocked")

        with self.assertRaisesRegex(ProjectOpsError, "different content"):
            ingest_gmail_message(
                self.db,
                FakeService(ReadOnlyMessages(conflicting)),
                message_id="gmail-message-001",
            )

        self.assertEqual(len(list_proposals(self.db)), 1)
        self.assertEqual(list_proposals(self.db)[0]["proposal"]["status"], "at_risk")

    def test_gmail_identity_is_retained_when_content_was_already_ingested_locally(self):
        local = ingest_message(self.db, self.email)
        gmail = ingest_gmail_message(
            self.db, FakeService(ReadOnlyMessages(self.raw)), message_id="gmail-message-001"
        )
        self.assertEqual(gmail.status, "duplicate")
        self.assertEqual(gmail.proposal_id, local.proposal_id)

        conflicting = self.raw.replace(b"Status: at_risk", b"Status: blocked")
        with self.assertRaisesRegex(ProjectOpsError, "different content"):
            ingest_gmail_message(
                self.db,
                FakeService(ReadOnlyMessages(conflicting)),
                message_id="gmail-message-001",
            )

    def test_retrieval_failure_does_not_mutate_project_state(self):
        before_project = get_project(self.db, "ACME-001")
        before_audit = list_audit_events(self.db)
        messages = ReadOnlyMessages(self.raw, get_error=RuntimeError("network unavailable"))

        with self.assertRaisesRegex(GmailIntakeError, "no message was ingested"):
            ingest_gmail_message(
                self.db, FakeService(messages), message_id="gmail-message-001"
            )

        self.assertEqual(before_project, get_project(self.db, "ACME-001"))
        self.assertEqual(before_audit, list_audit_events(self.db))
        self.assertEqual(list_proposals(self.db), [])

    def test_zero_or_ambiguous_query_matches_stop_without_mutation(self):
        for matches, expected in [([], "no messages"), ([{"id": "a"}, {"id": "b"}], "more than one")]:
            with self.subTest(matches=matches):
                messages = ReadOnlyMessages(self.raw, matches=matches)
                with self.assertRaisesRegex(GmailIntakeError, expected):
                    ingest_gmail_message(self.db, FakeService(messages), query="label:sandbox")
                self.assertEqual(list_proposals(self.db), [])

    def test_authorization_failure_from_cli_does_not_mutate_state(self):
        with patch(
            "gmail_intake.build_gmail_service",
            side_effect=GmailIntakeError("Gmail OAuth authorization failed"),
        ):
            status = gmail_main(
                [
                    "--db",
                    str(self.db),
                    "--message-id",
                    "gmail-message-001",
                    "--credentials",
                    str(self.root / "credentials.json"),
                    "--token",
                    str(self.root / "token.json"),
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(list_proposals(self.db), [])


if __name__ == "__main__":
    unittest.main()
