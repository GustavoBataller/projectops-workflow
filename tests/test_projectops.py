import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECTOPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECTOPS_ROOT))

from projectops import (  # noqa: E402
    ProjectMatchError,
    ProjectOpsError,
    ReviewError,
    get_project,
    ingest_message,
    initialize_database,
    list_audit_events,
    list_proposals,
    review_proposal,
)


class ProjectOpsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "projectops.sqlite3"
        self.projects = PROJECTOPS_ROOT / "sample_data" / "projects.json"
        self.email = PROJECTOPS_ROOT / "sample_data" / "inbox" / "acme_weekly_update.eml"
        initialize_database(self.db, self.projects)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_email(self, name, *, subject, body, message_id=None):
        path = self.root / name
        headers = [
            f"Message-ID: <{message_id or name}@example.test>",
            "From: client@example.test",
            "To: operations@example.test",
            f"Subject: {subject}",
            'Content-Type: text/plain; charset="utf-8"',
            "",
            body,
        ]
        path.write_text("\n".join(headers), encoding="utf-8")
        return path

    def test_pending_proposal_does_not_mutate_project_and_ingest_is_idempotent(self):
        before = get_project(self.db, "ACME-001")
        first = ingest_message(self.db, self.email)
        after = get_project(self.db, "ACME-001")
        duplicate = ingest_message(self.db, self.email)

        self.assertEqual(first.status, "created")
        self.assertEqual(first.project_id, "ACME-001")
        self.assertEqual(first.proposal_state, "pending")
        self.assertEqual(first.proposal["status"], "at_risk")
        self.assertEqual(before, after)
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(duplicate.proposal_id, first.proposal_id)
        self.assertEqual(len(list_proposals(self.db)), 1)

    def test_correction_stays_pending_and_approval_applies_exactly_once(self):
        result = ingest_message(self.db, self.email)
        corrected = review_proposal(
            self.db,
            result.proposal_id,
            "correct",
            "Morgan, Operations Lead",
            reason="Client confirmed the date",
            correction={"next_actions": ["Hold solution review on 23 September"]},
        )

        self.assertEqual(corrected["state"], "pending")
        self.assertEqual(corrected["version"], 2)
        self.assertEqual(get_project(self.db, "ACME-001")["status"], "on_track")

        applied = review_proposal(
            self.db,
            result.proposal_id,
            "approve",
            "Morgan, Operations Lead",
            reason="Verified against source",
        )
        project = get_project(self.db, "ACME-001")

        self.assertEqual(applied["state"], "applied")
        self.assertEqual(project["status"], "at_risk")
        self.assertEqual(project["milestone"], "Solution design")
        self.assertEqual(project["next_actions"], ["Hold solution review on 23 September"])
        with self.assertRaisesRegex(ReviewError, "already applied"):
            review_proposal(self.db, result.proposal_id, "approve", "Morgan")

        actions = [event["action"] for event in list_audit_events(self.db)]
        self.assertIn("proposal.corrected", actions)
        self.assertIn("proposal.approved", actions)
        self.assertIn("project.updated", actions)
        self.assertEqual(actions.count("project.updated"), 1)

    def test_rejection_records_decision_without_project_mutation(self):
        before = get_project(self.db, "ACME-001")
        result = ingest_message(self.db, self.email)
        rejected = review_proposal(
            self.db,
            result.proposal_id,
            "reject",
            "Morgan, Operations Lead",
            reason="Sender asked us to disregard this update",
        )

        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(before, get_project(self.db, "ACME-001"))
        self.assertEqual(list_proposals(self.db, "rejected")[0]["decision_reason"],
                         "Sender asked us to disregard this update")
        self.assertIn("proposal.rejected", [event["action"] for event in list_audit_events(self.db)])

    def test_ambiguous_project_stops_before_message_or_proposal_mutation(self):
        email = self.write_email(
            "ambiguous.eml",
            subject="Acme and Bravo combined update",
            body="Summary: Joint dependency review\nStatus: at_risk",
        )
        before_audit = list_audit_events(self.db)

        with self.assertRaisesRegex(ProjectMatchError, "Ambiguous project match"):
            ingest_message(self.db, email)

        self.assertEqual(list_proposals(self.db), [])
        self.assertEqual(before_audit, list_audit_events(self.db))

    def test_unmatched_project_and_unstructured_update_stop_without_mutation(self):
        unmatched = self.write_email(
            "unknown.eml",
            subject="Unknown engagement update",
            body="Summary: Work completed\nStatus: on_track",
        )
        with self.assertRaisesRegex(ProjectMatchError, "No project"):
            ingest_message(self.db, unmatched)

        unstructured = self.write_email(
            "unstructured.eml",
            subject="[ACME-001] Hello",
            body="Summary: Friendly note with no operational update",
        )
        with self.assertRaisesRegex(ProjectOpsError, "no structured update fields"):
            ingest_message(self.db, unstructured)

        self.assertEqual(list_proposals(self.db), [])

    def test_reused_message_id_with_different_content_stops_without_mutation(self):
        first = ingest_message(self.db, self.email)
        conflicting = self.write_email(
            "conflicting.eml",
            message_id="acme-weekly-update-001",
            subject="[ACME-001] Conflicting replay",
            body="Summary: Different update\nStatus: blocked",
        )

        with self.assertRaisesRegex(ProjectOpsError, "different content"):
            ingest_message(self.db, conflicting)

        proposals = list_proposals(self.db)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["proposal_id"], first.proposal_id)
        self.assertEqual(proposals[0]["proposal"]["status"], "at_risk")

    def test_correction_contract_rejects_unknown_or_invalid_fields(self):
        result = ingest_message(self.db, self.email)
        with self.assertRaisesRegex(ReviewError, "Unsupported correction fields"):
            review_proposal(
                self.db,
                result.proposal_id,
                "correct",
                "Morgan",
                correction={"approved": True},
            )
        with self.assertRaisesRegex(ReviewError, "must be an array of strings"):
            review_proposal(
                self.db,
                result.proposal_id,
                "correct",
                "Morgan",
                correction={"risks": "not-a-list"},
            )
        self.assertEqual(list_proposals(self.db)[0]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
