"""Offline diagnosis: no model calls, no writes to Core or the exam database."""
import os
os.environ["MEMOWEFT_SEMANTIC_ENABLED"] = "0"
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from collections import Counter
from memoweft.integrations.hermes import recall
from runner import summarize
os.environ["MEMOWEFT_SEMANTIC_ENABLED"] = "0"

ROOT = Path(__file__).resolve().parent
RUN = ROOT / "runs" / "medium-user-01"
OUT = ROOT / "reports"
UID = json.loads((RUN / "manifest.json").read_text(encoding="utf-8"))["uuid"]
COMPLETE_SESSIONS = 12  # Last fully scored session count observed before the stop request.


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    database = RUN / "home" / "memoweft" / "memoweft.sqlite3"
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    records = {key: [] for key in ("memory_integrity_records", "memory_accuracy_records", "memory_update_records", "question_answering_records")}
    qa = []
    for sid in range(COMPLETE_SESSIONS):
        folder = RUN / "sessions" / f"{sid:03}"
        artifact, scores = load(folder / "artifact.json"), load(folder / "scores.json")
        for key, rows in scores.items():
            records[key].extend(rows)
        for index, question in enumerate(artifact.get("questions", [])):
            score = next(row for row in scores["question_answering_records"] if row["question"] == question["question"])
            qa.append({"session": sid + 1, "question_index": index, **question, "verdict": score["result_type"]})
    summary = summarize(records)
    summary.update(sessions_completed=COMPLETE_SESSIONS, incomplete_session_excluded=13, not_scored_session_started=14,
                   note="Sessions 1-12 only; 3 pre-stop invalid memory judgments retained. Stopped session 13 is not included.")
    save("valid-summary.json", summary)
    save("valid-records.json", records)
    save("qa-audit.json", qa)

    current = recall._current_world_rows(db, UID, "cloud")
    def snapshot_rows(sid):
        saved = load(RUN / "sessions" / f"{sid:03}" / "after.json")
        by_id = {item["item_id"]: item for item in saved.values()}
        rows = [copy.deepcopy(row) for row in current if row["id"] in by_id]
        assert len(rows) == len(by_id)
        for row in rows:
            item = by_id[row["id"]]
            assert item["value"]["content"] in row["content"]
            row["confidence"] = item["value"]["confidence"]
        return rows

    first = snapshot_rows(0)
    birthday = next(row for row in first if "I was born on 1996-08-02" in row["content"])
    name_query = "What is Martin Mark's birth date?"
    name_hits = recall._match_world_rows(name_query, first)
    original_context = load(RUN / "sessions" / "000" / "artifact.json")["questions"][1]["context"]
    assert recall.format_recall(name_hits) == original_context
    name_only = [row for row in qa if row["context"] == "记忆：My name is Martin Mark, and I am a Male."]
    travel_rows = snapshot_rows(10)
    travel = load(RUN / "sessions" / "010" / "artifact.json")["questions"][0]
    target = next(row for row in travel_rows if "actually looking into alternatives" in row["content"])
    travel_hits = recall._match_world_rows(travel["question"], travel_rows)
    assert recall.format_recall(travel_hits) == travel["context"]
    query_tokens = recall._bigrams(travel["question"])
    travel_scores = []
    for row in [*travel_hits, target]:
        tokens = recall._bigrams(row["content"])
        common = query_tokens & tokens
        travel_scores.append({"content": row["content"], "shared_tokens": sorted(common),
                              "literal_score": recall._compute_overlap_score(query_tokens, tokens, common)})
    save("reproduction-fixtures.json", {"first_session_rows": first, "birthday_id": birthday["id"],
        "name_query": name_query, "travel_rows": travel_rows, "travel_query": travel["question"], "travel_target_id": target["id"]})

    formation_counts = Counter()
    formation_traces = []
    actions = Counter()
    for row in db.execute("SELECT * FROM memory_world_job"):
        sid = int(row["parent_session_id"].rsplit("-", 1)[1])
        if sid >= COMPLETE_SESSIONS:
            continue
        payload, result = json.loads(row["model_result_json"] or "{}"), json.loads(row["world_result_json"] or "{}")
        rewrite = payload.get("formation_rewrite")
        formation_counts["jobs"] += 1
        formation_counts["terminal_" + str(row["terminal_state"])] += 1
        if rewrite:
            formation_counts["rewrites"] += 1
            formation_counts["rewrite_first_" + str(rewrite.get("error", {}).get("code"))] += 1
        for item in result.get("cognitions", []):
            actions[item.get("action", "unknown")] += 1
        sources = [db.execute("SELECT raw_content FROM evidence WHERE id=?", (eid,)).fetchone()[0]
                   for eid in json.loads(row["evidence_ids_json"])]
        relevant = ((sid == 0 and any("Specifically" in text for text in sources))
                    or (sid in (10, 11) and any("I've been thinking" in text for text in sources))
                    or (sid == 9 and any("seminar" in text for text in sources)))
        if relevant:
            formation_traces.append({"session": sid + 1, "evidence": sources, "model_output": payload.get("content"),
                "world_result": result, "rewrite_final_error": None if not rewrite else rewrite.get("final_error")})
    totals = Counter()
    for line in (RUN / "usage.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        totals.update({key: row.get(key, 0) for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")})
        totals["calls"] += 1
    estimate = (totals["prompt_tokens"] - totals["cached_tokens"] + .02 * totals["cached_tokens"] + 2 * totals["completion_tokens"]) / 1_000_000
    evidence = {"scope": summary["note"], "formation_counts": dict(formation_counts), "formation_actions": dict(actions),
        "naming_filter": {"snapshot_rows": len(first), "eligible_named": len(recall._query_eligible_rows(name_query, first)),
                          "stored_birthday": birthday["content"], "actual_recall": original_context,
                          "name_only_qa_count": len(name_only), "name_only_verdicts": dict(Counter(q["verdict"] for q in name_only))},
        "ranking_example": travel_scores,
        "formation_traces": formation_traces,
        "middle_name_verdicts": dict(Counter(q["verdict"] for q in qa if "middle name" in q["question"])),
        "context_truncation_count": sum(q["context"].endswith("…") for q in qa),
        "usage_at_stop": dict(totals), "estimated_cny_at_published_price": estimate,
        "diagnostic_api_calls": 0, "database_total_changes": db.total_changes}
    db.close()
    assert before_hash == hashlib.sha256(database.read_bytes()).hexdigest()
    evidence["database_sha256_before_after"] = before_hash
    save("diagnosis-evidence.json", evidence)
    print(json.dumps({"fully_scored_sessions": COMPLETE_SESSIONS, "qa": summary["qa"],
                      "name_only": evidence["naming_filter"], "cost_cny": estimate, "database_unchanged": True}, ensure_ascii=True))


if __name__ == "__main__":
    main()
