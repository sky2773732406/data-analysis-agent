# 架构设计文档（决策记录）

> 原则：每个决策都有量化指标或事实依据支撑；指标不达标启动根因分析流程（见文末）。
> 本文件按天追加，Day 5 汇入 `performance_report.md`。

## Day 1 — 环境搭建与 POC

### 1.1 已核实事实（2026-06 官方文档快照，避免凭记忆选型）

| 事实 | 来源 |
| --- | --- |
| mysql-mcp-server（designcomputer）PyPI 上**全部版本 Requires-Python >=3.11**（0.1.0–0.4.4），与项目 Python 3.10 冲突 → **选型否决** | PyPI 元数据 |
| 其工具集仅 3 个且无连接池/查询超时/行数上限等生产控制 → 生产控制点必须自研 | GitHub README |
| `mcp` Python SDK 要求 >=3.10；2.x 为破坏性重构 → 钉 `mcp>=1.28,<2`（官方迁移建议） | PyPI 元数据 |
| 自研 server 工具集：`execute_sql` / `get_schema_info` / `get_table_sample` / `explain_query`（原方案 explain_query 由此实现） | 本项目 app/mcp/server.py |
| streamable-http/SSE 传输无内置认证 → 公网暴露必须前置反代 + API Key | MCP 生态共识 + Day 4 设计 |
| 连接变量：`MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`（DATABASE 留空 = 多库模式）、`MYSQL_SSL_MODE`、`MYSQL_CONNECT_TIMEOUT`；服务端自动加载 cwd 的 .env | GitHub README |
| Reasonix 1.x 为 Go 重写版，**必须 `npm i -g reasonix@next`**（npm `latest` 标签仍是旧 0.x TS 版）；配置 `reasonix.toml`，MCP 声明用 `[[plugins]]`（stdio 默认 / `type="http"` 走 Streamable HTTP），工具暴露为 `mcp__<server>__<tool>` | reasonix.cn 文档 |
| Reasonix 官方缓存口径：append-only 循环，长会话缓存命中 **90%+**、输入 token 约 1/5 —— 本项目指标「≥99%」是冲刺目标，复盘如实标注差异 | reasonix.cn |

### 1.2 决策记录

**决策 1：MCP Server 部署模式 = stdio（开发）→ SSE 独立服务（生产）**
- 理由：stdio 把 MCP Server 绑死在 Agent 进程内，无法独立水平扩展；生产必须独立进程 + 反向代理，支撑「单实例 QPS 扩展线性度 ≥ 80%」指标。
- Day 4 待验证项：streamable-http 传输的无状态语义（新版协议已移除 `Mcp-Session-Id`）；届时用压测数据确认扩展线性度。

**决策 2：Checkpointer = PostgresSaver（本地自托管），DynamoDBSaver 为云端替代**
- 理由：本项目无 AWS 依赖，Postgres 用 docker-compose 本地起、LangGraph 官方推荐、迁移零成本；DynamoDB 仅在需要云端弹性/免运维时切换，接口同为 `BaseCheckpointSaver`，业务代码零改动。
- 验证：`tests/poc/test_checkpointer_postgres.py`（跨实例共享状态）✅/❌ 待跑。

**决策 3：MySQL 只读账户 `mcp_reader`（仅 SELECT）**
- 理由：Agent 生成的 SQL 不可信，DB 权限层兜底 —— 即使提示词注入/工具误用，也物理上无法写库。这是安全底线，不是可选项。

**决策 4：工具列表缓存（Day 4）**
- 依据：POC 脚本实测 tools/list schema 大小，作为 TTL=5min 缓存设计输入；服务端工具集固定（4 个），缓存安全。

**决策 5：自研 MySQL MCP Server（替代 mysql-mcp-server）** —— 用户确认（路线 A）
- 触发：`pip install mysql-mcp-server` 报错 —— 该包 PyPI 上全部版本 Requires-Python >=3.11，与「Python 3.10 与 Agent-Platform 一致」约束冲突（PyPI 元数据核实）。
- 决策：用 `mcp` SDK（FastMCP）+ pymysql 自研 `app/mcp/server.py`，保持 Python 3.10 不变。
- 附带收益：连接池（20-50）/ 30s 查询超时 / 1000 行上限 / 应用层只读白名单 / 单语句强制 / 表名白名单 / explain_query —— 全部生产控制点内建自持，不再依赖第三方实现细节，Day 4 指标直接命中。
- 安全层次：① 应用层只读白名单 + 单语句强制（防注入）→ ② 表名白名单（堵拼接点）→ ③ DB 层只读账户 mcp_reader（最终兜底）。
- 传输：stdio（开发）→ streamable-http（生产，Day 4）；钉 `mcp>=1.28,<2`（2.x 为破坏性重构，后续评估迁移）。

**决策 6：MySQL 使用本机已有实例（docker 容器弃用）** —— 用户确认
- 触发：`docker compose up -d` 报 `bind: Only one usage of each socket address` —— 宿主机 3306 已被本机 MySQL 占用。
- 决策：Agent 直接连本机 MySQL（127.0.0.1:3306），docker-compose 移除 mysql 服务（保留 postgres/redis）。
- 理由：本机实例即真实分析目标；01-init.sql 为纯新增操作（新库 analytics + 只读账户 mcp_reader），零侵入现有数据。
- 初始化：`mysql -u root -p < deploy/docker/mysql-init/01-init.sql`（docker 首启自动执行改为手动执行）。
- 注意：本机 MySQL 8 默认 caching_sha2_password，pymysql 非 SSL 全量认证需要 cryptography 包（已加入 requirements.txt）。

### 1.3 指标基线（Day 1 实测）

| 指标 | 目标 | 实测 | 备注 |
| --- | --- | --- | --- |
| 首次端到端查询响应时间（基准值） | 记录即可 | initialize 900.6ms / execute_sql 586.8ms | 冷启动基准：含 Python 进程启动 + 20 连接池预热；Day 4 压测测稳态 |
| MCP Server 连接成功率 | 100% | **100%**（POC PASS，退出码 0） | mcp_stdio_poc.py |
| 状态存储方案可行性 | 通过 | **通过**（value=3，跨实例共享） | test_checkpointer_postgres.py |
| 只读守卫单测 | - | **23 passed** | tests/unit/test_mysql_mcp_guard.py |
| tools/list schema 大小（缓存设计输入） | - | **652 B**（4 工具） | TTL=5min 缓存成本极低，决策 4 |

### 1.4 Day 1 复盘：踩坑记录

| # | 坑 | 根因 | 修复 | 教训 |
| --- | --- | --- | --- | --- |
| 1 | `pip install mysql-mcp-server` 装不上 | 该包全部版本 Requires-Python >=3.11，与锁定 3.10 冲突 | 选型否决 → 自研 server（决策 5） | 锁版本前先查 Requires-Python 元数据 |
| 2 | `mysql -u root -p < init.sql` 静默不执行 | PowerShell 不支持 `<` 输入重定向 | 改用 `cmd /c` 执行 | 脚本类操作先确认 shell 语义 |
| 3 | `ERROR 1406 Data too long for column` | SQL 文件 UTF-8 与客户端默认字符集（latin1/gbk）不匹配 | 脚本头部 `SET NAMES utf8mb4;` | 含中文的 SQL 必须显式声明会话字符集 |
| 4 | `McpError: Connection closed`（initialize 阶段） | `FastMCP.run()` 不接受 host/port 参数，服务器启动即崩 | host/port 移入构造函数 | 新 SDK API 先实测签名再写（与「先实测再选型」一致） |
| 5 | 正则 DeprecationWarning | `(?m)` 内联 flag 不在表达式开头 | 移除无用的 `(?m)`（无 `^` 锚点时无效） | 用 flags 参数而非内联 flag |
| 6 | reasonix chat 中文输出乱码 | Windows 控制台 GBK 码页渲染 UTF-8 | 纯展示层问题（LLM 读到正确数据）；`chcp 65001` 缓解 | 区分「展示层乱码」与「数据损坏」 |

---

## Day 2 — 核心功能开发（已完成，指标见 2.6）

### 2.1 数据底座 schema v2（RFM + PII 需求驱动）
- 变更：新增 `customers` 表（name/phone 为 PII，供脱敏演示）+ `sales.customer_id`（RFM 的 R/F/M 聚合依据）
- 种子 v2：200 客户（Pareto 活跃度，14 个零订单=沉默层天然素材）+ 2,417 单；`seed=42` 确定性可复现
- 已知偏差（如实记录）：订单数生成写成了几何分布（均值 p/(1-p)）而非设计意图的泊松（均值 λ），行数高于预期；趋势/季节性/注入异常信号结构不变

### 2.2 Reasonix Skill 落地
- **文件格式实测**：Reasonix 技能 = markdown 文件；`.skill` 扩展名不被扫描（/skills 不显示），改为 `.md` 即生效（与 global 技能目录 `*.md` 一致）；reasonix.toml `[skills] paths = ["./skills"]`
- **调用方式实测**：skill 本身即斜杠命令 `/sales_trend`（不是 `/skill <name>`）；`/skills` 只是管理器
- 三个 skill：`sales_trend` / `customer_segment` / `anomaly_detect`（skills/*.md），全程只读 SQL + PII 红线 + 结论结构化

### 2.3 readOnlyHint 协议坑（重点复盘）
- **现象**：reasonix 只读/计划会话拦截 mysql 工具（"forbid state mutation"），即使全是 SELECT
- **原理**：Reasonix 权限层对声明 `readOnlyHint: true` 的 MCP 工具默认放行只读会话 + 并行调度；未声明 = 视为可能改状态
- **根因链**：① mcp 1.29.1 `ToolAnnotations` 无驼峰 alias → 线上序列化为 snake_case `read_only_hint`，违反协议规范 `readOnlyHint` → Reasonix 按规范键解析认不出；② 补 alias_generator 无效 → raw wire 探针实测 SDK 序列化不走 `by_alias`
- **修复**：子类化 `ToolAnnotations`，用规范驼峰名直接作 pydantic 字段名（字段名 = wire 键，绕开 alias 机制）→ 探针确认 wire 输出 `{"readOnlyHint":true}`
- **方法论文**：客户端 SDK 模型会"洗白"线上字节（重新映射字段），验证协议真相必须用 raw wire 探针直接读 stdout（`tests/poc/raw_tools_list.py`）
- **验证结果**：reasonix `/sales_trend` 全通 —— 6 条只读 SELECT、聚合全在 MySQL 侧、数据对账一致（126,238,063.45 / 2,417 单）、报告遵循 skill 四段结构

### 2.4 种子信号工程：v2→v5 迭代复盘（"数据要有可测的地面真值"）
- **教训总纲**：评估类任务（golden 测试集、skill 准确率）要求数据里的注入信号**肉眼可见、统计显著**——信号若被分布噪声淹没，测出来的准确率没有意义，且永远无法区分"模型错了"还是"数据里根本没这回事"。
- **方法论**：LLM 检测有随机性与宿主护栏开销 → 数据级事实先用确定性脚本钉死（`tests/poc/check_anomaly_signal.py`：逐日明细 + 周环比 + **百分位硬指标**），不达标不进 LLM 环节。
- 迭代记录：
  - v2：几何分布 p=0.9 + cap=5 → 基线日普遍贴着 cap，×3 尖峰被 cap 吞掉 → 华南信号在数据里不可见（reasonix 首轮如实报告"信号与数据不符"——模型行为是对的，数据是错的）
  - v3：基线日均 1.1 单（过稀）→ 48% 日子零单，周环比噪声 ±500%+（top5 全 +566% 起跳）→ 华南 +103% 仅 74 百分位；华北低谷被周末单日大单掩盖成正增长
  - v4：日均 4.5 单 + qty 收窄 1-12 + 低谷 ×0.05 + 星期因子抹平 → 华北检出（1%），但**几何分布 σ≈μ**：华南尖峰日(μ≈13)实测 5/0/17/1/3，一半日子掉回基线 → 仅 59 百分位
  - v5：订单数改用 **Poisson（σ=√μ）** → 日单量稳定 → 华南 +270.3%（**100 百分位**）、华北 -49.3%（**4 百分位**），双双达标
- **知识点**：分布选型影响信号可检测性——几何分布 σ≈μ（均值无意义），Poisson σ=√μ（计数数据的正确模型）；注入异常要留信噪比余量（≥95/≤5 百分位），cap 不能吞放大倍数，金额长尾会掩盖低谷。

### 2.5 三 skill 实证验收（reasonix 端到端）

| Skill | 验收结果 | 质量信号 |
| --- | --- | --- |
| sales_trend | ✅ 趋势结论（半年近翻倍、逐月单调） | 6 条只读 SELECT、聚合全在 MySQL 侧、**数据对账一致**（126M/2417 单）、四段结构 |
| customer_segment | ✅ RFM 分层（重要价值 51 户 27.4% 贡献 77.5%） | 16 条 SELECT **零 PII 输出**；主动发现 R 口径不一致（模板 33/66 天 vs 样本分位 8/29）→ playbook 已修 |
| anomaly_detect | ✅ **双注入窗口检出**（华南 +270.3% / 华北 -49.3% 零单空窗），量级与确定性 checker 一致 | 方法 A 17 候选全部经星期复核剔除（盲区纪律执行）；配对回落周不重复计数；中位数+周环比双基准；边界声明 |

### 2.6 Day 2-3 量化指标（实测，2026-09-05）

| 指标 | 目标 | 实测 | 测量方式 |
| --- | --- | --- | --- |
| 工具调用成功率 | >95% | **100%**（20/20） | tests/golden/run_golden.py（L1）+ reasonix 三 skill 验收 |
| SQL 结果正确率（L1 参考 SQL） | >90% | **100%**（20/20） | run_golden.py（结果落盘 tests/golden/results/） |
| PII 脱敏覆盖率 | 100% | **双出口实测通过**（明文→掩码） | execute_sql/get_table_sample 直调对比 + 13 个脱敏单测 |
| SQL 稳态延迟 | 基准 | **p50 4.4ms**（首调 606ms = 20 连接池预热） | run_golden.py 计时（进程内口径） |
| NL→SQL 准确率（L2，LLM） | >90% | Day 5 执行（复用同一 questions.json 作输入集） | reasonix |
| 结构化日志 | - | tool_ok/tool_error + trace_id 验证通过 | 直调 + golden 运行期间日志流 |

- **L1/L2 口径说明（诚实）**：L1 用已知参考 SQL 直跑工具层，度量"参考 SQL 在本数据上的正确性"（100%）——是 L2 的前置门槛而非 LLM 翻译能力的度量；L2（自然语言→SQL/结论）留待 Day 5 与压测同期用同一问题集执行，两层级可比对。

---

## Day 4 — 生产化与性能优化（已完成，指标见 4.3）

### 4.1 传输层验证：stdio → streamable-http（2026-09-05 实测）
- 独立 HTTP 服务跑通：initialize 8-16ms vs stdio 子进程冷启动 ~900ms —— **传输层选型消灭了每连接 ~900ms 冷启动**（压测/报告素材）
- 无状态验证：两个独立 HTTP 连接均可完整 initialize/list/call（协议层无会话依赖）
- mcp 1.29.1 API 实测：`streamable_http_client`（新名，旧名废弃）yield 三元组 `(read, write, get_session_id)`

### 4.2 连接池泄漏 bug（Day 4 最重要复盘）
- **症状链**：pool 20 + 50 并发 → 大量 `connection pool exhausted after 30s`；调大池到 50 只是推迟爆炸。回溯发现两天前 reasonix「空错误」（当时归因复杂 SQL 被拒）**真凶即此泄漏**
- **根因**：`_execute` 里 `release()` 写在 `try/except/else` 的 **else 子句**——`try` 块内 `return` 会跳过 else，每次成功查询泄漏一个连接
- **修复**：`finally` 统一释放（`healthy` 标记区分成功/异常路径）
- **教训**：连接获取必须 `try/finally`，`else` + `return` 组合是静默泄漏；泄漏症状伪装成"容量不足"，调参只能推迟爆炸不能治愈

### 4.3 Day 4 量化指标（实测，池 20 单实例）

| 指标 | 目标 | 实测 | 备注 |
| --- | --- | --- | --- |
| 50 并发 P95 延迟 | < 3s | **230/266/242ms**（三跑） | 12 倍余量；压测口径：纯工具最坏情况 |
| 单实例 QPS | 实测记录 | **112-121** | 扩展线性度分母（Day 5 多实例对比） |
| 错误率 | <1% | **0**（3000 调用） | 泄漏修复后 |
| 稳定性 | - | 三跑无衰减 | 泄漏回归通过 |
| golden 回归 | >90% | 20/20（p50 2.8ms） | 修复未破坏功能 |

- **池口径结论**：真实 Agent 负载是 LLM 主导（工具调用间有秒级推理间隙），20 池够用；50 并发纯工具压测是**最坏情况上界**——两者区分写入性能报告，压测调参 ≠ 生产配置
- **认证设计**：FastMCP 无内置认证（生态共识）→ 网关认证模式（nginx auth_request + API Key），与 designcomputer 建议一致；本地开发直连 127.0.0.1 绕过

---

## 根因分析流程（指标不达标时执行）

1. **定位瓶颈层**：LLM 推理慢 → Reasonix 日志 LLM 响应时间；MCP Server 慢 → MySQL 慢查询日志 + 连接池饱和检查；网络/IO 慢 → 连接延迟 + 磁盘 IOPS；状态存储慢 → Checkpointer 读写延迟。
2. **决策与记录**：可修复（优化 Prompt / 调整连接池 / 加索引 / 调并发参数 / 加实例）→ 记录动作与修复后指标；硬件限制（显存、IOPS、swap、带宽）→ 如实记录瓶颈表现与突破成本。
