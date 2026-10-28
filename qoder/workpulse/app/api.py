"""HTTP API for WorkPulse."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field

from . import db, embed, extract, pc, report
from .config import settings

router = APIRouter(prefix="/api")

DOC_TYPES = {"minutes": "会议纪要", "chat": "聊天记录", "doc": "文档", "task": "任务清单", "note": "随手记"}


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
class IngestRequest(BaseModel):
    project: str = Field(min_length=1, max_length=60)
    doc_type: str = "minutes"
    title: str = Field(min_length=1, max_length=200)
    doc_date: str = ""
    source_name: str = ""
    author: str = ""
    text: str = Field(min_length=1)
    remember: bool = True


class MemoryRequest(BaseModel):
    project: str
    kind: str = "preference"
    text: str
    reason: str = ""


class TaskStatusRequest(BaseModel):
    status: str
    memo: str = ""


class ReportRequest(BaseModel):
    project: str
    week_end: str = ""
    days: int = 7
    save: bool = False
    remember: bool = True


class ResetRequest(BaseModel):
    confirm: str = ""


def _parse_date(value: str, fallback: dt.date | None = None) -> dt.date:
    fallback = fallback or dt.date.today()
    if not value:
        return fallback
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m-%d", "%m/%d"):
        try:
            parsed = dt.datetime.strptime(value, fmt).date()
            if fmt.startswith("%m"):
                parsed = parsed.replace(year=fallback.year)
            return parsed
        except ValueError:
            continue
    match = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$", value)
    if match:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return fallback


def _doc_id(project: str, title: str, doc_date: dt.date) -> str:
    raw = f"{project}|{title}|{doc_date.isoformat()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# --------------------------------------------------------------------------
# health / overview
# --------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict[str, Any]:
    seekdb_ok = True
    seekdb_error = ""
    version = ""
    try:
        row = await db.fetch_one("SELECT VERSION() AS v, 1 AS ok")
        version = row["v"] if row else ""
    except Exception as exc:  # pragma: no cover - depends on live service
        seekdb_ok = False
        seekdb_error = str(exc)[:300]
    memory = await pc.health()
    return {
        "ok": seekdb_ok,
        "seekdb": {
            "ok": seekdb_ok,
            "version": version,
            "host": f"{settings.seekdb_host}:{settings.seekdb_port}",
            "database": settings.seekdb_database,
            "error": seekdb_error,
        },
        "powercontext": memory,
        "embedding": embed.provider_label(),
    }


@router.get("/overview")
async def overview(project: str = "") -> dict[str, Any]:
    stats = await db.stats()
    projects = await db.list_projects()
    documents = await db.list_documents(project or None, limit=12)
    tasks = await db.list_tasks(project or None)
    searches = await db.recent_searches(12)
    memory = {"available": False, "entries": [], "scope_id": None}
    if project:
        listed = await pc.list_entries(project)
        memory = {
            "available": bool(listed.get("ok")),
            "entries": listed.get("entries", [])[:6],
            "scope_id": listed.get("scope_id"),
            "error": listed.get("error"),
        }
    return {
        "stats": stats,
        "projects": [
            {
                "project": row["project"],
                "docs": int(row["docs"] or 0),
                "chunks": int(row["chunks"] or 0),
                "last_date": str(row["last_date"] or ""),
            }
            for row in projects
        ],
        "documents": [_json_safe(doc) for doc in documents],
        "tasks": tasks,
        "searches": searches,
        "memory": memory,
        "doc_types": DOC_TYPES,
        "config": {
            "embedding": embed.provider_label(),
            "seekdb": f"{settings.seekdb_host}:{settings.seekdb_port}/{settings.seekdb_database}",
            "powercontext": settings.pc_url,
            "chunk_chars": settings.chunk_chars,
            "ft_weight": settings.ft_weight,
        },
    }


@router.get("/documents")
async def documents(project: str = "") -> dict[str, Any]:
    docs = await db.list_documents(project or None, limit=200)
    return {"documents": [_json_safe(doc) for doc in docs]}


@router.get("/projects")
async def projects() -> dict[str, Any]:
    rows = await db.list_projects()
    return {"projects": [row["project"] for row in rows]}


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------
@router.post("/ingest")
async def ingest(payload: IngestRequest) -> dict[str, Any]:
    return await _do_ingest(
        project=payload.project.strip(),
        doc_type=payload.doc_type,
        title=payload.title.strip(),
        doc_date=_parse_date(payload.doc_date),
        source_name=payload.source_name,
        author=payload.author,
        text=payload.text,
        remember=payload.remember,
    )


@router.post("/ingest/file")
async def ingest_file(
    project: str = Form(...),
    doc_type: str = Form("doc"),
    title: str = Form(""),
    doc_date: str = Form(""),
    remember: bool = Form(True),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"无法解码文件，请使用 UTF-8 或 GBK：{exc}") from exc
    if not text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")
    return await _do_ingest(
        project=project.strip(),
        doc_type=doc_type,
        title=(title or file.filename or "未命名文档").strip(),
        doc_date=_parse_date(doc_date),
        source_name=file.filename or "",
        author="",
        text=text,
        remember=remember,
    )


async def _do_ingest(
    *,
    project: str,
    doc_type: str,
    title: str,
    doc_date: dt.date,
    source_name: str,
    author: str,
    text: str,
    remember: bool,
) -> dict[str, Any]:
    if doc_type not in DOC_TYPES:
        doc_type = "doc"
    started = time.perf_counter()
    doc_id = _doc_id(project, title, doc_date)
    pieces = db.chunk_text(text)
    inserted = await db.insert_document(
        doc_id=doc_id,
        project=project,
        doc_type=doc_type,
        title=title,
        source_name=source_name,
        author=author,
        doc_date=doc_date,
        raw_text=text,
        chunks=pieces,
    )

    tasks = extract.extract_tasks(text=text, project=project, doc_id=doc_id, base_date=doc_date)
    chunks = await db.fetch_all(
        "SELECT chunk_id, chunk_text FROM chunks WHERE doc_id=%s ORDER BY seq", (doc_id,)
    )
    extract.attach_chunk_ids(tasks, chunks)
    for task in tasks:
        await db.upsert_task(task)

    decisions = extract.extract_decisions(text=text, project=project, doc_id=doc_id, base_date=doc_date)
    memory_writes: list[dict[str, Any]] = []
    if remember:
        for decision in decisions[:3]:
            result = await pc.remember(
                project,
                "decision",
                f"[{title}] {decision['text']}",
                reason=f"来自《{title}》的决策自动归档",
            )
            memory_writes.append({"kind": "decision", "ok": result.get("ok"), "text": decision["text"][:80]})
        if tasks:
            digest = "；".join(f"{t['title'][:40]}（{t['owner'] or '待定'}）" for t in tasks[:3])
            result = await pc.remember(
                project,
                "progress",
                f"[{title}] 新增待办 {len(tasks)} 项：{digest}",
                reason="导入资料时自动记录项目进展",
            )
            memory_writes.append({"kind": "progress", "ok": result.get("ok"), "text": digest[:80]})

    return {
        "doc_id": doc_id,
        "project": project,
        "title": title,
        "doc_date": doc_date.isoformat(),
        "chunks": inserted,
        "tasks": tasks,
        "decisions": decisions,
        "memory_writes": memory_writes,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str) -> dict[str, Any]:
    await db.delete_document(doc_id)
    return {"deleted": doc_id}


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------
@router.get("/search")
async def search(
    q: str,
    project: str,
    mode: str = "hybrid",
    ft_weight: float | None = None,
    doc_type: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 8,
) -> dict[str, Any]:
    if not q.strip():
        raise HTTPException(status_code=400, detail="查询不能为空")
    if mode not in ("hybrid", "fulltext", "vector"):
        mode = "hybrid"
    doc_types = [t.strip() for t in doc_type.split(",") if t.strip()] or None
    result = await db.hybrid_search(
        query=q.strip(),
        project=project,
        mode=mode,
        ft_weight=ft_weight,
        doc_types=doc_types,
        date_from=_parse_date(date_from) if date_from else None,
        date_to=_parse_date(date_to) if date_to else None,
        limit=max(1, min(30, limit)),
    )
    result["answer"] = _extractive_answer(q, result["hits"])
    return result


def _extractive_answer(query: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a grounded, extractive answer when no LLM is configured."""
    if not hits:
        return {"text": "没有检索到相关原文，可以换一个说法或先导入资料。", "citations": []}
    terms = [t for t in embed.tokens(query) if t.startswith(("w:", "b:", "t:"))]
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for hit in hits[:5]:
        best_sentence, best_score = "", -1
        for sentence in extract.sentences(hit["text"]):
            if len(sentence) < 8 or extract.is_heading(sentence):
                continue
            cleaned = extract.clean_sentence(sentence)
            score = sum(1 for term in set(terms) if term[2:] in extract.normalize(cleaned))
            if score > best_score:
                best_score, best_sentence = score, cleaned
        if best_sentence:
            scored.append((best_score, best_sentence, hit))
    scored.sort(key=lambda item: -item[0])
    top = [item for item in scored if item[0] > 0][:3] or scored[:2]
    lines = []
    for idx, (_, sentence, hit) in enumerate(top, start=1):
        lines.append(f"{idx}. {sentence}〔{hit['title']} · {hit['doc_date']} · chunk#{hit['chunk_id']}〕")
    return {
        "text": "\n".join(lines),
        "citations": [
            {
                "chunk_id": hit["chunk_id"],
                "doc_id": hit["doc_id"],
                "title": hit["title"],
                "doc_date": hit["doc_date"],
                "text": sentence,
                "score": hit["score"],
            }
            for _, sentence, hit in top
        ],
    }


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------
@router.get("/tasks")
async def get_tasks(project: str = "") -> dict[str, Any]:
    tasks = await db.list_tasks(project or None)
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return {"tasks": tasks, "counts": counts}


@router.post("/tasks/{task_id}/status")
async def set_task_status(task_id: str, payload: TaskStatusRequest) -> dict[str, Any]:
    if payload.status not in ("todo", "doing", "done", "blocked"):
        raise HTTPException(status_code=400, detail="状态只能是 todo/doing/done/blocked")
    await db.update_task_status(task_id, payload.status, payload.memo)
    task = await db.fetch_one("SELECT * FROM tasks WHERE task_id=%s", (task_id,))
    if task:
        result = await pc.remember(
            task["project"],
            "progress",
            f"任务「{task['title'][:60]}」状态更新为 {payload.status}"
            + (f"（{payload.memo}）" if payload.memo else ""),
            reason="任务看板状态变更",
        )
        return {"ok": True, "task_id": task_id, "memory_ok": result.get("ok")}
    return {"ok": True, "task_id": task_id, "memory_ok": False}


@router.post("/tasks/extract")
async def reextract(project: str) -> dict[str, Any]:
    chunks = await db.fetch_all(
        "SELECT chunk_id, doc_id, doc_date, chunk_text FROM chunks WHERE project=%s", (project,)
    )
    created = 0
    for chunk in chunks:
        tasks = extract.extract_tasks(
            text=chunk["chunk_text"],
            project=project,
            doc_id=chunk["doc_id"],
            base_date=chunk["doc_date"],
        )
        for task in tasks:
            task["source_chunk"] = chunk["chunk_id"]
            await db.upsert_task(task)
            created += 1
    return {"project": project, "tasks_seen": created}


# --------------------------------------------------------------------------
# memory (PowerContext)
# --------------------------------------------------------------------------
@router.get("/memory")
async def get_memory(project: str) -> dict[str, Any]:
    listed = await pc.list_entries(project)
    recalled = await pc.prepare_context(project, f"{project} 周报 偏好 决策", max_bytes=3000)
    return {
        "project": project,
        "available": bool(listed.get("ok")),
        "scope_id": listed.get("scope_id"),
        "entries": listed.get("entries", []),
        "recall": {
            "status": recalled.get("status"),
            "content": (recalled.get("content") or "")[:1500],
            "ok": bool(recalled.get("ok")),
        },
        "error": listed.get("error") or recalled.get("error"),
    }


@router.post("/memory")
async def post_memory(payload: MemoryRequest) -> dict[str, Any]:
    result = await pc.remember(payload.project, payload.kind, payload.text, payload.reason)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=f"PowerContext 写入失败：{result.get('error')}")
    return {"ok": True, "scope_id": result.get("scope_id"), "entry": result.get("body", {}).get("entry")}


@router.get("/memory/search")
async def search_memory(project: str, q: str, limit: int = 5) -> dict[str, Any]:
    result = await pc.search_memory(project, q, limit)
    return {
        "ok": bool(result.get("ok")),
        "hits": result.get("hits", []),
        "error": result.get("error"),
    }


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------
@router.post("/report")
async def create_report(payload: ReportRequest) -> dict[str, Any]:
    end = _parse_date(payload.week_end) if payload.week_end else dt.date.today()
    built = await report.build_report(project=payload.project, end=end, days=max(1, min(31, payload.days)))
    if payload.save:
        report_id = hashlib.sha1(
            f"{payload.project}|{built['week_start']}|{built['week_end']}".encode()
        ).hexdigest()[:16]
        await db.save_report(
            report_id=report_id,
            project=payload.project,
            week_start=dt.date.fromisoformat(built["week_start"]),
            week_end=dt.date.fromisoformat(built["week_end"]),
            content_md=built["markdown"],
            citations=built["citations"],
            memory=built["memory"],
        )
        built["report_id"] = report_id
        if payload.remember:
            summary = _summary_line(built["markdown"])
            writes = []
            writes.append(
                await pc.remember(
                    payload.project,
                    "summary",
                    f"[{built['week_start']}~{built['week_end']}] 周报结论：{summary}",
                    reason="生成周报后归档结论，供下周复用",
                )
            )
            open_tasks = [t for t in await db.list_tasks(payload.project) if t["status"] in ("todo", "doing")]
            if open_tasks:
                writes.append(
                    await pc.remember(
                        payload.project,
                        "progress",
                        "未完成待跟进：" + "；".join(f"{t['title'][:30]}（{t['owner'] or '待定'}）" for t in open_tasks[:4]),
                        reason="周报结转未完成项",
                    )
                )
            built["memory_writes"] = [{"ok": w.get("ok"), "status": w.get("status")} for w in writes]
    return built


def _summary_line(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(("1.", "- ")):
            return stripped[:180]
    return markdown.strip().splitlines()[0][:180] if markdown.strip() else ""


@router.get("/reports")
async def get_reports(project: str = "") -> dict[str, Any]:
    return {"reports": await db.list_reports(project or None)}


@router.get("/reports/{report_id}")
async def get_report(report_id: str) -> dict[str, Any]:
    row = await db.get_report(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="报告不存在")
    return row


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------
@router.get("/evidence")
async def evidence() -> dict[str, Any]:
    ddl = {}
    for table in ("chunks", "documents", "tasks"):
        try:
            ddl[table] = await db.table_ddl(table)
        except Exception as exc:  # pragma: no cover
            ddl[table] = f"error: {exc}"
    pc_log = settings.log_dir / "powercontext_calls.jsonl"
    calls: list[dict[str, Any]] = []
    if pc_log.is_file():
        for line in pc_log.read_text(encoding="utf-8").splitlines()[-25:]:
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    searches = await db.recent_searches(10)
    last_sql = await db.fetch_one("SELECT sql_text FROM search_log ORDER BY id DESC LIMIT 1")
    return {
        "ddl": ddl,
        "search_log": searches,
        "last_search_sql": (last_sql or {}).get("sql_text", ""),
        "powercontext_calls": calls,
        "stats": await db.stats(),
    }


@router.post("/admin/reset")
async def reset(payload: ResetRequest) -> dict[str, Any]:
    """Clear imported data so the demo can be replayed from scratch."""
    if payload.confirm != "reset":
        raise HTTPException(status_code=400, detail='需要 {"confirm": "reset"} 才执行')
    for table in ("chunks", "documents", "tasks", "reports", "search_log"):
        await db.execute(f"DELETE FROM {table}")
    # PowerContext memory deliberately survives a data reset: it is the long
    # term record, and the scope binding keeps the next run reusing it.
    return {
        "ok": True,
        "cleared": ["chunks", "documents", "tasks", "reports", "search_log"],
        "kept": ["PowerContext scope binding + memory entries"],
    }


@router.get("/meta")
async def meta() -> dict[str, Any]:
    return {
        "embedding": embed.provider_label(),
        "embedding_dim": settings.embedding_dim,
        "chunk_chars": settings.chunk_chars,
        "chunk_overlap": settings.chunk_overlap,
        "ft_weight": settings.ft_weight,
        "doc_types": DOC_TYPES,
        "seekdb": f"{settings.seekdb_host}:{settings.seekdb_port}/{settings.seekdb_database}",
        "powercontext": settings.pc_url,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value
