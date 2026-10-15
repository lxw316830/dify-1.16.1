"""项目查重主流程。

脚本负责确定性步骤（文本提取、分块、合并校验、检索、分批、判定、报告），
需要大模型判断的两步（信息提取、证据评估）写成待办交给 agent，agent 写回 JSON 后由脚本校验。

    python3 dedup.py prepare <可研文档> [--work 目录] [--project-name 名称]
    python3 dedup.py next <工作目录> [--all]
    python3 dedup.py submit <工作目录> <待办ID>
    python3 dedup.py status <工作目录>
    python3 dedup.py summary <目录>...
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import convert  # noqa: E402
import retrieval  # noqa: E402
import workflow_nodes as nodes  # noqa: E402
from docx_text import extract_docx_text  # noqa: E402

STATE_FILE = "state.json"
REQUIREMENT_TYPES = {"需求内容", "项目目标", "项目成果"}
SOURCE_TYPES = {"正文", "标题", "表格"}
MAX_QUOTES = 2
MIN_TEXT_CHARS = 1000


class DedupError(RuntimeError):
    pass


# ---------------------------------------------------------------- 文件读写

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_state(work: Path) -> Dict:
    path = work / STATE_FILE
    if not path.is_file():
        raise DedupError(f"{work} 不是查重工作目录（缺少 {STATE_FILE}），先运行 prepare")
    return read_json(path)


def save_state(work: Path, state: Dict) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(work / STATE_FILE, state)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 渲染待办输入

def render_extract_input(batch: Dict) -> str:
    parts = []
    for chunk in batch["chunks"]:
        parts.append(
            "<chunk>\n"
            f"chunk_id={chunk['chunk_id']}\n"
            f"section_path={chunk['section_path']}\n"
            f"extract_mode={chunk['extract_mode']}\n\n"
            f"{chunk['content']}\n"
            "</chunk>\n"
        )
    return "\n".join(parts)


def render_evaluate_input(batch: Dict) -> str:
    lines = [f"批次ID={batch['batch_id']}", ""]
    for item in batch["items"]:
        task = item["task"]
        lines += [
            "<task>",
            f"task_id={task['task_id']}",
            f"类型={task['query_type']}",
            f"分类={task['source_field']}",
            f"功能状态={task['function_status']}",
            f"新项目内容={task['new_content']}",
            "",
        ]
        for candidate in item["candidate_groups"]:
            lines += [
                "<candidate>",
                f"candidate_id={candidate['candidate_id']}",
                f"历史项目={candidate['source_project']}",
                f"历史文档={candidate['document_name']}",
                "",
            ]
            for index, hit in enumerate(candidate["hits"], start=1):
                lines += [f"片段{index}，知识库检索分={hit['retrieval_score']}", hit["matched_content"], ""]
            lines.append("</candidate>")
        lines += ["</task>", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- prepare

def cmd_prepare(args: argparse.Namespace) -> int:
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise DedupError(f"文件不存在：{source}")
    work = Path(args.work).expanduser().resolve() if args.work else Path.cwd() / "dedup-work" / source.stem
    if (work / STATE_FILE).exists() and not args.force:
        raise DedupError(f"{work} 已有查重进度。继续请运行 next；重新开始请加 --force")
    work.mkdir(parents=True, exist_ok=True)

    docx_path = convert.ensure_docx(source, work)
    text = extract_docx_text(docx_path)
    (work / "text.md").write_text(text, encoding="utf-8")
    if len(text.strip()) < MIN_TEXT_CHARS:
        preview = text.strip()[:80].replace("\n", " ")
        raise DedupError(f"正文只有 {len(text.strip())} 字，不像完整的可研报告（开头：{preview}）。请确认文档内容或转换结果：{docx_path}")

    chunking = nodes.chunk_document(text, args.project_name or "")
    write_json(work / "chunking.json", chunking)
    if not chunking["project_name"]:
        raise DedupError("未识别正式项目名称（文档前部没有“项目名称：xxx”）。请向用户确认项目名称后加 --project-name 重新运行")

    tasks: Dict[str, Dict] = {}
    for index, batch in enumerate(chunking["batches"], start=1):
        task_id = f"extract-{index:03d}"
        input_path = work / "extract" / f"batch_{index:03d}.input.md"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(render_extract_input(batch), encoding="utf-8")
        tasks[task_id] = {
            "kind": "extract",
            "batch_index": index - 1,
            "input": str(input_path.relative_to(work)),
            "output": f"extract/batch_{index:03d}.json",
            "status": "pending",
        }

    warnings = []
    if not chunking["expected_chunk_ids"]:
        warnings.append("没有识别到需求分析或项目内容章节，查重结论不可靠，请人工确认文档结构")

    state = {
        "source": str(source),
        "docx": str(docx_path),
        "project_name": chunking["project_name"],
        "is_design_development_project": chunking["is_design_development_project"],
        "stage": "extract",
        "tasks": tasks,
        "warnings": warnings,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_state(work, state)
    print(f"工作目录：{work}")
    print(f"项目名称：{chunking['project_name']}")
    print(f"分块 {len(chunking['expected_chunk_ids'])} 个，信息提取待办 {len(tasks)} 个，功能清单 {len(chunking['function_items'])} 项")
    for warning in warnings:
        print(f"警告：{warning}")
    print()
    return advance_and_report(work, show_all=False)


# ---------------------------------------------------------------- 自动推进

def pending_tasks(state: Dict, kind: Optional[str] = None) -> List[Tuple[str, Dict]]:
    return [
        (task_id, task)
        for task_id, task in sorted(state["tasks"].items())
        if task["status"] != "done" and (kind is None or task["kind"] == kind)
    ]


def advance(work: Path, state: Dict) -> None:
    while True:
        stage = state["stage"]
        if stage == "extract" and not pending_tasks(state, "extract"):
            run_merge(work, state)
        elif stage == "retrieve":
            if not run_retrieval(work, state):
                return
        elif stage == "evaluate" and not pending_tasks(state, "evaluate"):
            run_report(work, state)
        else:
            return
        save_state(work, state)


def run_merge(work: Path, state: Dict) -> None:
    chunking = read_json(work / "chunking.json")
    batch_results = [
        read_json(work / task["output"])
        for _, task in sorted(state["tasks"].items())
        if task["kind"] == "extract"
    ]
    merged = nodes.merge_extractions(
        batch_results,
        chunking["expected_chunk_ids"],
        chunking["must_extract_requirement_ids"],
        chunking["chunk_sources"],
        chunking["project_name"],
        chunking["is_design_development_project"],
        chunking["function_items"],
    )
    write_json(work / "merge.json", merged)
    if merged["error_msg"]:
        fail(work, state, merged["error_msg"])
        return

    split = nodes.build_tasks(merged["json_obj"])
    write_json(work / "tasks.json", split)
    state["stage"] = "retrieve"
    log(f"合并校验通过：需求任务 {len(split['xmxq'])} 条，内容/功能任务 {len(split['xmnr'])} 条")


def run_retrieval(work: Path, state: Dict) -> bool:
    split = read_json(work / "tasks.json")
    all_tasks = split["xmxq"] + split["xmnr"]
    hits_path = work / "retrieval" / "hits.json"
    config = retrieval.load_config()

    if config.get("backend") == "manual":
        queries_path = work / "retrieval" / "queries.json"
        if not queries_path.exists():
            write_json(queries_path, [{"index": i, "query": t["query"]} for i, t in enumerate(all_tasks)])
            state["tasks"]["retrieve"] = {
                "kind": "retrieve",
                "input": "retrieval/queries.json",
                "output": "retrieval/hits.json",
                "status": "pending",
            }
            save_state(work, state)
        if state["tasks"]["retrieve"]["status"] != "done":
            return False
        results = {row["index"]: row["hits"] for row in read_json(hits_path)["results"]}
        hits_per_task = [[retrieval.normalize_hit(hit) for hit in results.get(i, [])] for i in range(len(all_tasks))]
    else:
        log(f"检索知识库：{len(all_tasks)} 条查询（后端 {config.get('backend')}）")
        started = time.monotonic()
        hits_per_task = retrieval.retrieve_all([t["query"] for t in all_tasks], config)
        write_json(hits_path, {"results": [{"index": i, "hits": h} for i, h in enumerate(hits_per_task)]})
        log(f"检索完成，用时 {time.monotonic() - started:.0f} 秒")

    requirement_count = len(split["xmxq"])
    wrapped = [nodes.wrap_evidence(task, hits)["wrapped_results"] for task, hits in zip(all_tasks, hits_per_task)]
    xmxq_items = [item for group in wrapped[:requirement_count] for item in group]
    xmnr_items = [item for group in wrapped[requirement_count:] for item in group]
    candidates = nodes.batch_candidates(xmxq_items, xmnr_items)
    write_json(work / "candidates.json", candidates)

    for index, batch in enumerate(candidates["batches"], start=1):
        task_id = f"evaluate-{index:03d}"
        input_path = work / "evaluate" / f"batch_{index:03d}.input.md"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(render_evaluate_input(batch), encoding="utf-8")
        state["tasks"][task_id] = {
            "kind": "evaluate",
            "batch_index": index - 1,
            "input": str(input_path.relative_to(work)),
            "output": f"evaluate/batch_{index:03d}.json",
            "status": "pending",
        }
    state["stage"] = "evaluate"
    log(f"候选证据 {candidates['candidate_item_count']} 条，分成 {candidates['batch_count']} 个评估批次")
    return True


def run_report(work: Path, state: Dict) -> None:
    candidates = read_json(work / "candidates.json")
    batch_results = []
    for _, task in sorted(state["tasks"].items()):
        if task["kind"] != "evaluate":
            continue
        batch = candidates["batches"][task["batch_index"]]
        batch_results.append(nodes.wrap_assessment(batch, read_json(work / task["output"]))["evaluated_results"])
    ranked = nodes.rank_evidence(batch_results)
    write_json(work / "evidence.json", ranked)
    report = nodes.render_report(ranked["ranked_projects"], ranked["all_evidence"])["text"]
    (work / "report.md").write_text(report, encoding="utf-8")
    state["stage"] = "done"
    state["conclusion"] = "重复" if any(item.get("is_duplicate") for item in ranked["all_evidence"]) else "不重复"


def fail(work: Path, state: Dict, message: str) -> None:
    state["stage"] = "failed"
    state["error"] = message
    (work / "report.md").write_text(
        f"# 查重未完成\n\n- 源文件：{state['source']}\n- 说明：{message}\n", encoding="utf-8"
    )


# ---------------------------------------------------------------- 校验 agent 输出

def validate_extract(work: Path, task: Dict, output: Dict) -> List[str]:
    chunking = read_json(work / "chunking.json")
    batch = chunking["batches"][task["batch_index"]]
    chunks = {chunk["chunk_id"]: chunk for chunk in batch["chunks"]}
    must_extract = set(chunking["must_extract_requirement_ids"]) & set(chunks)
    errors: List[str] = []

    for key in ("processed_chunk_ids", "requirements", "contents"):
        if not isinstance(output.get(key), list):
            errors.append(f"缺少数组字段 {key}")
    if errors:
        return errors

    missing = [chunk_id for chunk_id in chunks if chunk_id not in output["processed_chunk_ids"]]
    if missing:
        errors.append(f"processed_chunk_ids 缺少：{'、'.join(missing)}")
    unknown = [chunk_id for chunk_id in output["processed_chunk_ids"] if chunk_id not in chunks]
    if unknown:
        errors.append(f"processed_chunk_ids 含有本批次之外的分块：{'、'.join(map(str, unknown))}")

    requirement_chunks = set()
    for field, mode in (("requirements", "requirement"), ("contents", "project_content")):
        for index, item in enumerate(output[field]):
            where = f"{field}[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{where} 不是对象")
                continue
            chunk = chunks.get(item.get("chunk_id"))
            if chunk is None:
                errors.append(f"{where} 的 chunk_id={item.get('chunk_id')} 不在本批次")
                continue
            if chunk["extract_mode"] != mode:
                errors.append(f"{where} 来自 extract_mode={chunk['extract_mode']} 的分块 {chunk['chunk_id']}，不能放进 {field}")
            if item.get("source_type") not in SOURCE_TYPES:
                errors.append(f"{where} 的 source_type 只能是 正文/标题/表格")
            if not isinstance(item.get("table_name", ""), str):
                errors.append(f"{where} 的 table_name 必须是字符串")
            if field == "requirements":
                if item.get("requirement_type") not in REQUIREMENT_TYPES:
                    errors.append(f"{where} 的 requirement_type 只能是 需求内容/项目目标/项目成果")
                requirement_chunks.add(chunk["chunk_id"])
            content = str(item.get("content") or "").strip()
            if not content:
                errors.append(f"{where} 的 content 为空")
            elif not nodes.recover_exact_text(content, chunk["content"]):
                errors.append(f"{where} 不是分块 {chunk['chunk_id']} 的连续原文：{content[:40]}…")

    for chunk_id in sorted(must_extract - requirement_chunks):
        if not nodes.should_recover_requirement_summary(chunks[chunk_id]):
            errors.append(f"分块 {chunk_id} 是需求分块，至少要提取一条 requirements")
    return errors


def validate_evaluate(work: Path, task: Dict, output: Dict) -> Tuple[List[str], List[str]]:
    batch = read_json(work / "candidates.json")["batches"][task["batch_index"]]
    candidates: Dict[Tuple[str, str], Dict] = {}
    for item in batch["items"]:
        for candidate in item["candidate_groups"]:
            candidates[(item["task"]["task_id"], candidate["candidate_id"])] = candidate
    errors: List[str] = []
    warnings: List[str] = []

    rows = output.get("results")
    if not isinstance(rows, list):
        return ["缺少数组字段 results"], warnings

    seen = set()
    for index, row in enumerate(rows):
        where = f"results[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{where} 不是对象")
            continue
        key = (str(row.get("task_id")), str(row.get("candidate_id")))
        candidate = candidates.get(key)
        if candidate is None:
            errors.append(f"{where} 的 task_id/candidate_id={key[0]}/{key[1]} 不在本批次")
            continue
        if key in seen:
            errors.append(f"{where} 重复评估了 {key[0]}/{key[1]}")
        seen.add(key)
        if not isinstance(row.get("is_relevant"), bool):
            errors.append(f"{where} 的 is_relevant 必须是 true/false")
        score = row.get("similarity_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
            errors.append(f"{where} 的 similarity_score 必须是 0 到 100 的数字")
        if not isinstance(row.get("reason"), str):
            errors.append(f"{where} 缺少字符串字段 reason")
        quotes = row.get("evidence_quotes")
        if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
            errors.append(f"{where} 的 evidence_quotes 必须是字符串数组")
            continue
        if len(quotes) > MAX_QUOTES:
            errors.append(f"{where} 的 evidence_quotes 最多 {MAX_QUOTES} 条")
        if row.get("is_relevant") is False and (quotes or score):
            warnings.append(f"{where} 标记为不相关，但给了分数或证据，判定时会按不相关处理")
        hit_texts = [hit["matched_content"] for hit in candidate["hits"]]
        for quote in quotes:
            if not any(quote.strip() and (quote.strip() in text or nodes.recover_exact_text(quote, text)) for text in hit_texts):
                errors.append(f"{where} 的证据不是历史片段原文：{quote[:40]}…")

    missing = [f"{t}/{c}" for (t, c) in candidates if (t, c) not in seen]
    if missing:
        errors.append(f"缺少以下 task_id/candidate_id 的评估：{'、'.join(missing)}")
    return errors, warnings


def validate_retrieve(work: Path, output: Dict) -> List[str]:
    queries = read_json(work / "retrieval" / "queries.json")
    rows = output.get("results")
    if not isinstance(rows, list):
        return ["缺少数组字段 results"]
    errors = []
    indexes = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("hits"), list):
            errors.append(f"结果格式不对：{str(row)[:60]}")
            continue
        indexes.add(row.get("index"))
        for hit in row["hits"]:
            if not isinstance(hit, dict) or not hit.get("content") or not (hit.get("document_name") or hit.get("title")):
                errors.append(f"index={row.get('index')} 的命中缺少 content 或 document_name")
    missing = [str(q["index"]) for q in queries if q["index"] not in indexes]
    if missing:
        errors.append(f"缺少以下查询的结果（没有命中也要写空数组）：{'、'.join(missing)}")
    return errors


def cmd_submit(args: argparse.Namespace) -> int:
    work = Path(args.work).expanduser().resolve()
    state = load_state(work)
    task = state["tasks"].get(args.task_id)
    if task is None:
        raise DedupError(f"没有待办 {args.task_id}。运行 status 查看全部待办")
    output_path = work / task["output"]
    if not output_path.is_file():
        raise DedupError(f"还没有写入结果文件：{output_path}")
    try:
        output = read_json(output_path)
    except json.JSONDecodeError as exc:
        print(f"校验失败：{output_path} 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）")
        return 1
    if not isinstance(output, dict):
        print("校验失败：结果必须是 JSON 对象")
        return 1

    warnings: List[str] = []
    if task["kind"] == "extract":
        errors = validate_extract(work, task, output)
    elif task["kind"] == "evaluate":
        errors, warnings = validate_evaluate(work, task, output)
    else:
        errors = validate_retrieve(work, output)

    for warning in warnings:
        print(f"提示：{warning}")
    if errors:
        task["attempts"] = task.get("attempts", 0) + 1
        save_state(work, state)
        print(f"校验失败（第 {task['attempts']} 次），请修改 {output_path} 后重新提交：")
        for error in errors[:30]:
            print(f"- {error}")
        if len(errors) > 30:
            print(f"- ……另有 {len(errors) - 30} 条")
        return 1

    task["status"] = "done"
    save_state(work, state)
    print(f"{args.task_id} 校验通过")
    return advance_and_report(work, show_all=False)


# ---------------------------------------------------------------- next / status / summary

def describe_task(work: Path, task_id: str, task: Dict) -> str:
    guide = {
        "extract": SKILL_DIR / "extraction.md",
        "evaluate": SKILL_DIR / "evaluation.md",
        "retrieve": SKILL_DIR / "knowledge-base.md",
    }[task["kind"]]
    label = {"extract": "信息提取", "evaluate": "证据评估", "retrieve": "知识库检索"}[task["kind"]]
    return "\n".join([
        f"[{task_id}] {label}",
        f"  读取：{work / task['input']}",
        f"  规则：{guide}",
        f"  写入：{work / task['output']}",
        f"  提交：python3 {SCRIPT_DIR / 'dedup.py'} submit {work} {task_id}",
    ])


def advance_and_report(work: Path, show_all: bool) -> int:
    state = load_state(work)
    try:
        advance(work, state)
    except retrieval.RetrievalError as exc:
        save_state(work, state)
        print(f"检索失败：{exc}\n修复知识库配置或网络后重新运行 next 即可继续")
        return 1
    state = load_state(work)

    if state["stage"] == "done":
        print(f"查重完成：{state['conclusion']}")
        print(f"报告：{work / 'report.md'}")
        for warning in state.get("warnings", []):
            print(f"警告：{warning}")
        return 0
    if state["stage"] == "failed":
        print(f"查重未完成：{state['error']}")
        print(f"说明已写入：{work / 'report.md'}")
        return 1

    pending = pending_tasks(state)
    kinds = {task["kind"] for _, task in pending}
    total = {kind: sum(1 for t in state["tasks"].values() if t["kind"] == kind) for kind in kinds}
    done = {kind: sum(1 for t in state["tasks"].values() if t["kind"] == kind and t["status"] == "done") for kind in kinds}
    summary = "，".join(f"{kind} 已完成 {done[kind]}/{total[kind]}" for kind in sorted(kinds))
    print(f"阶段：{state['stage']}（{summary}）")
    print("待办：" if show_all else "下一个待办：")
    for task_id, task in pending if show_all else pending[:1]:
        print(describe_task(work, task_id, task))
    if not show_all and len(pending) > 1:
        print(f"（还有 {len(pending) - 1} 个同类待办，互不依赖，可并行处理；用 next --all 查看全部）")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    return advance_and_report(Path(args.work).expanduser().resolve(), show_all=args.all)


def cmd_status(args: argparse.Namespace) -> int:
    work = Path(args.work).expanduser().resolve()
    state = load_state(work)
    print(f"源文件：{state['source']}")
    print(f"项目名称：{state['project_name']}")
    print(f"阶段：{state['stage']}")
    for task_id, task in sorted(state["tasks"].items()):
        attempts = f"，校验失败 {task['attempts']} 次" if task.get("attempts") else ""
        print(f"  {task_id}: {task['status']}{attempts}")
    for warning in state.get("warnings", []):
        print(f"警告：{warning}")
    return 0


def find_work_dirs(paths: Sequence[str]) -> List[Path]:
    found: List[Path] = []
    for raw in paths:
        root = Path(raw).expanduser().resolve()
        candidates = [root] if (root / STATE_FILE).is_file() else sorted(p.parent for p in root.glob(f"*/{STATE_FILE}"))
        found.extend(candidates)
    return found


def cmd_summary(args: argparse.Namespace) -> int:
    rows = []
    for work in find_work_dirs(args.paths):
        state = read_json(work / STATE_FILE)
        top = ""
        if state["stage"] == "done" and (work / "evidence.json").is_file():
            ranked = read_json(work / "evidence.json")["ranked_projects"][:3]
            top = "；".join(f"{p['project_name']}（{p['duplicate_count']} 条）" for p in ranked)
        result = state.get("conclusion") or {"failed": "未完成"}.get(state["stage"], f"进行中（{state['stage']}）")
        note = state.get("error") or "；".join(state.get("warnings", []))
        rows.append((Path(state["source"]).name, result, top, note, str(work / "report.md")))
    if not rows:
        print("没有找到查重工作目录")
        return 1
    print("|文档|结论|主要重复历史项目|说明|报告|")
    print("|---|---|---|---|---|")
    for row in rows:
        print("|" + "|".join(cell.replace("|", "｜") for cell in row) + "|")
    counts = {}
    for row in rows:
        counts[row[1]] = counts.get(row[1], 0) + 1
    print()
    print("，".join(f"{key} {value} 篇" for key, value in counts.items()))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="项目查重主流程")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="提取文本、分块，生成信息提取待办")
    p.add_argument("file", help="可研文档（docx/doc/wps 等）")
    p.add_argument("--work", help="工作目录，默认 ./dedup-work/<文件名>")
    p.add_argument("--project-name", help="文档里识别不到项目名称时手动指定")
    p.add_argument("--force", action="store_true", help="覆盖已有进度重新开始")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("next", help="推进自动步骤并显示下一个待办")
    p.add_argument("work")
    p.add_argument("--all", action="store_true", help="显示全部待办")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("submit", help="校验并提交一个待办的结果")
    p.add_argument("work")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status", help="查看进度")
    p.add_argument("work")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("summary", help="汇总多个工作目录的结论")
    p.add_argument("paths", nargs="+", help="工作目录或其上级目录")
    p.set_defaults(func=cmd_summary)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
