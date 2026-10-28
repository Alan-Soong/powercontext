# WorkPulse · 周报与项目记忆工作台

> 把散落的会议纪要、聊天记录和文档变成**可追溯的项目记忆**，每周自动生成带出处的周报。

WorkPulse 用 **seekdb** 做「资料 → 分块 → 混合检索」的底座，用 **PowerContext** 做「跨会话的长期记忆」，解决三件每天都在发生的事：

| 痛点 | WorkPulse 的做法 |
| --- | --- |
| 周报要从聊天记录里一条条拼 | 按周聚合原文、任务与记忆，一键生成周报草稿，每条进展都带 `chunk#id` 出处 |
| 项目资料散落，找不到当初的决定和出处 | seekdb 一条 SQL 做全文 + 向量 + 结构化过滤的混合检索，答案永远可回查原文 |
| 任务进度要反复确认 | 从纪要里自动抽取待办（负责人/截止/状态），状态变更同步写进长期记忆 |

---

## 1. 技术分工（这是本项目最关键的设计）

```
        浏览器 (app/static)
              │  HTML / fetch
              ▼
      FastAPI 应用 (app/api.py)
        │                    │
        │ 一条 SQL 混合检索    │ HTTP /v1/*
        ▼                    ▼
   ┌─────────┐          ┌──────────────┐
   │ seekdb  │          │ PowerContext │
   │ 2881    │          │ 8000         │
   └─────────┘          └──────────────┘
   原文与检索             长期记忆
```

### seekdb（OceanBase seekdb v1.4.0，MySQL 协议）

| 表 | 作用 |
| --- | --- |
| `documents` | 一份资料（纪要/聊天/文档）的元数据与原文 |
| `chunks` | 检索单元：`chunk_text` + `VECTOR(384)`，**同一张表上同时建 FULLTEXT 与 VECTOR 索引** |
| `tasks` | 抽取出来的待办：负责人、截止日、状态、来源 chunk |
| `reports` | 生成过的周报（正文 + 引用 + 记忆快照） |
| `search_log` | 每次检索的 SQL、耗时、命中数，作为可核对证据 |
| `meta` | PowerContext scope 绑定、记忆去重标记 |

关键 DDL（`app/db.py`）：

```sql
CREATE TABLE chunks (
  chunk_id   BIGINT       NOT NULL AUTO_INCREMENT,
  project    VARCHAR(64)  NOT NULL,
  doc_type   VARCHAR(32)  NOT NULL,
  doc_date   DATE         NOT NULL,
  chunk_text TEXT         NOT NULL,
  embedding  VECTOR(384),
  PRIMARY KEY (chunk_id),
  FULLTEXT INDEX ft_chunk (chunk_text) WITH PARSER ik,
  VECTOR INDEX vec_chunk (embedding) WITH (DISTANCE=cosine, TYPE=hnsw, LIB=vsag),
  KEY idx_chunk_filter (project, doc_type, doc_date)
) ORGANIZATION = HEAP;
```

关键查询：**一条 SQL 同时用到全文分数、向量距离和结构化条件**，加权融合后排序。

```sql
SELECT ...,
       MATCH(c.chunk_text) AGAINST (%s) AS ft_score,
       cosine_distance(c.embedding, %s)  AS vec_dist,
       (%s * LEAST(IFNULL(MATCH(c.chunk_text) AGAINST (%s), 0) * 2.2, 1.0)
      + %s * (1 - LEAST(IFNULL(cosine_distance(c.embedding, %s), 1.0), 1.0))) AS score
FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
WHERE c.project = %s AND c.doc_type IN (...) AND c.doc_date BETWEEN %s AND %s
ORDER BY score DESC LIMIT %s;
```

界面上的「混合 / 仅全文 / 仅向量」三个模式，就是同一张表的三种检索路径，可以直观看到两路信号各自的贡献。

### PowerContext（1.2.0，basic memory 模式，无需模型 API）

| 环节 | 接口 | 存的是什么 |
| --- | --- | --- |
| 建项目作用域 | `POST /v1/scopes` | 每个项目一个 scope，`idempotency_key` 保证复用同一个作用域 |
| 写记忆 | `POST /v1/memory/remember` | `decision` 决策、`progress` 进度、`summary` 阶段结论、`preference` 周报口径 |
| 有界召回 | `POST /v1/context/prepare` | 生成周报时召回历史结论与口径，注入报告的「记忆延续」段 |
| 列出记忆 | `POST /v1/memory/entries/list` | 界面「长期记忆」页、报告侧栏 |
| 人工核对 | Dashboard `/dashboard/home` | 记忆读写记录可视化 |

每次调用都会追加到 `logs/powercontext_calls.jsonl`，界面的「技术证据」页也会展示最近 25 次调用。

**记忆为什么是真的有用**：`preference` 里存的是「周报只写三段、每条进展必须带出处」，`summary` 里存的是上周的结论。下周再生成时，`/v1/context/prepare` 会把这些召回进报告，任务状态变更（如把某条任务标为 `blocked`）也会立刻写成一条 `progress` 记忆——这就是「越用越准」的具体实现。

### 向量从哪来

默认使用**离线确定性向量器**（`app/embed.py`，字符 n-gram + 有符号随机投影，384 维，L2 归一化），不下载模型、不联网，保证评审在任何机器上都能复现。配置 `WP_EMBEDDING_PROVIDER=openai` + `WP_EMBEDDING_BASE_URL` 即可换成任意 OpenAI 兼容的 embedding 服务，业务代码无需改动。

---

## 2. 目录结构

```
qoder/workpulse/
├── app/
│   ├── api.py          # HTTP 接口（导入/检索/任务/周报/记忆/证据）
│   ├── config.py       # 环境变量与 .env 加载
│   ├── db.py           # seekdb 连接池、建表、混合检索 SQL
│   ├── embed.py        # 向量器（离线 hash / OpenAI 兼容）
│   ├── extract.py      # 规则化抽取：待办、决策、风险、截止日
│   ├── pc.py           # PowerContext 客户端 + 调用留痕
│   ├── report.py       # 周报组装（进展/风险/计划/记忆）
│   ├── main.py         # FastAPI 入口
│   └── static/         # 前端（原生 HTML/CSS/JS，无构建步骤）
├── demo/               # 演示资料（9 份中文纪要/聊天/复盘 + manifest）
├── scripts/
│   ├── seed_demo.py    # 通过 HTTP 接口灌入演示数据
│   └── verify_sample.py# 端到端验证样例（21 项检查）
├── site/               # Qoder Sites 项目网站（静态）
├── docs/               # 开发记录与验证说明
├── run.sh              # 一键检查并启动
├── requirements.txt
└── .env.example
```

---

## 3. 运行步骤

### 3.1 环境要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.11+（实测 3.12） | 应用与 PowerContext 都用它 |
| seekdb | v1.4.0（Docker 镜像 `quay.io/oceanbase/seekdb:latest`） | 也可以用 `pylibseekdb` 嵌入式 |
| PowerContext | 1.2.0 | `pip install "powercontext[cli,server]==1.2.0"` |
| 操作系统 | Linux / macOS（Windows 建议 WSL2） | seekdb 与 PowerContext 官方均主打 Linux |

### 3.2 启动 seekdb

```bash
docker run -d --name seekdb -p 2881:2881 quay.io/oceanbase/seekdb:latest
docker exec seekdb sh -c 'mysql -h127.0.0.1 -uroot -e "select version()"'
# 期望：5.7.25-OceanBase seekdb-v1.4.0.0
```

应用会自动 `CREATE DATABASE workpulse` 并建表，**不需要手工执行 SQL**。

### 3.3 启动 PowerContext（长期记忆）

```bash
pip install "powercontext[cli,server]==1.2.0"

export POWERCONTEXT_HOME=$HOME/.workpulse/powercontext
export POWERCONTEXT_SERVER_DASHBOARD_ENABLED=true
export POWERCONTEXT_SERVER_ACCESS_MODE=enforced
export POWERCONTEXT_SERVER_AUTH_TOKEN=workpulse-local-token
powercontext server run          # 监听 127.0.0.1:8000
```

`basic memory` 模式**不需要任何模型 API Key**。Dashboard 用同一个 token 登录：<http://127.0.0.1:8000/dashboard/home>。

### 3.4 启动 WorkPulse

```bash
cd qoder/workpulse
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env             # 按需修改端口/密码
python -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

或者直接：

```bash
./run.sh --seed                  # 检查 seekdb、拉起 PowerContext、启动应用并灌演示数据
```

打开 <http://127.0.0.1:8077>。

### 3.5 加载演示数据（可选但推荐）

```bash
python scripts/seed_demo.py --reset
```

会通过**正常的 HTTP 导入接口**写入 9 份资料（2 个项目：`支付中台改造`、`数据平台迁移`），因此分块、向量、任务抽取、PowerContext 写入全都真实发生。

### 3.6 配置项（`.env`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WP_SEEKDB_HOST/PORT/USER/PASSWORD` | `127.0.0.1` / `2881` / `root` / 空 | seekdb 连接 |
| `WP_SEEKDB_DATABASE` | `workpulse` | 数据库名，不存在会自动创建 |
| `WP_PC_URL` | `http://127.0.0.1:8000` | PowerContext 地址 |
| `WP_PC_TOKEN` | `workpulse-local-token` | 与 `POWERCONTEXT_SERVER_AUTH_TOKEN` 保持一致 |
| `WP_EMBEDDING_PROVIDER` | `hash` | `hash`（离线）/ `openai`（外部服务） |
| `WP_FT_WEIGHT` | `0.45` | 全文权重，其余为向量权重 |
| `WP_CHUNK_CHARS` | `320` | 分块长度 |
| `WP_PORT` | `8077` | 应用端口 |

---

## 4. 验证样例

```bash
python scripts/verify_sample.py
```

预期输出（实测，本地 WSL2 + 容器版 seekdb v1.4.0）：

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
    fulltext     3ms  10-23 企微群 · 灰度(97) / 10-24 灰度复盘(106) / 10-21 需求评审(94)
    vector       3ms  10-21 需求评审：退款超(93) / 10-24 灰度复盘(106) / 10-08 周会纪要(89)
    hybrid       1ms  10-23 企微群 · 灰度(97) / 10-24 灰度复盘(106) / 10-21 需求评审(94)
[5] PowerContext 记忆读写
  ✓ 写入成功 — scope scp_...
  ✓ 条目可列出 — 56 条
  ✓ 有界召回可用 — status=ready
[6] 生成周报（引用原文 + 复用记忆）
  ✓ 每条进展带出处 — 17 处引用
  ✓ 召回了 PowerContext 记忆 — 8 条

结果：21/21 项通过
```

更详细的核对点与截图见 [`docs/validation.md`](docs/validation.md)。

---

## 5. 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | seekdb + PowerContext + 向量器状态 |
| POST | `/api/ingest` | 导入文本资料（分块、向量、抽任务、写记忆） |
| POST | `/api/ingest/file` | 上传 `.txt/.md/.csv` |
| GET | `/api/search?q=&project=&mode=&ft_weight=` | 混合检索，返回命中 + 生成 SQL |
| GET/POST | `/api/tasks`、`/api/tasks/{id}/status` | 任务看板与状态变更（变更会写记忆） |
| POST | `/api/report` | 生成/保存周报 |
| GET/POST | `/api/memory` | 记忆列表、召回测试、写入 |
| GET | `/api/evidence` | 建表 DDL、最近 SQL、检索日志、PowerContext 调用记录 |
| POST | `/api/admin/reset` | 清空文档/任务/报告（保留长期记忆） |

交互式文档：<http://127.0.0.1:8077/docs>

---

## 6. 数据与隐私

- `demo/` 下的全部资料为**虚构示例**（姓名、指标、时间均为演示编造），不含任何真实用户数据。
- `.env` 不提交；`.env.example` 只有占位值。`POWERCONTEXT_SERVER_AUTH_TOKEN` 默认值仅用于本机演示。
- PowerContext 以 `basic memory` 模式运行，不向任何外部服务发送数据；默认的 hash 向量器完全离线。

## 7. 赛题信息

- 参赛作品：Qoder × OceanBase「Build with Qoder」
- 代码位置：`powercontext` 仓库 `qoder-2026-workpulse` 分支 → `qoder/workpulse/`
- 项目网站：`qoder/workpulse/site/`（通过 Qoder Sites 发布）
- Qoder 开发记录：[`docs/qoder-dev-record.md`](docs/qoder-dev-record.md)

## 8. License

Apache-2.0（与上游 `powercontext` 保持一致）。复用的开源组件：seekdb（Docker 镜像）、PowerContext（HTTP API）、FastAPI、aiomysql。
