#!/usr/bin/env python3
"""Seed the WorkPulse demo dataset through the public HTTP API.

Usage:
    python scripts/seed_demo.py            # uses http://127.0.0.1:8077
    WP_BASE=http://127.0.0.1:9000 python scripts/seed_demo.py

Every document goes through the normal ingest path, so the seeded data
exercises chunking, embedding, task extraction and the PowerContext writes
exactly like a human import would.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("WP_BASE", "http://127.0.0.1:8077").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"

MEMORIES = [
    (
        "支付中台改造",
        "preference",
        "周报只写三段：本周进展、风险与阻塞、下周计划；每条进展必须带原文出处，全文不超过 800 字。",
        "周会确认的汇报口径",
    ),
    (
        "支付中台改造",
        "decision",
        "灰度分三档推进（10% / 20% / 50%），每档至少观察 48 小时，20% 之后必须先有退款超时监控才允许上探。",
        "10-22 周会结论",
    ),
]


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.load(response)


def main() -> int:
    manifest = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["documents"] + manifest["second_project"]

    if "--reset" in sys.argv[1:]:
        try:
            post("/api/admin/reset", {"confirm": "reset"})
            print("已清空文档/分块/任务/报告/检索日志（PowerContext 记忆保留）\n")
        except urllib.error.HTTPError as exc:
            print(f"重置失败：HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}")
            return 1

    try:
        health = get("/api/health")
    except urllib.error.URLError as exc:
        print(f"✗ 无法连接 {BASE}：{exc}. 先启动服务再运行本脚本。")
        return 1

    print(f"seekdb   : {health['seekdb']['version']} @ {health['seekdb']['host']}")
    print(f"记忆服务 : {health['powercontext']['url']} reachable={health['powercontext']['reachable']}")
    print(f"向量器   : {health['embedding']}")
    print()

    total_chunks = 0
    total_tasks = 0
    for item in entries:
        text = (DEMO / item["file"]).read_text(encoding="utf-8")
        result = post(
            "/api/ingest",
            {
                "project": item["project"],
                "doc_type": item["doc_type"],
                "title": item["title"],
                "doc_date": item["date"],
                "source_name": item["source"],
                "text": text,
                "remember": True,
            },
        )
        total_chunks += result["chunks"]
        total_tasks += len(result["tasks"])
        print(
            f"  + {item['date']} {item['title']:<28} chunks={result['chunks']:<3} "
            f"tasks={len(result['tasks'])} memory={len(result['memory_writes'])}"
        )

    print()
    for project, kind, text, reason in MEMORIES:
        post("/api/memory", {"project": project, "kind": kind, "text": text, "reason": reason})
        print(f"  * 记忆写入 [{kind}] {text[:36]}…")

    print()
    print(f"完成：{len(entries)} 份资料 / {total_chunks} 个分块 / {total_tasks} 条待办（含重复合并前计数）")
    print(f"打开 {BASE} 查看 WorkPulse。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
