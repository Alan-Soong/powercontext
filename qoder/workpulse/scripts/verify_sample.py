#!/usr/bin/env python3
"""End-to-end verification sample for WorkPulse.

    python scripts/verify_sample.py                  # http://127.0.0.1:8077
    WP_BASE=http://127.0.0.1:9000 python scripts/verify_sample.py

It walks the full product loop and prints the evidence that the two required
technologies (seekdb and PowerContext) really take part in the flow:

  1. services reachable          — seekdb version + PowerContext health
  2. ingest                      — chunks written, tasks/decisions extracted
  3. seekdb hybrid retrieval     — one SQL, full-text + vector + filters
  4. channel comparison          — full-text only vs vector only vs hybrid
  5. PowerContext memory         — write, list, bounded recall
  6. weekly report               — citations + recalled memory in the output

Every step prints PASS/FAIL and the relevant raw values, so the run doubles as
the "verification sample" required by the competition.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("WP_BASE", "http://127.0.0.1:8077").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
PROJECT = "支付中台改造"
SAMPLE = ROOT / "demo" / "2026-10-24-pay-review.md"
QUERY = "退款超时现在到哪一步了，谁在负责"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))


def request(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def get(path: str, **params) -> dict:
    if params:
        path += "?" + urllib.parse.urlencode(params)
    return request("GET", path)


def post(path: str, payload: dict) -> dict:
    return request("POST", path, payload)


def main() -> int:
    print(f"WorkPulse 验证样例 · {BASE}\n")

    print("[1] 服务连通性")
    try:
        health = get("/api/health")
    except urllib.error.URLError as exc:
        print(f"  ✗ 无法连接 {BASE}：{exc}")
        return 1
    check("seekdb 可连接", health["seekdb"]["ok"], health["seekdb"]["version"])
    check("PowerContext 可连接", health["powercontext"]["reachable"], health["powercontext"]["url"])
    print(f"    向量器：{health['embedding']}")

    print("\n[2] 导入资料（写入 seekdb + 抽取任务 + 写记忆）")
    text = SAMPLE.read_text(encoding="utf-8")
    ingest = post(
        "/api/ingest",
        {
            "project": PROJECT,
            "doc_type": "doc",
            "title": "10-24 灰度复盘（20% 流量）",
            "doc_date": "2026-10-24",
            "source_name": "验证样例",
            "text": text,
            "remember": True,
        },
    )
    check("分块写入 seekdb", ingest["chunks"] > 0, f"{ingest['chunks']} 个分块 · {ingest['latency_ms']}ms")
    check("抽取待办", len(ingest["tasks"]) > 0, f"{len(ingest['tasks'])} 条")
    check("抽取决策", len(ingest["decisions"]) > 0, f"{len(ingest['decisions'])} 条")
    check(
        "写入 PowerContext",
        any(item.get("ok") for item in ingest["memory_writes"]),
        f"{len(ingest['memory_writes'])} 次写调用",
    )
    for task in ingest["tasks"][:3]:
        print(f"    · {task['status']:<7} {task['owner'] or '待定':<4} {task['title'][:52]}")

    print("\n[3] seekdb 混合检索（一条 SQL：全文 + 向量 + 结构化过滤）")
    hybrid = get("/api/search", q=QUERY, project=PROJECT, mode="hybrid", limit=5)
    check("返回结果", len(hybrid["hits"]) > 0, f"{len(hybrid['hits'])} 段 · {hybrid['latency_ms']}ms")
    top = hybrid["hits"][0] if hybrid["hits"] else None
    check(
        "Top1 与问题相关",
        bool(top and ("退款" in top["text"] or "超时" in top["text"])),
        (top["title"] + f" chunk#{top['chunk_id']}") if top else "",
    )
    check("结果带原文出处", bool(top and top["doc_id"]), top["doc_id"] if top else "")
    check("SQL 同时包含全文与向量语义", "MATCH(" in hybrid["sql"] and "cosine_distance" in hybrid["sql"])
    check("SQL 包含结构化过滤", "c.project =" in hybrid["sql"])
    print("    SQL 片段：")
    for line in hybrid["sql"].splitlines()[:6]:
        print("      " + line)

    print("\n[4] 三路通道对比（同一问题）")
    variants = {}
    for mode in ("fulltext", "vector", "hybrid"):
        data = get("/api/search", q=QUERY, project=PROJECT, mode=mode, limit=3)
        variants[mode] = data
        names = " / ".join(f"{h['title'][:14]}({h['chunk_id']})" for h in data["hits"][:3])
        print(f"    {mode:<9} {data['latency_ms']:>4}ms  {names}")
    check("三种模式都能返回结果", all(v["hits"] for v in variants.values()))

    print("\n[5] PowerContext 记忆读写")
    marker = f"验证样例记忆 {int(time.time())}"
    written = post(
        "/api/memory",
        {
            "project": PROJECT,
            "kind": "preference",
            "text": f"{marker}：周报里风险项必须写明负责人和截止日。",
            "reason": "verify_sample.py",
        },
    )
    check("写入成功", bool(written.get("ok")), f"scope {written.get('scope_id')}")
    listed = get("/api/memory", project=PROJECT)
    check("条目可列出", len(listed["entries"]) > 0, f"{len(listed['entries'])} 条")
    check("刚写入的记忆存在", any(marker in (e.get("text") or "") for e in listed["entries"]))
    check(
        "有界召回可用",
        bool(listed["recall"]["content"]),
        f"status={listed['recall']['status']}",
    )

    print("\n[6] 生成周报（引用原文 + 复用记忆）")
    report = post(
        "/api/report",
        {"project": PROJECT, "week_end": "2026-10-28", "days": 10, "save": True, "remember": True},
    )
    markdown = report["markdown"]
    check("周报包含进展段落", "本周进展" in markdown)
    check("每条进展带出处", "chunk#" in markdown, f"{markdown.count('chunk#')} 处引用")
    check("周报包含风险段落", "风险与阻塞" in markdown)
    check("召回了 PowerContext 记忆", bool(report["memory"]["available"]), f"{len(report['memory']['entries'])} 条")
    check("报告已持久化", bool(report.get("report_id")), str(report.get("report_id")))
    print("    周报前 6 行：")
    for line in markdown.splitlines()[:6]:
        print("      " + line[:100])

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 62)
    print(f"结果：{passed}/{total} 项通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  失败：{name} {detail}")
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
