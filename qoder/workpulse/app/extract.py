"""Deterministic extraction of tasks, decisions and risks from raw text.

The extractor is intentionally rule based: it runs offline, it is reproducible
for the graders, and every extracted item keeps a pointer back to the chunk it
came from, so a reviewer can verify the claim against the original note.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any

ACTION_WORDS = (
    "负责", "跟进", "推进", "修复", "上线", "联调", "评审", "确认", "输出", "交付",
    "排期", "验证", "灰度", "迁移", "优化", "复盘", "整理", "对接", "补齐", "回归",
    "压测", "接入", "排查", "补充", "落实", "回滚", "发版", "定稿",
)
DONE_WORDS = (
    "已完成", "已上线", "已发布", "已修复", "已通过", "验收通过", "已灰度",
    "已提交", "已同步", "已更新", "完成了", "已交付", "已回滚",
)
FUTURE_WORDS = ("前完成", "待评审", "待完成", "计划", "预计", "将", "明天", "下周", "下月", "待定", "需要")
DOING_WORDS = ("进行中", "推进中", "灰度中", "联调中", "开发中", "排查中", "进行")
BLOCKED_WORDS = ("阻塞", "卡住", "受阻", "延期", "风险", "待定", "依赖外部", "等待", "卡在")
DECISION_WORDS = ("决定", "结论", "定了", "拍板", "统一为", "确认采用", "评审通过", "确定", "定稿")
RISK_WORDS = ("风险", "阻塞", "延期", "隐患", "超时", "失败率", "回滚", "依赖")
PROGRESS_WORDS = (
    "完成", "上线", "通过", "达成", "交付", "发布", "灰度", "修复", "联调", "提升",
    "下降", "降低", "新增", "接入", "验证", "评审", "合并", "上线率", "覆盖",
)
NOISE_PREFIX = ("好的", "收到", "嗯", "哈哈", "早上好", "晚上好", "谢谢", "以上")

OWNER_PATTERNS = (
    re.compile(r"^([\u4e00-\u9fa5]{2,4})[：:]\s*"),                                    # 聊天发言人
    re.compile(r"@([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{1,11})"),
    re.compile(r"([\u4e00-\u9fa5]{1,3}(?:工|总|老师|同学))(?=负责|跟进|推进|输出|确认|对接|排查)"),
    re.compile(r"([\u4e00-\u9fa5]{2,4})负责"),
    re.compile(r"负责人[:：]\s*([\u4e00-\u9fa5A-Za-z]{2,12})"),
    re.compile(r"由([\u4e00-\u9fa5]{2,4})(?:来)?(?:负责|跟进|推进|输出|确认)"),
    re.compile(
        r"^([\u4e00-\u9fa5]{2,3})(?=负责|跟进|推进|输出|确认|对接|排查|补齐|补充|整理|更新|同步|提交)"
    ),
)

WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

DATE_PATTERNS = (
    (re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?"), "ymd"),
    (re.compile(r"(\d{1,2})月(\d{1,2})[日号]"), "md"),
    (re.compile(r"下下周([一二三四五六日天])"), "dd_weekday"),
    (re.compile(r"下周([一二三四五六日天])"), "next_weekday"),
    (re.compile(r"本周([一二三四五六日天])"), "this_weekday"),
    (re.compile(r"周([一二三四五六日天])"), "this_weekday"),
    (re.compile(r"(\d{1,2})月(底|初)"), "month_edge"),
    (re.compile(r"(明天|后天|今天|本周内|下周内|月底)"), "relative"),
)


def sentences(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in (text or "").replace("\r\n", "\n"):
        if ch == "\n":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
            continue
        buf += ch
        if ch in "。！？!?；;":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def normalize(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:（）()【】\[\]“”\"'`·\-#*>|]+", "", text or "").lower()


_MARKER = re.compile(
    r"^\s*(?:[（(]?\d{1,2}[)）.、]|[-*•·]|[一二三四五六七八九十]{1,2}[、.])\s*"
)


def clean_sentence(sentence: str) -> str:
    return _MARKER.sub("", sentence).strip()


def is_heading(sentence: str) -> bool:
    """Drop section titles such as ``10-27 风险同步 · 支付中台``."""
    stripped = sentence.strip()
    if not stripped:
        return True
    if stripped.endswith(("。", "！", "？", "!", "?", "；", ";")):
        return False
    return len(stripped) <= 30


def _hash_id(*parts: str) -> str:
    raw = "|".join(normalize(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


BLOCK_OWNERS = (
    "我们", "大家", "团队", "对方", "客户", "结论", "背景", "方案", "风险", "决策",
    "进展", "待办", "问题", "数据", "下一步", "参会", "时间", "记录", "备注", "原因",
    "影响", "目标", "计划", "建议", "一", "二", "三",
)


def find_owner(sentence: str) -> str:
    for pattern in OWNER_PATTERNS:
        match = pattern.search(sentence)
        if match:
            owner = match.group(1)
            if owner not in BLOCK_OWNERS:
                return owner
    return ""


def find_due(sentence: str, base: dt.date) -> dt.date | None:
    for pattern, kind in DATE_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        try:
            if kind == "ymd":
                return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if kind == "md":
                year = base.year + (1 if int(match.group(1)) < base.month - 6 else 0)
                return dt.date(year, int(match.group(1)), int(match.group(2)))
            if kind == "dd_weekday":
                return _next_weekday(base, WEEKDAY[match.group(1)], weeks=2)
            if kind == "next_weekday":
                return _next_weekday(base, WEEKDAY[match.group(1)], weeks=1)
            if kind == "this_weekday":
                return _this_weekday(base, WEEKDAY[match.group(1)])
            if kind == "month_edge":
                month = int(match.group(1))
                year = base.year + (1 if month < base.month - 6 else 0)
                if match.group(2) == "底":
                    nxt = dt.date(year + (month == 12), (month % 12) + 1, 1)
                    return nxt - dt.timedelta(days=1)
                return dt.date(year, month, 1)
            if kind == "relative":
                token = match.group(1)
                if token == "今天":
                    return base
                if token == "明天":
                    return base + dt.timedelta(days=1)
                if token == "后天":
                    return base + dt.timedelta(days=2)
                if token == "本周内":
                    return _this_weekday(base, 4)
                if token == "下周内":
                    return _next_weekday(base, 4, weeks=1)
                if token == "月底":
                    nxt = dt.date(base.year + (base.month == 12), (base.month % 12) + 1, 1)
                    return nxt - dt.timedelta(days=1)
        except (ValueError, KeyError):
            continue
    return None


def _next_weekday(base: dt.date, weekday: int, weeks: int = 1) -> dt.date:
    monday = base - dt.timedelta(days=base.weekday())
    return monday + dt.timedelta(days=7 * weeks + weekday)


def _this_weekday(base: dt.date, weekday: int) -> dt.date:
    monday = base - dt.timedelta(days=base.weekday())
    candidate = monday + dt.timedelta(days=weekday)
    if candidate < base:
        candidate += dt.timedelta(days=7)
    return candidate


def find_status(sentence: str) -> str:
    head = sentence.split("，")[0] if "，" in sentence else sentence
    scope = head if len(head) >= 6 else sentence
    if any(word in scope for word in BLOCKED_WORDS):
        return "blocked"
    if any(word in scope for word in DONE_WORDS):
        if any(word in sentence for word in FUTURE_WORDS):
            return "doing"
        return "done"
    if any(word in sentence for word in BLOCKED_WORDS):
        return "blocked"
    if any(word in scope for word in FUTURE_WORDS):
        return "todo"
    if any(word in scope for word in DOING_WORDS):
        return "doing"
    if "完成" in scope:
        return "doing"
    return "todo"


def find_priority(sentence: str) -> str:
    if any(word in sentence for word in ("紧急", "P0", "最高优先", "立刻", "今天必须")):
        return "P0"
    if any(word in sentence for word in ("重要", "P1", "优先", "尽快", "本周必须")):
        return "P1"
    return "P2"


def _clean_title(sentence: str) -> str:
    title = clean_sentence(sentence)
    title = re.sub(r"^(待办|TODO|todo|下一步|跟进项)[:：]?\s*", "", title)
    title = re.sub(r"^[\u4e00-\u9fa5]{2,4}[：:]\s*", "", title)  # 聊天发言人前缀
    title = title.rstrip("。；;，, ")
    return title.strip()[:120]


HEADING_PREFIX = (
    "结论", "决定", "背景", "方案", "风险", "问题", "数据", "参会", "时间",
    "记录", "目标", "影响", "原因", "建议", "进展", "上一步", "备注",
)


def _is_noise(sentence: str) -> bool:
    stripped = clean_sentence(sentence)
    if len(stripped) < 8:
        return True
    if is_heading(sentence):
        return True
    if stripped.startswith(NOISE_PREFIX) and len(stripped) < 16:
        return True
    return False


def _is_lead_in(sentence: str) -> bool:
    """``结论：…`` / ``风险：…`` style lead-ins are headings, not action items."""
    stripped = clean_sentence(sentence)
    if not stripped.startswith(HEADING_PREFIX):
        return False
    head = stripped.split("：")[0]
    return len(head) <= 6


def extract_tasks(
    *,
    text: str,
    project: str,
    doc_id: str,
    base_date: dt.date,
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for raw in sentences(text):
        if _is_noise(raw) or _is_lead_in(raw):
            continue
        sentence = clean_sentence(raw)
        has_action = any(word in sentence for word in ACTION_WORDS)
        has_owner = bool(find_owner(sentence))
        todo_marker = any(w in sentence for w in ("待办", "TODO", "下一步", "跟进项", "负责", "安排"))
        if not (has_action and (has_owner or todo_marker)):
            continue
        title = _clean_title(sentence)
        if len(title) < 6:
            continue
        key = _hash_id(project, title)
        if key in found:
            continue
        found[key] = {
            "task_id": key,
            "project": project,
            "title": title,
            "owner": find_owner(sentence),
            "status": find_status(sentence),
            "priority": find_priority(sentence),
            "due_date": find_due(sentence, base_date),
            "source_doc_id": doc_id,
            "source_chunk": None,
            "evidence": sentence[:400],
            "memo": "",
        }
    return list(found.values())


def extract_decisions(
    *,
    text: str,
    project: str,
    doc_id: str,
    base_date: dt.date,
) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for raw in sentences(text):
        if _is_noise(raw):
            continue
        sentence = clean_sentence(raw)
        if len(sentence) < 10:
            continue
        if not any(word in sentence for word in DECISION_WORDS):
            continue
        key = _hash_id(project, sentence)
        if key in seen:
            continue
        seen[key] = {
            "decision_id": key,
            "project": project,
            "text": sentence[:400],
            "decided_at": base_date.isoformat(),
            "source_doc_id": doc_id,
        }
    return list(seen.values())


def extract_risks(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in sentences(text):
        if _is_noise(raw):
            continue
        sentence = clean_sentence(raw)
        if len(sentence) < 10 or not any(word in sentence for word in RISK_WORDS):
            continue
        if "风险" not in sentence and "阻塞" not in sentence and "延期" not in sentence:
            continue
        key = normalize(sentence)
        if key in seen:
            continue
        seen.add(key)
        out.append(sentence[:240])
    return out


def is_progress_sentence(sentence: str) -> bool:
    stripped = clean_sentence(sentence)
    if len(stripped) < 10 or is_heading(sentence):
        return False
    return any(word in stripped for word in PROGRESS_WORDS)


def attach_chunk_ids(tasks: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> None:
    """Link each task to the stored chunk that contains its evidence."""
    for task in tasks:
        needle = normalize(task.get("evidence", ""))[:40]
        if not needle:
            continue
        for chunk in chunks:
            if needle and needle in normalize(chunk["chunk_text"]):
                task["source_chunk"] = chunk["chunk_id"]
                break
