"""Weekly report composition.

The report is assembled from three sources, and the UI shows which one each
line came from:

1. **seekdb** – the原文 chunks inside the requested week, so every bullet can
   be traced back to a document, a date and a chunk id.
2. **seekdb tasks** – extracted to-dos with owner / due date / status.
3. **PowerContext** – the standing preferences, decisions and last week's
   conclusions, recalled through ``/v1/context/prepare``.

Nothing is invented: when a section has no evidence it says so explicitly.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import db, extract, pc


async def window_documents(project: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        SELECT doc_id, doc_type, title, source_name, author, doc_date, chunk_count
        FROM documents
        WHERE project=%s AND doc_date BETWEEN %s AND %s
        ORDER BY doc_date ASC
        """,
        (project, start, end),
    )


async def window_chunks(project: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    return await db.fetch_all(
        """
        SELECT c.chunk_id, c.doc_id, c.doc_type, c.doc_date, c.seq, c.chunk_text,
               d.title
        FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.project=%s AND c.doc_date BETWEEN %s AND %s
        ORDER BY c.doc_date ASC, c.doc_id ASC, c.seq ASC
        """,
        (project, start, end),
    )


def _day(value: Any) -> str:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%m-%d")
    return str(value)


def _pick_progress(chunks: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """Pick progress sentences round-robin across documents.

    Round-robin keeps the report representative: without it the first long
    document of the week fills every bullet and later documents never show up.
    """
    by_doc: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for chunk in chunks:
        bucket = by_doc.setdefault(chunk["doc_id"], [])
        for sentence in extract.sentences(chunk["chunk_text"]):
            if not extract.is_progress_sentence(sentence):
                continue
            cleaned = extract.clean_sentence(sentence).rstrip("。；; ")
            key = extract.normalize(cleaned)[:36]
            if key in seen or cleaned == chunk["title"]:
                continue
            seen.add(key)
            if len(bucket) >= 3:
                break
            bucket.append(
                {
                    "text": cleaned[:220],
                    "doc_id": chunk["doc_id"],
                    "doc_title": chunk["title"],
                    "doc_type": chunk["doc_type"],
                    "doc_date": _day(chunk["doc_date"]),
                    "chunk_id": chunk["chunk_id"],
                }
            )
    picked: list[dict[str, Any]] = []
    for round_index in range(3):
        for items in by_doc.values():
            if round_index < len(items):
                picked.append(items[round_index])
                if len(picked) >= limit:
                    return picked
    return picked


def _pick_risks(chunks: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    for chunk in chunks:
        for sentence in extract.extract_risks(chunk["chunk_text"]):
            if sentence.startswith(("结论", "决定", "评审通过")):
                continue
            key = extract.normalize(sentence)[:32]
            if key in seen:
                continue
            seen.add(key)
            picked.append(
                {
                    "text": sentence,
                    "doc_title": chunk["title"],
                    "doc_date": _day(chunk["doc_date"]),
                    "chunk_id": chunk["chunk_id"],
                }
            )
            if len(picked) >= limit:
                return picked
    return picked


STATUS_LABEL = {"todo": "待开始", "doing": "进行中", "done": "已完成", "blocked": "阻塞"}


def compose_markdown(
    *,
    project: str,
    start: dt.date,
    end: dt.date,
    documents: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    progress: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    memory: dict[str, Any],
    search_count: int,
) -> str:
    doc_kinds: dict[str, int] = {}
    for doc in documents:
        doc_kinds[doc["doc_type"]] = doc_kinds.get(doc["doc_type"], 0) + 1
    kind_text = " / ".join(f"{k} {v}" for k, v in doc_kinds.items()) or "无"
    memory_entries = memory.get("entries", []) if isinstance(memory, dict) else []

    lines: list[str] = []
    lines.append(f"# {project} · 周报（{start:%Y-%m-%d} ~ {end:%Y-%m-%d}）")
    lines.append("")
    lines.append(
        f"> WorkPulse 自动汇总 · 本周资料 {len(documents)} 份（{kind_text}）· "
        f"原文引用 {len(progress) + len(risks)} 段 · "
        f"PowerContext 记忆 {len(memory_entries)} 条 · 检索记录 {search_count} 次"
    )
    lines.append("")

    lines.append("## 一、本周进展")
    lines.append("")
    if progress:
        for idx, item in enumerate(progress, start=1):
            lines.append(
                f"{idx}. {item['text']}"
                f"（`{item['doc_title']}` · {item['doc_date']} · chunk#{item['chunk_id']}）"
            )
    else:
        lines.append("_本周没有检索到可引用的进展原文，请补充会议纪要或聊天记录。_")
    lines.append("")

    lines.append("## 二、风险与阻塞")
    lines.append("")
    risk_lines = [f"- {item['text']}（`{item['doc_title']}` · {item['doc_date']}）" for item in risks]
    risk_keys = {extract.normalize(item["text"])[:24] for item in risks}
    for task in tasks:
        if task["status"] != "blocked":
            continue
        key = extract.normalize(task["title"])[:24]
        if key in risk_keys:
            continue
        risk_keys.add(key)
        risk_lines.append(
            f"- 任务阻塞：{task['title']}（负责人 {task['owner'] or '未指定'} · chunk#{task['source_chunk'] or '-'}）"
        )
    lines.extend(risk_lines[:8] if risk_lines else ["_本周未识别到风险或阻塞项。_"])
    lines.append("")

    lines.append("## 三、下周计划")
    lines.append("")
    open_tasks = [t for t in tasks if t["status"] in ("todo", "doing")]
    open_tasks.sort(key=lambda t: (t.get("due_date") or "9999-12-31", t["title"]))
    if open_tasks:
        lines.append("| 任务 | 负责人 | 截止 | 状态 | 备注 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for task in open_tasks[:10]:
            due = task.get("due_date") or "未定"
            note = ""
            if task.get("due_date") and task["due_date"] < end.isoformat():
                note = "已逾期"
            lines.append(
                f"| {task['title'][:40]} | {task['owner'] or '待定'} | {due} | "
                f"{STATUS_LABEL.get(task['status'], task['status'])} | {note} |"
            )
    else:
        lines.append("_本周没有待跟进的任务。_")
    lines.append("")

    done_tasks = [t for t in tasks if t["status"] == "done"]
    lines.append("## 四、本周已完成")
    lines.append("")
    if done_tasks:
        for task in done_tasks[:10]:
            lines.append(f"- {task['title'][:80]}（{task['owner'] or '未指定'}）")
    else:
        lines.append("_本周没有标记为已完成的任务。_")
    lines.append("")

    lines.append("## 五、记忆延续（PowerContext）")
    lines.append("")
    ordered = sorted(
        memory_entries,
        key=lambda e: {"preference": 0, "decision": 1, "summary": 2, "progress": 3}.get(e.get("kind", ""), 4),
    )
    if ordered:
        for entry in ordered[:8]:
            kind = entry.get("kind", "note")
            text = (entry.get("text") or "").strip()
            if text:
                lines.append(f"- **[{kind}]** {text[:200]}")
    else:
        lines.append("_本次没有召回长期记忆，下周生成时会自动复用本周保存的结论。_")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("### 证据索引")
    lines.append("")
    seen_chunks: set[int] = set()
    for item in progress + risks:
        if item["chunk_id"] in seen_chunks:
            continue
        seen_chunks.add(item["chunk_id"])
        lines.append(f"- `chunk#{item['chunk_id']}` · {item['doc_title']} · {item['doc_date']}")
    return "\n".join(lines).strip() + "\n"


async def build_report(
    *,
    project: str,
    end: dt.date | None = None,
    days: int = 7,
) -> dict[str, Any]:
    end = end or dt.date.today()
    start = end - dt.timedelta(days=days - 1)

    documents = await window_documents(project, start, end)
    chunks = await window_chunks(project, start, end)
    tasks = await db.list_tasks(project)
    progress = _pick_progress(chunks)
    risks = _pick_risks(chunks)
    searches = await db.recent_searches(50)
    search_count = len([s for s in searches if s.get("project") == project])

    recall = await pc.prepare_context(project, f"周报 {project} 本周进展 风险 决策 偏好", max_bytes=6000)
    list_result = await pc.list_entries(project)
    entries = list_result.get("entries", [])[:8]
    memory = {
        "available": bool(recall.get("ok")),
        "status": recall.get("status", "unavailable"),
        "scope_id": recall.get("scope_id"),
        "recalled_text": (recall.get("content") or "")[:2000],
        "entries": entries,
        "error": recall.get("error"),
    }

    markdown = compose_markdown(
        project=project,
        start=start,
        end=end,
        documents=documents,
        chunks=chunks,
        tasks=tasks,
        progress=progress,
        risks=risks,
        memory=memory,
        search_count=search_count,
    )

    citations = [
        {
            "chunk_id": item["chunk_id"],
            "doc_id": item.get("doc_id", ""),
            "doc_title": item["doc_title"],
            "doc_date": item["doc_date"],
            "text": item["text"],
            "section": "progress",
        }
        for item in progress
    ] + [
        {
            "chunk_id": item["chunk_id"],
            "doc_id": "",
            "doc_title": item["doc_title"],
            "doc_date": item["doc_date"],
            "text": item["text"],
            "section": "risk",
        }
        for item in risks
    ]

    return {
        "project": project,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "markdown": markdown,
        "citations": citations,
        "memory": memory,
        "stats": {
            "documents": len(documents),
            "chunks": len(chunks),
            "progress_items": len(progress),
            "risks": len(risks),
            "open_tasks": len([t for t in tasks if t["status"] in ("todo", "doing")]),
            "done_tasks": len([t for t in tasks if t["status"] == "done"]),
            "searches": search_count,
        },
    }
