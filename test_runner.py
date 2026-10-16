import unittest
from runner import boundary, turns, changed_memories, summarize
from memoweft.integrations.dsh_bridge import _validate_boundary_envelope


class AdapterChecks(unittest.TestCase):
    def test_roles_dates_and_no_gold_in_boundary(self):
        messages = [{"role": role, "content": content, "timestamp": "Jan 01, 2026, 10:00:00", "answer": "GOLD"}
                    for role, content in [("user", "My name is Ada."), ("assistant", "Hello."), ("user", "I like tea."), ("assistant", "Noted.")]]
        groups = turns(messages)
        self.assertEqual([len(g) for g in groups], [2, 2])
        payload = boundary(groups[0], "test", 0, 0)
        _, normalized = _validate_boundary_envelope(payload)
        self.assertEqual([x["role"] for x in normalized], ["user", "assistant"])
        self.assertNotIn("GOLD", str(payload))
        self.assertEqual(normalized[0]["timestamp"], 1767261600)

    def test_extraction_keeps_new_and_changed_but_not_old(self):
        def item(text):
            return {"value": {"content": text}}
        before = {"old": item("old fact"), "changed": item("coffee")}
        after = {"old": item("old fact"), "changed": item("tea"), "new": item("new fact")}
        self.assertEqual(changed_memories(before, after), ["tea", "new fact"])

    def test_empty_metrics_are_undefined(self):
        records = {k: [] for k in ("memory_integrity_records", "memory_accuracy_records", "memory_update_records", "question_answering_records")}
        result = summarize(records)
        self.assertIsNone(result["qa"]["correct"])
        self.assertIsNone(result["extraction_recall"])
        self.assertEqual(result["invalid_judgements"], 0)


if __name__ == "__main__":
    unittest.main()
