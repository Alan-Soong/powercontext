# 验证样例（Verification Sample）

本文件给出**可按步骤复现**的验证流程：每一步说明「怎么做、看什么、期望结果」，并在最后给出一张技术核对表。

> 环境：Linux / macOS（Windows 用 WSL2）。全部命令在 `qoder/workpulse/` 下执行。
> 时间基准：演示数据构造在 2026-10 下旬，周报窗口默认「截至 2026-10-28」。

---

## 0. 前置条件

| 服务 | 检查命令 | 期望 |
| --- | --- | --- |
| seekdb | `docker exec seekdb mysql -h127.0.0.1 -uroot -e "select version()"` | `5.7.25-OceanBase seekdb-v1.4.0.0` |
| PowerContext | `curl -s http://127.0.0.1:8000/health/live` | HTTP 200 |
| WorkPulse | `curl -s http://127.0.0.1:8077/api/health` | `"ok": true` |

三者都起来后，先灌演示数据（通过正常导入接口，不是直接写库）：

```bash
python scripts/seed_demo.py --reset
```

期望输出（节选）：

```
seekdb   : 5.7.25-OceanBase seekdb-v1.4.0.0 @ 127.0.0.1:2881
记忆服务 : http://127.0.0.1:8000 reachable=True
  + 2026-10-22 10-22 支付中台周会纪要               chunks=2   tasks=4 memory=4
  ...
完成：9 份资料 / 16 个分块 / 29 条待办
```

---

## 1. 一键端到端验证

```bash
python scripts/verify_sample.py
```

该脚本走完整产品闭环，输出 21 项检查与对应证据。实测结果：

```
[1] 服务连通性
  ✓ seekdb 可连接 — 5.7.25-OceanBase seekdb-v1.4.0.0
  ✓ PowerContext 可连接 — http://127.0.0.1:8000
[2] 导入资料（写入 seekdb + 抽取任务 + 写记忆）
  ✓ 分块写入 seekdb — 2 个分块 · 31ms
  ✓ 抽取待办 — 4 条
  ✓ 抽取决策 — 1 条
  ✓ 写入 PowerContext — 2 次写调用
[3] seekdb 混合检索（一条 SQL：全文 + 向量 + 结构化过滤）
  ✓ 返回结果 — 5 段 · 5ms
  ✓ Top1 与问题相关 — 10-23 企微群 · 灰度问题排查 chunk#97
  ✓ SQL 同时包含全文与向量语义
  ✓ SQL 包含结构化过滤
[4] 三路通道对比（同一问题）
    fulltext     3ms  ...
    vector       3ms  ...
    hybrid       1ms  ...
[5] PowerContext 记忆读写
  ✓ 写入成功 — scope scp_...
  ✓ 条目可列出
  ✓ 有界召回可用 — status=ready
[6] 生成周报（引用原文 + 复用记忆）
  ✓ 每条进展带出处 — 17 处引用
  ✓ 召回了 PowerContext 记忆 — 8 条
结果：21/21 项通过
```

退出码 `0` 表示全部通过。

---

## 2. 分步手工验证（在浏览器里做）

打开 <http://127.0.0.1:8077>。

### 2.1 混合检索：seekdb 三路信号同时生效

1. 左侧切到「混合检索」，输入 `退款超时现在到哪一步了，谁在负责`，回车。
2. 期望：返回 5~8 段原文，每段带 `chunk#id · 文档 · 日期 · 来源`。
3. 每条命中下方有三个分数条：**融合 / 全文 / 向量**。
4. 切换「仅全文」与「仅向量」，对比排序差异——这正是 hybrid 融合的价值。
5. 展开「查看本次检索使用的那一条 SQL」，可以看到 `MATCH(...) AGAINST(...)` 与 `cosine_distance(...)` 在同一条语句里。
6. 右侧「关键词权重」滑块从 0.45 拉到 0.9，再检索：关键词命中的权重变大，排序立即变化（权重即 SQL 里的系数）。

### 2.2 任务看板：从原文自动抽取 + 状态写回记忆

1. 切到「任务看板」，看到按「进行中 / 阻塞 / 待开始 / 已完成」分组的待办。
2. 每条带负责人（王工/李敏/张磊/夏工/周航）、截止日、来源 `chunk#id`。
3. 把任意一条状态改成「已完成」。
4. 期望：Toast 提示「状态已更新，并写入 PowerContext 记忆」。
5. 到「技术证据」页 → 右侧 `PowerContext 调用` 表，能看到新出现的 `/v1/memory/remember` 记录（200）。

### 2.3 周报生成：引用 + 记忆复用

1. 切到「周报生成」，日期填 `2026-10-28`，回溯天数 `10`，点「生成周报草稿」。
2. 期望：左侧出 Markdown 周报，结构为
   `一、本周进展`（每条带 `chunk#id` 出处）→ `二、风险与阻塞` → `三、下周计划`（表格，含逾期标记）→ `四、本周已完成` → `五、记忆延续（PowerContext）`。
3. 右侧「记忆复用」显示本次召回的记忆条目与 scope 状态 `ready`。
4. 点「保存并写入记忆」→ 期望提示保存成功，并多出 `summary` 记忆（周报结论）。

### 2.4 长期记忆：写入 → 召回

1. 切到「长期记忆」，右侧「写入一条记忆」，类型选 `preference`，内容填
   `周报风险项必须写明负责人和截止日`，提交。
2. 期望：左侧条目列表出现该条（`kind=preference`）。
3. 「召回测试」输入 `周报 风险 负责人`，点召回 → 期望返回刚写入的条目。
4. 回到「周报生成」再次生成 → 「记忆延续」段出现这条偏好。

### 2.5 技术证据：可核对的原始记录

「技术证据」页给出四份材料：

| 区块 | 内容 | 说明 |
| --- | --- | --- |
| seekdb 建表语句 | `SHOW CREATE TABLE chunks` | 同一张表上有 `FULLTEXT KEY ... WITH PARSER ik` 和 `VECTOR KEY ... (DISTANCE=COSINE, TYPE=HNSW)` |
| 最近一条混合检索 SQL | `search_log.sql_text` | 全文 + 向量 + 结构化条件 |
| 检索日志 | `search_log` 表 | 时间、查询、模式、命中数、耗时 |
| PowerContext 调用 | `logs/powercontext_calls.jsonl` | 每次 `/v1/scopes`、`/v1/memory/remember`、`/v1/context/prepare` 的状态码与耗时 |

也可以在数据库里直接核对：

```bash
docker exec -it seekdb mysql -h127.0.0.1 -uroot workpulse -e "
  select count(*) as docs from documents;
  select count(*) as chunks from chunks;
  select project, count(*) c from chunks group by project;
  select task_id, owner, status, due_date, left(title,40) from tasks limit 8;
  select id, mode, latency_ms, hits, left(query,30) from search_log order by id desc limit 5;
"
```

PowerContext 侧的人工核对入口：<http://127.0.0.1:8000/dashboard/home>（用 `.env` 里的 `WP_PC_TOKEN` 登录，选择 `WorkPulse · 支付中台改造` 这个 scope，可看到记忆条目与版本历史）。

---

## 3. 技术核对表

| 要求 | 在项目里怎么体现 | 在哪核对 |
| --- | --- | --- |
| seekdb 提供存储 | `documents` / `chunks` / `tasks` / `reports` / `search_log` 五张表 | `/api/evidence` 的 DDL、`show tables` |
| seekdb 提供全文检索 | `FULLTEXT INDEX ft_chunk (chunk_text) WITH PARSER ik`，检索时 `MATCH ... AGAINST` | 检索页「仅全文」模式、SQL 面板 |
| seekdb 提供向量检索 | `VECTOR INDEX vec_chunk ... (DISTANCE=cosine, TYPE=hnsw, LIB=vsag)`，`VECTOR(384)` 列 | 检索页「仅向量」模式、向量分数条 |
| seekdb 提供结构化过滤 | `project` / `doc_type` / `doc_date` 条件与索引 | 检索页的类型、起止日期筛选 |
| 三者结合在**一条 SQL** 里 | `hybrid` 模式的加权融合 SQL | 「技术证据 → 最近一条混合检索 SQL」 |
| PowerContext 保存上下文 | `/v1/scopes` 建作用域；`/v1/memory/remember` 写决策/进度/结论/偏好 | `/api/memory`、`powercontext_calls.jsonl` |
| 记忆在后续复用 | 生成周报时 `/v1/context/prepare` 召回，注入「记忆延续」段 | 周报页右侧「记忆复用」、报告正文第五节 |
| 记忆可人工核对 | Dashboard + `entries/list` | <http://127.0.0.1:8000/dashboard/home> |

---

## 4. 截图

`site/images/` 下是本次实测截图（1440×1500，无裁剪）：

| 文件 | 内容 |
| --- | --- |
| `shot-overview.png` | 概览：资料/分块/待办/检索统计、最近入库、任务、记忆 |
| `shot-search.png` | 混合检索：回答 + 出处 + 三路分数 + 权重 + SQL |
| `shot-tasks.png` | 任务看板：状态分组、负责人、截止日、来源 chunk |
| `shot-report.png` | 周报生成：Markdown 草稿 + 记忆复用 + 引用索引 |
| `shot-memory.png` | 长期记忆：条目、写入表单、召回测试 |
| `shot-evidence.png` | 技术证据：DDL、混合检索 SQL、检索日志、PowerContext 调用 |
