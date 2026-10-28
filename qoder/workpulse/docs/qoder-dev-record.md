# Qoder 开发记录（模板 + 填写说明）

> ⚠️ **本文件是记录骨架，不是已完成的记录。**
> 赛题要求「项目的主要开发工作必须在 Qoder 中完成，并保留关键开发环节的对话截图或录屏，附 Qoder 账号 UID」。
> 仓库里的这份文件提供：① 开发过程的真实拆解（需求 → 设计 → 编码 → 验证）；② 可直接粘贴进 Qoder 的提示词序列；
> ③ 需要人工补齐的截图/UID 位置。**截图与 UID 必须由参赛者在 Qoder 中真实产生**，不要伪造。

---

## 0. 待填写信息

| 字段 | 值 |
| --- | --- |
| Qoder 账号 UID | `<在 Qoder 个人设置中查看后填写>` |
| 使用的 Qoder 形态 | `<Desktop / IDE 插件 / JetBrains / CLI>` |
| 开发时间 | `<起止日期>` |
| 代码仓库 | `https://github.com/Alan-Soong/powercontext`（分支 `qoder-2026-workpulse`，目录 `qoder/workpulse/`） |
| 项目网站 | `<Qoder Sites 发布后的公网地址>` |

---

## 1. 关键开发环节与证据位

把下面每一节在 Qoder 里重放一次（或直接对着已有代码提问/迭代），截取对话界面即可形成完整记录。
截图建议统一命名放在 `docs/evidence/qoder-01-xxx.png`。

### 1.1 需求拆解

**给 Qoder 的提示词：**

```
我要参赛（Qoder × OceanBase，主题：用 Qoder 做一个让学习/工作更高效的项目）。
我的真实痛点是：① 每周写周报要把会议纪要和群聊记录一条条翻出来；② 项目资料散落，
想找某个决定是谁定的、什么时候定的很难；③ 任务进度要反复问人。
请帮我拆成一个可交付的 MVP，说明每个功能对应解决哪个痛点，并列出需要的技术组件。
约束：必须真实使用 seekdb 或 PowerContext 中的至少一种，能本地跑通，评审要能复现。
```

**产出**：三个痛点 → 六个功能（导入 / 混合检索 / 任务看板 / 周报生成 / 记忆复用 / 证据面板）。

📸 截图位：`qoder-01-requirement.png`

### 1.2 技术选型

**提示词：**

```
在 seekdb 和 PowerContext 之间怎么分工？seekdb 负责文档分块与混合检索（全文 + 向量 + 结构化过滤），
PowerContext 负责跨会话的长期记忆（周报口径、决策、结论）。请给出数据模型和两者的调用边界，
并说明为什么不用其中一方包打天下。
```

**产出**：`README.md` 第 1 节的技术分工表；`chunks`、`tasks`、`reports`、`search_log` 等表设计。

📸 截图位：`qoder-02-architecture.png`

### 1.3 seekdb 建库建表

**提示词：**

```
用 seekdb（MySQL 协议，127.0.0.1:2881，root 空密码）写 Python 建表脚本：
chunks 表要同时支持中文全文检索和向量检索，建 FULLTEXT 索引（ik 分词）和 HNSW 向量索引（cosine），
并按 project / doc_type / doc_date 建普通索引。给出可执行的 DDL 和对应 Python 代码。
```

**产出**：`app/db.py` 的 `ensure_schema()`。

📸 截图位：`qoder-03-seekdb-ddl.png`

### 1.4 混合检索 SQL

**提示词：**

```
写一条 SQL，在 chunks 表上同时计算全文分数 MATCH(chunk_text) AGAINST(...) 和向量距离
cosine_distance(embedding, ...)，按可调的加权和排序，并支持 project / doc_type / doc_date 过滤。
要求：一条语句完成，返回每条的 ft_score、vec_dist、最终 score。
```

**产出**：`app/db.py` 的 `build_search_sql()`。
定稿前试过 3 版：先分两次查询再用 RRF 融合 → 改为一次全表加权（能召回只有向量相似的结果）→
最后加上 `IFNULL` 防 NULL、`LEAST` 归一到 [0,1]。

📸 截图位：`qoder-04-hybrid-sql.png`

### 1.5 PowerContext 接入

**提示词：**

```
PowerContext 1.2.0 以 basic memory 模式运行在 127.0.0.1:8000（Bearer token 鉴权）。
写一个异步客户端：建项目 scope（幂等）、写记忆 /v1/memory/remember、召回 /v1/context/prepare、
列条目 /v1/memory/entries/list。要求：失败时降级（不影响主流程），每次调用追加到
logs/powercontext_calls.jsonl 以便人工核对。
```

**产出**：`app/pc.py`。

📸 截图位：`qoder-05-powercontext.png`

### 1.6 前端界面

**提示词：**

```
用原生 HTML/CSS/JS 写单页应用（不引入构建工具和 CDN），左侧栏 + 主区，七个视图：
概览 / 混合检索 / 资料库 / 任务看板 / 周报生成 / 长期记忆 / 技术证据。
风格：暖白纸感 + 墨黑文字 + 单一砖橙强调色，卡片细边框，等宽字体显示数字与 chunk#id。
检索结果要同时展示融合/全文/向量三个分数条，并提供权重滑块。
```

**产出**：`app/static/{index.html,style.css,app.js}`。

📸 截图位：`qoder-06-frontend.png`

### 1.7 调试记录（真实踩过的坑）

| 问题 | 现象 | 定位与修复 |
| --- | --- | --- |
| 全文 + 向量 + 过滤同时出现时返回空 | `WHERE MATCH(...) AND project=... ORDER BY d APPROXIMATE` 无结果 | `ORDER BY ... APPROXIMATE` 与额外过滤条件组合不生效，改为在 SQL 里算加权分再 `ORDER BY score`，保留索引参与打分 |
| `cosine_distance` 单独 `ORDER BY x APPROXIMATE` 返回空 | 但同一表达式在 SELECT 里能算出值 | 统一改为显式表达式排序，不再依赖 `APPROXIMATE` 关键字 |
| 周报正文出现标题行、编号残留 | 「10-27 风险同步 · 支付中台」「1. 决定…」被当成进展 | `extract.is_heading()` + `clean_sentence()` 去掉标题与列表序号 |
| 已完成任务被误判成待开始 | 「本周五前完成」命中 `完成` | 状态判定改为只看首个分句，并区分「完成 / 前完成」 |
| 记忆无限增长、重复写入 | 同一份资料重复导入会重复写记忆 | 记忆写入前按 `sha1(project|kind|text)` 去重，标记存 `meta` 表 |
| 8000 端口被旧服务占用导致 PowerContext 假死 | 健康检查返回 501 | 启动脚本改为校验 HTTP 200，而不是「端口能连上」 |

📸 截图位：`qoder-07-debug.png`

### 1.8 验证与发布

**提示词：**

```
写一个端到端验证脚本 scripts/verify_sample.py：
依次检查 seekdb/PowerContext 连通、导入资料、混合检索（并断言 SQL 里同时有 MATCH 和 cosine_distance）、
三路通道对比、记忆写入与召回、周报生成（断言带 chunk# 引用并召回记忆），
每步打印 PASS/FAIL，最后汇总 N/N 通过并以退出码反映结果。
```

**产出**：`scripts/verify_sample.py`（21 项检查）。

**发布**：在 Qoder 中通过 **Sites** 创建站点，上传 `site/` 目录内容，可见范围设为「任何人都可访问」。

📸 截图位：`qoder-08-verify.png`、`qoder-09-sites-publish.png`

---

## 2. 提交用摘要（可直接粘进平台）

| 项 | 内容 |
| --- | --- |
| 项目 | WorkPulse · 周报与项目记忆工作台 |
| 一句话 | 把纪要/聊天/文档变成可追溯的项目记忆，每周自动生成带出处的周报 |
| seekdb 的作用 | 文档与分块的存储；同一张表上的中文全文索引（ik）+ HNSW 向量索引；一条 SQL 完成全文/向量/结构化三路混合检索；任务、报告、检索日志落库 |
| PowerContext 的作用 | 保存周报口径、项目决策、任务状态与阶段结论；生成周报时召回并注入报告；Dashboard 与 `powercontext_calls.jsonl` 可核对 |
| 验证方式 | `python scripts/verify_sample.py` → 21/21 通过 |
| Qoder 的作用 | 需求拆解、架构设计、DDL/SQL/后端/前端编码、联调排错、验证脚本、Sites 发布 |
