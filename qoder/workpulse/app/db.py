"""seekdb access layer.

Everything the product knows lives in one seekdb database:

* ``documents``  – one row per imported file / chat export / meeting note
* ``chunks``     – retrieval unit, with a FULLTEXT index (ik parser) and a
                   HNSW VECTOR index in the same table
* ``tasks``      – extracted to-dos with owner, due date and status
* ``reports``    – generated weekly reports
* ``search_log`` – every retrieval call, kept as verifiable evidence
* ``meta``       – small key/value store (PowerContext scope ids, schema flag)

The hybrid query is a single SQL statement that combines the full-text score,
the vector distance and the structured filters, which is exactly the capability
seekdb is chosen for here.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any, Iterable

import aiomysql

from . import embed
from .config import settings

SCHEMA_VERSION = "1"

_pool: aiomysql.Pool | None = None


# --------------------------------------------------------------------------
# connection handling
# --------------------------------------------------------------------------
async def _connect(**kwargs: Any) -> aiomysql.Connection:
    return await aiomysql.connect(
        host=settings.seekdb_host,
        port=settings.seekdb_port,
        user=settings.seekdb_user,
        password=settings.seekdb_password,
        charset="utf8mb4",
        autocommit=True,
        **kwargs,
    )


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    tmp = await _connect()
    try:
        async with tmp.cursor() as cur:
            await cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.seekdb_database}` "
                "DEFAULT CHARACTER SET utf8mb4"
            )
    finally:
        tmp.close()
    _pool = await aiomysql.create_pool(
        host=settings.seekdb_host,
        port=settings.seekdb_port,
        user=settings.seekdb_user,
        password=settings.seekdb_password,
        db=settings.seekdb_database,
        charset="utf8mb4",
        autocommit=True,
        minsize=1,
        maxsize=8,
        pool_recycle=1800,
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


def pool() -> aiomysql.Pool:
    if _pool is None:
        raise RuntimeError("seekdb pool is not initialised")
    return _pool


async def fetch_all(sql: str, args: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    async with pool().acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, tuple(args) if args else None)
            return list(await cur.fetchall())


async def execute(sql: str, args: Iterable[Any] | None = None) -> int:
    async with pool().acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(args) if args else None)
            return cur.rowcount


async def fetch_one(sql: str, args: Iterable[Any] | None = None) -> dict[str, Any] | None:
    rows = await fetch_all(sql, args)
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
async def ensure_schema() -> None:
    dim = settings.embedding_dim
    await execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
          doc_id       VARCHAR(64)  NOT NULL,
          project      VARCHAR(64)  NOT NULL,
          doc_type     VARCHAR(32)  NOT NULL,
          title        VARCHAR(255) NOT NULL,
          source_name  VARCHAR(255) NOT NULL DEFAULT '',
          author       VARCHAR(64)  NOT NULL DEFAULT '',
          doc_date     DATE         NOT NULL,
          raw_text     LONGTEXT     NOT NULL,
          chunk_count  INT          NOT NULL DEFAULT 0,
          created_at   DATETIME     NOT NULL,
          PRIMARY KEY (doc_id),
          KEY idx_doc_project (project, doc_date)
        ) ORGANIZATION = HEAP
        """
    )
    await execute(
        f"""
        CREATE TABLE IF NOT EXISTS chunks (
          chunk_id   BIGINT       NOT NULL AUTO_INCREMENT,
          doc_id     VARCHAR(64)  NOT NULL,
          project    VARCHAR(64)  NOT NULL,
          doc_type   VARCHAR(32)  NOT NULL,
          doc_date   DATE         NOT NULL,
          seq        INT          NOT NULL,
          chunk_text TEXT         NOT NULL,
          embedding  VECTOR({dim}),
          PRIMARY KEY (chunk_id),
          FULLTEXT INDEX ft_chunk (chunk_text) WITH PARSER ik,
          VECTOR INDEX vec_chunk (embedding) WITH (DISTANCE=cosine, TYPE=hnsw, LIB=vsag),
          KEY idx_chunk_filter (project, doc_type, doc_date)
        ) ORGANIZATION = HEAP
        """
    )
    await execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
          task_id      VARCHAR(64)  NOT NULL,
          project      VARCHAR(64)  NOT NULL,
          title        VARCHAR(255) NOT NULL,
          owner        VARCHAR(64)  NOT NULL DEFAULT '',
          status       VARCHAR(16)  NOT NULL DEFAULT 'todo',
          priority     VARCHAR(8)   NOT NULL DEFAULT 'P2',
          due_date     DATE         NULL,
          source_doc_id VARCHAR(64) NOT NULL DEFAULT '',
          source_chunk BIGINT       NULL,
          evidence     TEXT         NULL,
          memo         VARCHAR(512) NOT NULL DEFAULT '',
          created_at   DATETIME     NOT NULL,
          updated_at   DATETIME     NOT NULL,
          PRIMARY KEY (task_id),
          KEY idx_task_project (project, status)
        ) ORGANIZATION = HEAP
        """
    )
    await execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
          report_id      VARCHAR(64) NOT NULL,
          project        VARCHAR(64) NOT NULL,
          week_start     DATE        NOT NULL,
          week_end       DATE        NOT NULL,
          content_md     LONGTEXT    NOT NULL,
          citations_json LONGTEXT    NULL,
          memory_json    LONGTEXT    NULL,
          created_at     DATETIME    NOT NULL,
          PRIMARY KEY (report_id)
        ) ORGANIZATION = HEAP
        """
    )
    await execute(
        """
        CREATE TABLE IF NOT EXISTS search_log (
          id         BIGINT       NOT NULL AUTO_INCREMENT,
          ts         DATETIME     NOT NULL,
          project    VARCHAR(64)  NOT NULL DEFAULT '',
          query      VARCHAR(512) NOT NULL DEFAULT '',
          mode       VARCHAR(16)  NOT NULL DEFAULT 'hybrid',
          latency_ms INT          NOT NULL DEFAULT 0,
          hits       INT          NOT NULL DEFAULT 0,
          sql_text   LONGTEXT     NULL,
          PRIMARY KEY (id)
        ) ORGANIZATION = HEAP
        """
    )
    await execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
          k          VARCHAR(64)  NOT NULL,
          v          VARCHAR(512) NOT NULL,
          updated_at DATETIME     NOT NULL,
          PRIMARY KEY (k)
        ) ORGANIZATION = HEAP
        """
    )
    await set_meta("schema_version", SCHEMA_VERSION)


async def set_meta(key: str, value: str) -> None:
    await execute(
        "INSERT INTO meta(k, v, updated_at) VALUES(%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at)",
        (key, value, dt.datetime.now()),
    )


async def get_meta(key: str) -> str | None:
    row = await fetch_one("SELECT v FROM meta WHERE k=%s", (key,))
    return row["v"] if row else None


# --------------------------------------------------------------------------
# documents & chunks
# --------------------------------------------------------------------------
def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split text on paragraph/sentence boundaries, never mid-sentence."""
    size = size or settings.chunk_chars
    overlap = overlap or settings.chunk_overlap
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    buf = ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        for sentence in _split_sentences(para):
            if len(buf) + len(sentence) + 1 <= size:
                buf = f"{buf}\n{sentence}".strip()
            else:
                if buf:
                    pieces.append(buf)
                if len(sentence) <= size:
                    buf = sentence
                else:
                    for start in range(0, len(sentence), size - overlap):
                        pieces.append(sentence[start : start + size])
                    buf = ""
    if buf:
        pieces.append(buf)
    return [p for p in pieces if p.strip()]


def _split_sentences(para: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in para:
        buf += ch
        if ch in "。！？!?；;":
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


async def insert_document(
    *,
    doc_id: str,
    project: str,
    doc_type: str,
    title: str,
    source_name: str,
    author: str,
    doc_date: dt.date,
    raw_text: str,
    chunks: list[str],
) -> int:
    vectors = embed.embed_many(chunks)
    now = dt.datetime.now()
    await execute(
        """
        INSERT INTO documents(doc_id, project, doc_type, title, source_name, author,
                              doc_date, raw_text, chunk_count, created_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE title=VALUES(title), raw_text=VALUES(raw_text),
          chunk_count=VALUES(chunk_count), doc_date=VALUES(doc_date)
        """,
        (doc_id, project, doc_type, title, source_name, author, doc_date, raw_text, len(chunks), now),
    )
    await execute("DELETE FROM chunks WHERE doc_id=%s", (doc_id,))
    if not chunks:
        return 0
    rows = [
        (
            doc_id,
            project,
            doc_type,
            doc_date,
            seq,
            text,
            embed.vector_literal(vec),
        )
        for seq, (text, vec) in enumerate(zip(chunks, vectors))
    ]
    sql = (
        "INSERT INTO chunks(doc_id, project, doc_type, doc_date, seq, chunk_text, embedding) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s)"
    )
    async with pool().acquire() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)
    return len(rows)


async def list_documents(project: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if project:
        return await fetch_all(
            "SELECT doc_id, project, doc_type, title, source_name, author, doc_date, chunk_count, created_at "
            "FROM documents WHERE project=%s ORDER BY doc_date DESC, created_at DESC LIMIT %s",
            (project, limit),
        )
    return await fetch_all(
        "SELECT doc_id, project, doc_type, title, source_name, author, doc_date, chunk_count, created_at "
        "FROM documents ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )


async def delete_document(doc_id: str) -> None:
    await execute("DELETE FROM chunks WHERE doc_id=%s", (doc_id,))
    await execute("DELETE FROM documents WHERE doc_id=%s", (doc_id,))


async def list_projects() -> list[dict[str, Any]]:
    return await fetch_all(
        """
        SELECT d.project AS project,
               COUNT(DISTINCT d.doc_id) AS docs,
               SUM(d.chunk_count) AS chunks,
               MAX(d.doc_date) AS last_date
        FROM documents d
        GROUP BY d.project
        ORDER BY last_date DESC
        """
    )


# --------------------------------------------------------------------------
# hybrid retrieval
# --------------------------------------------------------------------------
FT_SCALE = 2.2  # full-text scores in seekdb/ik land around 0.4-0.6 for a hit


def build_search_sql(
    *,
    mode: str,
    ft_weight: float,
    with_type: bool,
    with_dates: bool,
) -> str:
    type_clause = " AND c.doc_type IN (%s)" if with_type else ""
    date_clause = ""
    if with_dates:
        date_clause = " AND c.doc_date >= %s AND c.doc_date <= %s"

    if mode == "fulltext":
        return f"""
        SELECT c.chunk_id, c.doc_id, c.project, c.doc_type, c.doc_date, c.seq,
               c.chunk_text, d.title, d.source_name,
               MATCH(c.chunk_text) AGAINST (%s) AS ft_score,
               NULL AS vec_dist
        FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.project = %s AND MATCH(c.chunk_text) AGAINST (%s) > 0{type_clause}{date_clause}
        ORDER BY ft_score DESC, c.chunk_id ASC
        LIMIT %s
        """

    if mode == "vector":
        return f"""
        SELECT c.chunk_id, c.doc_id, c.project, c.doc_type, c.doc_date, c.seq,
               c.chunk_text, d.title, d.source_name,
               NULL AS ft_score,
               cosine_distance(c.embedding, %s) AS vec_dist
        FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.project = %s{type_clause}{date_clause}
        ORDER BY vec_dist ASC, c.chunk_id ASC
        LIMIT %s
        """

    vec_weight = max(0.0, 1.0 - ft_weight)
    return f"""
    SELECT c.chunk_id, c.doc_id, c.project, c.doc_type, c.doc_date, c.seq,
           c.chunk_text, d.title, d.source_name,
           MATCH(c.chunk_text) AGAINST (%s) AS ft_score,
           cosine_distance(c.embedding, %s) AS vec_dist,
           (%s * LEAST(IFNULL(MATCH(c.chunk_text) AGAINST (%s), 0) * {FT_SCALE}, 1.0)
            + %s * (1 - LEAST(IFNULL(cosine_distance(c.embedding, %s), 1.0), 1.0))) AS score
    FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
    WHERE c.project = %s{type_clause}{date_clause}
    ORDER BY score DESC, c.chunk_id ASC
    LIMIT %s
    """


async def hybrid_search(
    *,
    query: str,
    project: str,
    mode: str = "hybrid",
    ft_weight: float | None = None,
    doc_types: list[str] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    limit: int = 8,
    log: bool = True,
) -> dict[str, Any]:
    ft_weight = settings.ft_weight if ft_weight is None else ft_weight
    ft_weight = min(1.0, max(0.0, ft_weight))
    vec_weight = 1.0 - ft_weight
    with_type = bool(doc_types)
    with_dates = bool(date_from and date_to)
    sql = build_search_sql(mode=mode, ft_weight=ft_weight, with_type=with_type, with_dates=with_dates)
    qv = embed.vector_literal(embed.embed(query))

    if mode == "fulltext":
        args: list[Any] = [query, project, query]
    elif mode == "vector":
        args = [qv, project]
    else:
        args = [query, qv, ft_weight, query, vec_weight, qv, project]
    if with_type:
        args.append(list(doc_types))  # type: ignore[arg-type]
    if with_dates:
        args.extend([date_from, date_to])
    args.append(limit)

    started = time.perf_counter()
    rows = await fetch_all(sql, args)
    latency = int((time.perf_counter() - started) * 1000)

    hits: list[dict[str, Any]] = []
    for row in rows:
        ft_score = float(row["ft_score"]) if row.get("ft_score") is not None else 0.0
        vec_dist = float(row["vec_dist"]) if row.get("vec_dist") is not None else None
        score = row.get("score")
        if score is None:
            score = ft_weight * min(ft_score * FT_SCALE, 1.0) + vec_weight * max(
                0.0, 1.0 - (vec_dist if vec_dist is not None else 1.0)
            )
        hits.append(
            {
                "chunk_id": row["chunk_id"],
                "doc_id": row["doc_id"],
                "project": row["project"],
                "doc_type": row["doc_type"],
                "doc_date": _date_str(row["doc_date"]),
                "seq": row["seq"],
                "text": row["chunk_text"],
                "title": row["title"],
                "source_name": row["source_name"],
                "ft_score": round(ft_score, 4),
                "vec_dist": None if vec_dist is None else round(vec_dist, 4),
                "score": round(float(score), 4),
            }
        )

    if log:
        await execute(
            "INSERT INTO search_log(ts, project, query, mode, latency_ms, hits, sql_text) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (dt.datetime.now(), project, query[:500], mode, latency, len(hits), sql.strip()),
        )
    return {
        "query": query,
        "project": project,
        "mode": mode,
        "ft_weight": round(ft_weight, 3),
        "vec_weight": round(vec_weight, 3),
        "latency_ms": latency,
        "hits": hits,
        "sql": sql.strip(),
        "embedding_provider": embed.provider_label(),
    }


def _date_str(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    return str(value or "")


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------
async def upsert_task(task: dict[str, Any]) -> None:
    now = dt.datetime.now()
    await execute(
        """
        INSERT INTO tasks(task_id, project, title, owner, status, priority, due_date,
                          source_doc_id, source_chunk, evidence, memo, created_at, updated_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE title=VALUES(title), owner=VALUES(owner),
          status=VALUES(status), priority=VALUES(priority), due_date=VALUES(due_date),
          evidence=VALUES(evidence), updated_at=VALUES(updated_at)
        """,
        (
            task["task_id"],
            task["project"],
            task["title"][:250],
            task.get("owner", "")[:60],
            task.get("status", "todo"),
            task.get("priority", "P2"),
            task.get("due_date"),
            task.get("source_doc_id", ""),
            task.get("source_chunk"),
            task.get("evidence", ""),
            task.get("memo", "")[:500],
            now,
            now,
        ),
    )


async def list_tasks(project: str | None = None) -> list[dict[str, Any]]:
    base = (
        "SELECT task_id, project, title, owner, status, priority, due_date, source_doc_id, "
        "source_chunk, evidence, memo, updated_at FROM tasks"
    )
    if project:
        rows = await fetch_all(base + " WHERE project=%s ORDER BY status, due_date IS NULL, due_date", (project,))
    else:
        rows = await fetch_all(base + " ORDER BY project, status, due_date IS NULL, due_date")
    for row in rows:
        row["due_date"] = _date_str(row["due_date"]) or None
        row["updated_at"] = _date_str(row["updated_at"])
    return rows


async def update_task_status(task_id: str, status: str, memo: str = "") -> None:
    await execute(
        "UPDATE tasks SET status=%s, memo=%s, updated_at=%s WHERE task_id=%s",
        (status, memo[:500], dt.datetime.now(), task_id),
    )


async def count_tasks_by_status(project: str) -> dict[str, int]:
    rows = await fetch_all("SELECT status, COUNT(*) AS c FROM tasks WHERE project=%s GROUP BY status", (project,))
    return {row["status"]: int(row["c"]) for row in rows}


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------
async def save_report(
    *,
    report_id: str,
    project: str,
    week_start: dt.date,
    week_end: dt.date,
    content_md: str,
    citations: list[dict[str, Any]],
    memory: dict[str, Any],
) -> None:
    await execute(
        """
        INSERT INTO reports(report_id, project, week_start, week_end, content_md,
                            citations_json, memory_json, created_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE content_md=VALUES(content_md),
          citations_json=VALUES(citations_json), memory_json=VALUES(memory_json)
        """,
        (
            report_id,
            project,
            week_start,
            week_end,
            content_md,
            json.dumps(citations, ensure_ascii=False),
            json.dumps(memory, ensure_ascii=False),
            dt.datetime.now(),
        ),
    )


async def list_reports(project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    sql = (
        "SELECT report_id, project, week_start, week_end, created_at, "
        "CHAR_LENGTH(content_md) AS size FROM reports"
    )
    args: tuple[Any, ...]
    if project:
        sql += " WHERE project=%s"
        args = (project, limit)
    else:
        args = (limit,)
    sql += " ORDER BY created_at DESC LIMIT %s"
    rows = await fetch_all(sql, args)
    for row in rows:
        row["week_start"] = _date_str(row["week_start"])
        row["week_end"] = _date_str(row["week_end"])
        row["created_at"] = _date_str(row["created_at"])
    return rows


async def get_report(report_id: str) -> dict[str, Any] | None:
    row = await fetch_one("SELECT * FROM reports WHERE report_id=%s", (report_id,))
    if not row:
        return None
    row["week_start"] = _date_str(row["week_start"])
    row["week_end"] = _date_str(row["week_end"])
    row["created_at"] = _date_str(row["created_at"])
    row["citations"] = json.loads(row.pop("citations_json") or "[]")
    row["memory"] = json.loads(row.pop("memory_json") or "{}")
    return row


# --------------------------------------------------------------------------
# evidence / stats
# --------------------------------------------------------------------------
async def recent_searches(limit: int = 20) -> list[dict[str, Any]]:
    rows = await fetch_all(
        "SELECT id, ts, project, query, mode, latency_ms, hits FROM search_log ORDER BY id DESC LIMIT %s",
        (limit,),
    )
    for row in rows:
        row["ts"] = _date_str(row["ts"])
    return rows


async def stats() -> dict[str, Any]:
    row = await fetch_one(
        """
        SELECT (SELECT COUNT(*) FROM documents) AS documents,
               (SELECT COUNT(*) FROM chunks)    AS chunks,
               (SELECT COUNT(*) FROM tasks)     AS tasks,
               (SELECT COUNT(*) FROM reports)   AS reports,
               (SELECT COUNT(*) FROM search_log) AS searches,
               (SELECT COALESCE(SUM(chunk_count),0) FROM documents) AS indexed_chunks
        """
    )
    return row or {}


async def table_ddl(name: str) -> str:
    row = await fetch_one(f"SHOW CREATE TABLE {name}")
    if not row:
        return ""
    return next((v for k, v in row.items() if "Create" in k), "")
