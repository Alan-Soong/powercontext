"""PowerContext client.

PowerContext is the long-term memory of WorkPulse. It is *not* used as a
vector store: it holds the small, high-value facts that must survive between
sessions and be reused later —

* how this person wants the weekly report written (``preference``)
* decisions that were already made and should not be re-litigated (``decision``)
* where each project stands this week (``progress``)
* the conclusions of the previous report (``summary``)

Every call is appended to ``logs/powercontext_calls.jsonl`` so that the memory
traffic can be audited independently of the UI.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from typing import Any

import httpx

from . import db
from .config import settings

LOG_FILE = settings.log_dir / "powercontext_calls.jsonl"


def _log(op: str, path: str, request: dict[str, Any], status: int, latency_ms: int, extra: dict[str, Any]) -> None:
    record = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "op": op,
        "method": "POST",
        "path": path,
        "status": status,
        "latency_ms": latency_ms,
        "request": request,
        **extra,
    }
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.pc_token:
        headers["Authorization"] = f"Bearer {settings.pc_token}"
    return headers


async def _post(op: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.pc_timeout) as client:
            resp = await client.post(f"{settings.pc_url}{path}", json=payload, headers=_headers())
        latency = int((time.perf_counter() - started) * 1000)
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}
        _log(op, path, _redact(payload), resp.status_code, latency, {"response": _brief(body)})
        if resp.status_code >= 400:
            return {"ok": False, "status": resp.status_code, "error": _brief(body), "body": body}
        return {"ok": True, "status": resp.status_code, "body": body, "latency_ms": latency}
    except Exception as exc:  # network down, server down, timeout ...
        latency = int((time.perf_counter() - started) * 1000)
        _log(op, path, _redact(payload), 0, latency, {"error": str(exc)[:300]})
        return {"ok": False, "status": 0, "error": str(exc)[:300]}


async def _get(path: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.pc_timeout) as client:
            resp = await client.get(f"{settings.pc_url}{path}", headers=_headers())
        latency = int((time.perf_counter() - started) * 1000)
        return {"ok": resp.status_code < 400, "status": resp.status_code, "body": resp.json(), "latency_ms": latency}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)[:300]}


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > 400:
            out[key] = value[:400] + f"...({len(value)} chars)"
        else:
            out[key] = value
    return out


def _brief(body: Any) -> Any:
    if isinstance(body, dict):
        keep = {}
        for key in ("status", "scope_id", "mode", "content", "entries", "hits", "error", "detail", "memory", "entry"):
            if key in body:
                value = body[key]
                keep[key] = value if not isinstance(value, (list, dict, str)) else _truncate(value)
        return keep or str(body)[:300]
    return str(body)[:300]


def _truncate(value: Any, limit: int = 240) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_truncate(v, 120) for v in value[:5]] + ([f"...+{len(value) - 5}"] if len(value) > 5 else [])
    if isinstance(value, dict):
        return {k: _truncate(v, 120) for k, v in list(value.items())[:8]}
    return value


# --------------------------------------------------------------------------
# scopes
# --------------------------------------------------------------------------
async def ensure_scope(project: str) -> str | None:
    """Return the PowerContext scope id bound to a WorkPulse project."""
    key = f"pc_scope:{project}"
    cached = await db.get_meta(key)
    if cached:
        return cached
    payload = {
        "title": f"WorkPulse · {project}",
        "summary": f"WorkPulse 项目「{project}」的周报口径、决策与进度记忆",
        "idempotency_key": f"{settings.pc_scope_prefix}:{project}",
    }
    result = await _post("create_scope", "/v1/scopes", payload)
    if not result["ok"] and result.get("status") == 409:
        # the idempotency key already exists server-side but our binding was
        # lost (fresh database, moved machine). Open a parallel scope instead
        # of failing every memory call.
        payload["idempotency_key"] = f"{settings.pc_scope_prefix}:{project}:{dt.date.today():%Y%m%d}"
        result = await _post("create_scope", "/v1/scopes", payload)
    if not result["ok"]:
        return None
    scope_id = _extract_scope_id(result["body"])
    if scope_id:
        await db.set_meta(key, scope_id)
    return scope_id


def _extract_scope_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("scope_id", "id"):
        if isinstance(body.get(key), str):
            return body[key]
    scope = body.get("scope")
    if isinstance(scope, dict):
        for key in ("scope_id", "id"):
            if isinstance(scope.get(key), str):
                return scope[key]
    return None


# --------------------------------------------------------------------------
# memory operations
# --------------------------------------------------------------------------
async def remember(project: str, kind: str, text: str, reason: str = "") -> dict[str, Any]:
    scope_id = await ensure_scope(project)
    if not scope_id:
        return {"ok": False, "error": "PowerContext scope unavailable"}
    digest = hashlib.sha1(f"{project}|{kind}|{text}".encode("utf-8")).hexdigest()[:24]
    dedupe_key = f"pc_mem:{digest}"
    if await db.get_meta(dedupe_key):
        _log("remember_skipped", "/v1/memory/remember", {"kind": kind, "text": text[:120]}, 0, 0, {"reason": "duplicate"})
        return {"ok": True, "skipped": True, "scope_id": scope_id}
    payload = {"scope_id": scope_id, "kind": kind, "text": text}
    if reason:
        payload["reason"] = reason[:500]
    result = await _post("remember", "/v1/memory/remember", payload)
    result["scope_id"] = scope_id
    if result.get("ok"):
        await db.set_meta(dedupe_key, scope_id)
    return result


async def search_memory(project: str, query: str, limit: int = 5) -> dict[str, Any]:
    scope_id = await ensure_scope(project)
    if not scope_id:
        return {"ok": False, "error": "PowerContext scope unavailable", "hits": []}
    payload = {"scope_id": scope_id, "query": query, "limit": limit, "mode": "auto"}
    result = await _post("search_memory", "/v1/memory/search", payload)
    result["scope_id"] = scope_id
    hits = []
    if result["ok"]:
        for hit in (result["body"] or {}).get("hits", []) or []:
            hits.append(
                {
                    "text": hit.get("text") or (hit.get("entry") or {}).get("text", ""),
                    "kind": hit.get("kind") or (hit.get("entry") or {}).get("kind", ""),
                    "score": hit.get("score"),
                    "citation": hit.get("citation"),
                }
            )
    result["hits"] = hits
    return result


async def prepare_context(project: str, query: str, max_bytes: int = 6000) -> dict[str, Any]:
    """Recall a bounded, read-only slice of history for the current request."""
    scope_id = await ensure_scope(project)
    if not scope_id:
        return {"ok": False, "error": "PowerContext scope unavailable", "status": "unavailable", "content": ""}
    payload = {"scope_id": scope_id, "query": query, "max_bytes": max_bytes}
    result = await _post("prepare_context", "/v1/context/prepare", payload)
    body = result.get("body") or {}
    result["scope_id"] = scope_id
    result["status"] = body.get("status", "unknown")
    result["content"] = body.get("content") or ""
    result["citations"] = body.get("citations") or body.get("references") or []
    return result


async def list_entries(project: str, include_inactive: bool = False) -> dict[str, Any]:
    scope_id = await ensure_scope(project)
    if not scope_id:
        return {"ok": False, "error": "PowerContext scope unavailable", "entries": []}
    payload = {"scope_id": scope_id, "include_inactive": include_inactive}
    result = await _post("list_entries", "/v1/memory/entries/list", payload)
    entries = []
    if result["ok"]:
        for entry in (result["body"] or {}).get("entries", []) or []:
            entries.append(
                {
                    "kind": entry.get("kind", ""),
                    "text": entry.get("text", ""),
                    "state": entry.get("state", ""),
                    "version": entry.get("version"),
                    "citation": entry.get("citation"),
                }
            )
    result["entries"] = entries
    result["scope_id"] = scope_id
    return result


async def health() -> dict[str, Any]:
    live = await _get("/health/live")
    return {
        "url": settings.pc_url,
        "reachable": bool(live.get("ok")),
        "status": live.get("status", 0),
        "auth": bool(settings.pc_token),
    }
