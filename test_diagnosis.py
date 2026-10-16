"""Expected failures demonstrate unfixed defects; these are not passing quality gates."""
import os
os.environ["MEMOWEFT_SEMANTIC_ENABLED"] = "0"
import json
import pathlib
import sqlite3
import unittest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from memoweft.integrations.hermes import recall
from memoweft.semantic.runtime import SemanticCandidate
from memoweft.semantic.store import content_hash
from runner import evaluation
os.environ["MEMOWEFT_SEMANTIC_ENABLED"] = "0"

ROOT = pathlib.Path(__file__).resolve().parent
FIXTURE = json.loads((ROOT / "reports/reproduction-fixtures.json").read_text(encoding="utf-8"))


class Diagnosis(unittest.TestCase):
    def test_original_name_failure_is_reproduced(self):
        rows = FIXTURE["first_session_rows"]
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(recall._query_eligible_rows(FIXTURE["name_query"], rows)), 1)
        hits = recall._match_world_rows("When was I born?", rows)
        self.assertEqual(hits[0]["id"], FIXTURE["birthday_id"])

    @unittest.expectedFailure
    def test_named_owner_question_should_keep_known_birthday_eligible(self):
        allowed = recall._query_eligible_rows(FIXTURE["name_query"], FIXTURE["first_session_rows"])
        self.assertIn(FIXTURE["birthday_id"], [row["id"] for row in allowed])

    @unittest.expectedFailure
    def test_even_perfect_semantic_nomination_is_blocked_by_name_filter(self):
        # Controlled oracle candidate tests the filter; NOT a measured embedding score or benchmark improvement.
        class Oracle:
            config = SimpleNamespace(enabled=True, threshold=.87, max_additions=3)
            def candidates(self, db, subject_id, query):
                text = db.execute("SELECT content FROM cognition WHERE id=?", (FIXTURE["birthday_id"],)).fetchone()[0]
                return [SemanticCandidate("cognition", FIXTURE["birthday_id"], 1.0, content_hash(text))]
        path = ROOT / "runs/medium-user-01/home/memoweft/memoweft.sqlite3"
        uid = json.loads((ROOT / "runs/medium-user-01/manifest.json").read_text())["uuid"]
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            result = recall.recall_world_snapshot(db, uid, FIXTURE["name_query"], model_tier="cloud", candidate_provider=Oracle())
            self.assertIsNotNone(result)
            self.assertEqual(db.total_changes, 0)
            self.assertIn(("cognition", FIXTURE["birthday_id"]), result.selected_item_ids)

    @unittest.expectedFailure
    def test_explicit_travel_alternatives_should_be_retrieved(self):
        matches = recall._match_world_rows(FIXTURE["travel_query"], FIXTURE["travel_rows"])
        self.assertIn(FIXTURE["travel_target_id"], [row["id"] for row in matches])

    @unittest.expectedFailure
    def test_stop_should_abort_scoring_instead_of_becoming_invalid_judgement(self):
        user = {"uuid": "test", "user_name": "test", "sessions": [{"memory_points": [],
            "extracted_memories": [], "dialogue": [], "questions": [{"question": "test", "answer": "test", "evidence": [], "system_response": "test"}]}]}
        with patch.object(evaluation, "ProcessPoolExecutor", ThreadPoolExecutor), patch.object(
            evaluation, "evaluation_for_question", side_effect=RuntimeError("stop_requested")):
            with self.assertRaises(RuntimeError):
                evaluation.process_user(1, user, max_workers=1)


if __name__ == "__main__":
    unittest.main()
