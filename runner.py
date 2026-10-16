"""Isolated HaluMem pilot. Core and official scoring prompts stay unmodified."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

ROOT = Path(__file__).resolve().parent
MODEL = "mimo-v2.6-flash"
BASE = "https://api.xiaomimimo.com/v1"
CORE_SHA = "e86b73ba9570a9bd490ef90bfb7059574de5e449"
HALU_SHA = "718f16ff0c83413b1c86fa83fc13cc1a639871f9"
os.environ.update(OPENAI_BASE_URL=BASE, OPENAI_MODEL=MODEL,
                  OPENAI_API_KEY=os.environ.get("MIMO_API_KEY", "not-set"),
                  RETRY_TIMES="3", WAIT_TIME_LOWER="1", WAIT_TIME_UPPER="10",
                  MEMOWEFT_SEMANTIC_ENABLED="1",
                  MEMOWEFT_SEMANTIC_CACHE_DIR=str(ROOT / "model-cache"))
sys.path.insert(0, str(ROOT / "halumem" / "eval"))
import eval_tools
import evaluation
from prompts import PROMPT_MEMZERO
from memoweft.integrations.dsh_bridge import DshMemoWeftRuntime, _boundary_payload_hash
from memoweft.integrations.trust import QueryService
from memoweft.semantic.runtime import runtime_for


def save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


class Api:
    def __init__(self, folder: Path, token_limit=25_000_000):
        self.folder = folder
        self.lock = threading.Lock()
        self.token_limit = token_limit
        self.usage_file = folder / "usage.jsonl"
        self.totals = Counter()
        self.stage = "startup"
        folder.mkdir(parents=True, exist_ok=True)
        if self.usage_file.exists():
            for line in self.usage_file.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                self.totals.update({k: row.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")})
                self.totals["calls"] += 1
        self.client = httpx.Client(timeout=180)

    def request(self, messages, *, stage, json_mode=False, cache=False):
        body = {"model": MODEL, "messages": messages, "temperature": 0,
                "thinking": {"type": "disabled"}, "max_tokens": 8192 if stage == "formation" else 2048}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.folder / "cache" / (stage + "-" + digest + ".json")
        if cache and path.exists():
            return read(path)
        for attempt in range(3):
            if (ROOT / "STOP").exists():
                raise RuntimeError("stop_requested")
            with self.lock:
                if self.totals["total_tokens"] >= self.token_limit:
                    raise RuntimeError("token_budget_reached")
            try:
                response = self.client.post(BASE + "/chat/completions", json=body,
                    headers={"Authorization": "Bearer " + os.environ["MIMO_API_KEY"]})
                if response.status_code != 200:
                    raise RuntimeError(f"api_http_{response.status_code}")
                payload = response.json()
                usage = payload.get("usage", {})
                row = {"at": datetime.now(timezone.utc).isoformat(), "stage": stage,
                       "model": payload.get("model"), "request_hash": digest,
                       **{k: int(usage.get(k, 0)) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                       "cached_tokens": int(usage.get("prompt_tokens_details", {}).get("cached_tokens", 0))}
                with self.lock:
                    with self.usage_file.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    self.totals.update({k: row[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")})
                    self.totals["calls"] += 1
                choice = payload["choices"][0]
                result = {"content": choice["message"].get("content") or "",
                          "finish_reason": choice.get("finish_reason"),
                          "model": payload.get("model", MODEL), "provider": "xiaomi-mimo", "usage": usage}
                if result["finish_reason"] == "length" and stage != "formation":
                    raise RuntimeError("model_output_truncated")
                if cache:
                    save(path, result)
                return result
            except Exception as exc:
                if attempt == 2:
                    # Never expose HTTP response bodies or request headers.
                    raise RuntimeError("api_failure_" + type(exc).__name__) from None
                time.sleep(2 * (attempt + 1))

    def formation(self, messages, session_id=""):
        return self.request(messages, stage="formation", json_mode=True)

    def judge(self, prompt):
        result = self.request([{"role": "user", "content": prompt}], stage="judge", cache=True)
        content = result["content"]
        match = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
        # Official parser accepts fenced JSON; accepting a bare JSON object is a transport-only extension.
        return json.loads(match.group(1) if match else content)


def turns(dialogue):
    """Preserve all speakers and order; one user turn plus following assistant context."""
    groups, current = [], []
    for message in dialogue:
        if message["role"] == "user" and any(m["role"] == "user" for m in current):
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def boundary(messages, uid, sid, tid):
    session_id = f"halumem-{uid}-{sid}"
    rows = []
    for index, message in enumerate(messages):
        dt = datetime.strptime(message["timestamp"], "%b %d, %Y, %H:%M:%S").replace(tzinfo=timezone.utc)
        rows.append({"role": message["role"], "content": message["content"],
                     "source_ref": f"source:{index}", "timestamp": dt.timestamp(),
                     "message_id": f"{uid}-{sid}-{tid}-{index}"})
    result = {"schema_version": 1, "provider_name": "memoweft",
              "parent_session_id": session_id, "result_session_id": session_id,
              "mode": "turn", "source_messages": rows}
    result["payload_hash"] = _boundary_payload_hash(result)
    occurrence = hashlib.sha256(f"{uid}:{sid}:{tid}".encode()).hexdigest()[:32]
    result["event_id"] = f"weftmate-turn-boundary-v1:{occurrence}:{result['payload_hash']}"
    return result


def memory_items(query):
    # Entity registry names alone are not statements. All content-bearing formal kinds participate.
    return {(x["object_kind"] + ":" + x["item_id"]): x for x in query.list_world_items()["items"]
            if x["object_kind"] in {"event", "relationship", "cognition"}}


def item_text(item):
    return str(item["value"]["content"])


def changed_memories(before, after):
    return [item_text(item) for key, item in after.items()
            if key not in before or item["value"] != before[key]["value"]]


def wait_job(runtime, event_id):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if (ROOT / "STOP").exists():
            raise RuntimeError("stop_requested")
        with sqlite3.connect(runtime.db_path, timeout=20) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT job_id,state,terminal_state,terminal_detail FROM memory_world_job WHERE boundary_event_id=?", (event_id,)).fetchall()
        if rows and all(row["terminal_state"] is not None for row in rows):
            return [dict(row) for row in rows]
        time.sleep(0.25)
    raise RuntimeError("formation_wait_timeout")


def ready_semantics(runtime):
    sem = runtime_for(runtime.db_path)
    sem.start()
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = sem.status()
        if status["state"] == "unavailable":
            raise RuntimeError("semantic_unavailable_" + str(status.get("reason")))
        if status["state"] == "ready" and status.get("backfill_complete"):
            with sqlite3.connect(runtime.db_path, timeout=20) as db:
                pending = db.execute("SELECT count(*) FROM semantic_pending").fetchone()[0]
            if pending == 0:
                return status
        time.sleep(0.25)
    raise RuntimeError("semantic_wait_timeout")


def retrieve(query_service, query, limit):
    preview = query_service.preview_recall(query, model_tier="cloud")["preview"]
    # Preserve the production renderer, ordering, truncation and historical labels.
    # Core applies its own retrieval budget; do not invent a separate ranking or truncate lines.
    return preview, [preview["rendered_recall"]] if preview["count"] else []


def ratio(a, b):
    return a / b if b else None


def summarize(records):
    integrity = records["memory_integrity_records"]
    target = [x for x in integrity if x["memory_source"] != "interference"]
    interference = [x for x in integrity if x["memory_source"] == "interference"]
    accuracy = records["memory_accuracy_records"]
    target_acc = [x for x in accuracy if x.get("is_included_in_golden_memories") in ("true", "True")]
    recall = ratio(sum(x.get("memory_integrity_score") == 2 for x in target), len(target))
    precision = ratio(sum(.5 * (x.get("memory_accuracy_score") or 0) for x in target_acc), len(target_acc))
    result = {"extraction_recall": recall, "target_precision": precision,
              "extraction_f1": (2 * recall * precision / (recall + precision) if recall is not None and precision is not None and recall + precision else 0 if recall == 0 or precision == 0 else None),
              "false_memory_resistance": ratio(sum(x.get("memory_integrity_score") == 0 for x in interference), len(interference)),
              "memory_accuracy": ratio(sum(.5 * (x.get("memory_accuracy_score") or 0) for x in accuracy), len(accuracy)),
              "counts": {k: len(v) for k, v in records.items()}}
    for name, key, field in (("qa", "question_answering_records", "result_type"), ("update", "memory_update_records", "memory_update_type")):
        rows = records[key]
        counts = Counter(x.get(field) or "Invalid" for x in rows)
        result[name] = {"count": len(rows), "counts": dict(counts), **{label.lower(): ratio(counts[label], len(rows)) for label in ("Correct", "Hallucination", "Omission")}}
    result["invalid_judgements"] = (sum(x.get("memory_integrity_score") is None for x in integrity)
        + sum(x.get("memory_accuracy_score") is None for x in accuracy)
        + sum(x.get("memory_update_type") not in {"Correct", "Hallucination", "Omission", "Other"} for x in records["memory_update_records"])
        + sum(x.get("result_type") not in {"Correct", "Hallucination", "Omission"} for x in records["question_answering_records"]))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-limit", type=int, default=65)
    parser.add_argument("--run", default="medium-user-01")
    args = parser.parse_args()
    folder = ROOT / "runs" / args.run
    folder.mkdir(parents=True, exist_ok=True)
    process_lock = (folder / "run.lock").open("a+b")
    if process_lock.tell() == 0:
        process_lock.write(b"0")
        process_lock.flush()
    process_lock.seek(0)
    try:
        msvcrt.locking(process_lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("This isolated exam is already running.", flush=True)
        process_lock.close()
        raise SystemExit(2)
    api = Api(folder)
    runtime = DshMemoWeftRuntime()
    status = {"pid": os.getpid(), "state": "starting", "core": CORE_SHA, "halumem": HALU_SHA, "model": MODEL,
              "started_at": datetime.now(timezone.utc).isoformat(), "run": args.run}

    def progress(**values):
        status.update(values)
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        status["usage"] = dict(api.totals)
        save(folder / "status.json", status)
        print(json.dumps({k: status.get(k) for k in ("state", "session", "sessions_total", "turn", "turns_total", "usage")}), flush=True)

    try:
        if not os.environ.get("MIMO_API_KEY"):
            raise RuntimeError("missing_api_key")
        data_path = ROOT / "data" / "HaluMem-Medium.jsonl"
        with data_path.open(encoding="utf-8") as f:
            user = json.loads(next(f))
        uid = user["uuid"]
        # Match official use of persona only for the display name; never ingest persona_info.
        name = re.search(r"Name:\s*(.*?); Gender:", user["persona_info"]).group(1).strip()
        metadata = {**status, "uuid": uid, "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
                    "runner_sha256_at_start": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "user_index": 0, "sessions": len(user["sessions"]), "qa": sum(len(s.get("questions", [])) for s in user["sessions"]),
                    "thinking": "disabled", "temperature": 0, "formation_max_tokens": 8192,
                    "answer_judge_max_tokens": 2048, "token_stop_limit": api.token_limit,
                    "input_granularity": "one user turn and following assistant context; sequential terminal wait",
                    "qa_prompt": "official PROMPT_MEMZERO", "judge_prompts": "official unmodified eval_tools.py",
                    "extraction": "session delta of current event/relationship/cognition content; entity registry excluded",
                    "recall": "production preview_recall (5 items / 600 characters); provisional recent_evidence excluded; semantic ready and backfill complete",
                    "scoring": "official process_user with thread executor and MiMo transport; official update routing retained",
                    "limitations": ["one user pilot, not leaderboard", "MiMo used for formation, answers and judge", "same-model judge may be biased", "no production data", "no gold or QA answers supplied to formation"]}
        if not (folder / "manifest.json").exists():
            save(folder / "manifest.json", metadata)
        runtime.initialize("halumem", dsh_home=str(folder / "home"), platform="halumem",
                           subject_id=uid, model_tier="cloud", lang="en", one_shot_llm=api.formation)
        query = QueryService(runtime.db_path, subject_id=uid)
        eval_tools.llm_request_for_json = api.judge
        evaluation.ProcessPoolExecutor = ThreadPoolExecutor
        progress(state="semantic_setup", sessions_total=len(user["sessions"]))
        ready_semantics(runtime)
        for sid, session in enumerate(user["sessions"][:args.session_limit]):
            session_dir = folder / "sessions" / f"{sid:03}"
            if (session_dir / "scores.json").exists():
                continue
            artifact_path = session_dir / "artifact.json"
            if not artifact_path.exists():
                before_path = session_dir / "before.json"
                before = read(before_path)
                if before is None:
                    before = memory_items(query)
                    save(before_path, before)
                groups = turns(session["dialogue"])
                diagnostics = read(session_dir / "turns.json", [])
                completed = len(diagnostics)
                start = time.monotonic()
                for tid, messages in enumerate(groups):
                    if tid < completed:
                        continue
                    progress(state="forming", session=sid + 1, turn=tid + 1, turns_total=len(groups))
                    envelope = boundary(messages, uid, sid, tid)
                    receipt = runtime.ingest_durable_boundary(envelope)
                    jobs = wait_job(runtime, envelope["event_id"])
                    diagnostics.append({"turn": tid, "jobs": jobs, "receipt": receipt})
                    save(session_dir / "turns.json", diagnostics)
                    if any(x["terminal_state"] in {"dead", "failed"} for x in jobs):
                        raise RuntimeError("formation_dead_terminal")
                progress(state="indexing", session=sid + 1)
                semantic = ready_semantics(runtime)
                after = memory_items(query)
                extracted = changed_memories(before, after)
                save(session_dir / "after.json", after)
                artifact = copy.deepcopy(session)
                artifact["extracted_memories"] = extracted
                artifact["add_dialogue_duration_ms"] = (time.monotonic() - start) * 1000
                artifact["semantic_status"] = semantic
                progress(state="retrieving", session=sid + 1)
                for memory in artifact.get("memory_points", []):
                    if memory["is_update"] == "True" and memory.get("original_memories"):
                        _, texts = retrieve(query, memory["memory_content"], 10)
                        memory["memories_from_system"] = texts
                for qa in artifact.get("questions", []):
                    begin = time.monotonic()
                    preview, _ = retrieve(query, qa["question"], 20)
                    qa["context"] = preview["rendered_recall"]
                    qa["search_duration_ms"] = (time.monotonic() - begin) * 1000
                    qa["selected_item_ids"] = preview["selected_item_ids"]
                    prompt = PROMPT_MEMZERO.format(context=qa["context"], question=qa["question"])
                    qa["system_response"] = api.request([{"role": "user", "content": prompt}], stage="answer", cache=True)["content"]
                save(artifact_path, artifact)
            artifact = read(artifact_path)
            progress(state="judging", session=sid + 1)
            scored = evaluation.process_user(1, {"uuid": uid, "user_name": name, "sessions": [artifact]}, max_workers=4)
            for rows in scored.values():
                for row in rows:
                    row["ssession_id"] = sid
            save(session_dir / "scores.json", scored)
            artifacts = [read(p) for p in sorted((folder / "sessions").glob("*/artifact.json"))]
            records = {k: [] for k in scored}
            for path in sorted((folder / "sessions").glob("*/scores.json")):
                for key, rows in read(path).items():
                    records[key].extend(rows)
            save(folder / "records.json", records)
            summary = summarize(records)
            summary.update(sessions_completed=len(artifacts), sessions_total=len(user["sessions"]), usage=dict(api.totals))
            save(folder / "summary.json", summary)
            output = {"uuid": uid, "user_name": name, "sessions": artifacts}
            (folder / "memoweft_eval_results.jsonl").write_text(json.dumps(output, ensure_ascii=False) + "\n", encoding="utf-8")
            progress(state="session_complete", sessions_completed=len(artifacts), summary=summary)
            write_report(folder, summary, complete=False)
        complete = args.session_limit >= len(user["sessions"])
        if complete:
            records = read(folder / "records.json")
            overall = {"memory_integrity": {}, "memory_accuracy": {}, "memory_extraction_f1": 0,
                       "memory_update": {}, "question_answering": {}, "memory_type_accuracy":
                       {kind: {"memory_integrity_acc": 0, "memory_update_acc": 0, "total_num": 0}
                        for kind in ("Event Memory", "Persona Memory", "Relationship Memory")}}
            try:
                official = evaluation.aggregate_eval_results({"overall_score": overall, **copy.deepcopy(records)})
                save(folder / "official-stat-results.json", official)
            except ZeroDivisionError:
                save(folder / "official-aggregation-note.json", {"reason": "official_zero_denominator", "fallback": "summary.json uses null for undefined ratios"})
        progress(state="completed" if complete else "partial_complete")
        write_report(folder, read(folder / "summary.json"), complete=complete)
    except Exception as exc:
        safe_error = str(exc) if isinstance(exc, RuntimeError) and re.fullmatch(r"[A-Za-z0-9_]+", str(exc)) else type(exc).__name__
        progress(state="stopped", error=safe_error)
        report_path = folder / "REPORT.md"
        old_report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        report_path.write_text(f"# 考试已停止\n\n原因：`{safe_error}`。已有断点与用量已保留，尚未完成全部考试。\n\n" + old_report, encoding="utf-8")
        raise SystemExit(1) from None
    finally:
        runtime.shutdown()
        api.client.close()
        process_lock.seek(0)
        msvcrt.locking(process_lock.fileno(), msvcrt.LK_UNLCK, 1)
        process_lock.close()


def write_report(folder, summary, *, complete):
    def percent(value):
        return "不适用" if value is None else f"{value:.2%}"
    state = "首位用户完整考试完成" if complete else "考试进行中，以下为已完成会话的临时成绩"
    text = f"""# MemoWeft × MiMo / HaluMem 首轮评测

{state}。

- 范围：Halu-Medium 第 1 位用户，{summary['sessions_completed']}/{summary['sessions_total']} 个会话。
- 固定内核：`{CORE_SHA}`；HaluMem：`{HALU_SHA}`。
- 记忆形成、答题和判卷：`{MODEL}`，关闭思考，温度 0。
- 此成绩是单用户试考，且使用 MiMo 判卷，不是官方同配置排行榜成绩。

| 指标 | 成绩 |
|---|---:|
| 记忆提取召回率 | {percent(summary['extraction_recall'])} |
| 目标记忆精确率 | {percent(summary['target_precision'])} |
| 提取 F1 | {percent(summary['extraction_f1'])} |
| 抵抗虚假记忆 | {percent(summary['false_memory_resistance'])} |
| 更新正确率 | {percent(summary['update']['correct'])} |
| 问答正确率 | {percent(summary['qa']['correct'])} |
| 问答幻觉率 | {percent(summary['qa']['hallucination'])} |
| 问答遗漏率 | {percent(summary['qa']['omission'])} |

已判问答 {summary['qa']['count']} 题；无效判卷 {summary['invalid_judgements']} 条。
累计 API（模型接口）调用 {summary['usage'].get('calls',0)} 次，输入 {summary['usage'].get('prompt_tokens',0):,}、输出 {summary['usage'].get('completion_tokens',0):,} 词元。
其中命中缓存的输入 {summary['usage'].get('cached_tokens',0):,} 词元。实际金额以 MiMo 账单为准。

## 方法与限制

使用未修改的 MemoWeft Python 内核，逐轮摄取，等待形成结束；每个会话结束立即考试，再读取下个会话。
只把对话送入内核，不把人物设定、标准记忆、答案写入记忆。用户和助手角色保持原样。
按会话前后正式记忆差异导出提取结果；不把原始证据冒充提取结果；排除仅用于实体登记的名字。
召回使用正式产品接口，等待语义模型与索引就绪，保留产品原有数量限制和输出截断，不用临时原文补答案。
问答提示使用官方 PROMPT_MEMZERO；判卷提示与分类规则使用官方 eval_tools.py/evaluation.py。
官方更新分桶规则保留：更新检索为空的条目归入提取完整性，另可从逐条结果检查漏检索。
同模型答题与判卷可能存在偏差；判卷失败保留为无效记录，不悄悄删除。

可复核文件：manifest.json、status.json、summary.json、records.json、usage.jsonl、memoweft_eval_results.jsonl 以及 sessions/。
全部工作位于独立评测目录，没有修改现有 MemoWeft/WeftMate 项目或日用数据。
"""
    (folder / "REPORT.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
