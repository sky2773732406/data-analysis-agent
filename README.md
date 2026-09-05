# data-analysis-agent

基于 **Reasonix + MCP + MySQL** 的生产级数据分析 Agent：自然语言查询与分析 MySQL 数据，目标支撑成百上千人并发。5 天冲刺交付，全部决策与量化指标见 [docs/architecture.md](docs/architecture.md) 与 [docs/performance_report.md](docs/performance_report.md)。

## 架构总览

```
用户 ──> Reasonix（DeepSeek agent，append-only 前缀缓存）
           ├─ /sales_trend /customer_segment /anomaly_detect（Skill playbook）
           └─ MCP 客户端 ──> 自研 MySQL MCP Server（app/mcp/server.py）
                              ├─ stdio（开发）/ streamable-http 独立服务（生产，无状态可水平扩展）
                              ├─ 只读白名单 + 单语句强制 + 表名白名单（防注入）
                              ├─ 连接池(20-50) + 30s 查询超时 + 1000 行上限
                              ├─ readOnlyHint 声明（Reasonix 只读会话默认放行）
                              ├─ PII 脱敏（name/phone 离开 server 前掩码）
                              └─ 结构化 JSON 日志 + trace_id（stderr）
                              └─> MySQL（只读账户 mcp_reader，仅 SELECT = 安全底线）
```

- **自研 MCP Server 而非 mysql-mcp-server**：后者 PyPI 全部版本要求 Python ≥3.11，与锁定的 3.10 冲突；且连接池/超时/行数/只读强制/EXPLAIN 等生产控制点应自持（[决策 5](docs/architecture.md)）
- **生产化路径**：streamable-http 独立服务已验证（initialize 8-16ms vs stdio ~900ms）；多实例水平扩展/nginx 认证网关为 [deploy/](deploy/) 参考工件，需 Linux 集群实测

## 5 天冲刺实测结果（摘要）

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 50 并发 P95 延迟 | <3s | **230-266ms**（12 倍余量，三跑稳定） |
| 单实例 QPS | 记录 | **112-121** |
| SQL 正确率 L1（20 题参考 SQL） | >90% | **100%** |
| LLM 层 L2（20 题结论） | >90% | **100%**（A/B 组 reasonix 实测，C 组含验收记录，口径见报告 §4.1） |
| 工具调用成功率 | >95% | **100%** |
| PII 脱敏覆盖率 | 100% | **双出口实测通过** |
| HA 演练（杀实例→恢复） | 通过 | **PASS**（30/30 → 0/10 预期失败 → 30/30） |
| 连接成功率 | 100% | **100%** |

## 快速开始

```bash
# 1. conda 环境（Python 3.10，与 Agent-Platform 一致）
conda create -n data-analysis-agent python=3.10 -y
conda activate data-analysis-agent
pip install -r requirements.txt
npm install -g reasonix@next        # 注意 @next：1.x Go 版；latest 仍是旧 0.x

# 2. 本地基础设施（Postgres + Redis 用 Docker；MySQL 用本机实例）
docker compose up -d
# 本机 MySQL 初始化：建 analytics 库 + customers/sales 表 + 只读账户 mcp_reader（纯新增）
# Windows 用 cmd 执行（PowerShell 不支持 < 重定向）：
cmd /c "mysql -u root -p < deploy/docker/mysql-init/01-init.sql"

# 3. 配置环境变量并造体积数据（~3300 单，含趋势/季节性/两处注入异常，供三 skill 评估）
copy .env.example .env
python scripts/seed_sales.py

# 4. 验证链路
python tests/poc/mcp_stdio_poc.py                     # stdio POC（查行数）
python tests/poc/test_checkpointer_postgres.py        # PostgresSaver 跨实例共享
python -m pytest tests/unit -q                        # 单测（36 用例：守卫/PII/日志）
python tests/golden/run_golden.py                     # golden L1（20 题回测）

# 5. Reasonix 会话（reasonix.toml 已声明 mysql MCP + skills）
reasonix chat
#   /mcp                          → 确认 mysql 已连接
#   /skills                       → 应看到三个 skill
#   /sales_trend 分析 sales 趋势  → skill 即斜杠命令（不是 /skill <name>）
```

## 测试与评估

| 层 | 工具 | 说明 |
| --- | --- | --- |
| 单元 | `pytest tests/unit` | 只读守卫/表名白名单/PII 掩码（36 用例，零 DB 依赖） |
| 正确性 L1 | `python tests/golden/run_golden.py` | 20 题参考 SQL 直跑 MCP server，结果版本化落盘 |
| 正确性 L2 | [tests/golden/prompts_l2.txt](tests/golden/prompts_l2.txt) | 同一 20 题以自然语言问 LLM（reasonix），按「应含要点」判分 |
| 压测 | `python -m tests.benchmark --concurrency 50` | 需先起 streamable-http 服务；测 P95/QPS/错误率 |
| HA 演练 | `python tests/poc/ha_drill.py` | 杀实例→重启→恢复验证（独立端口 8001） |
| 异常信号核对 | `python tests/poc/check_anomaly_signal.py` | 确定性核对注入异常的存在性与可检出百分位 |
| wire 协议探针 | `python tests/poc/raw_tools_list.py` | 直读线上 JSON-RPC 字节（验证 readOnlyHint 驼峰） |

## 目录结构（交付状态）

```
app/
  mcp/server.py        自研 MySQL MCP Server（4 工具 + 连接池/超时/行数/只读守卫/脱敏/日志）
  core/logging.py      结构化 JSON 日志（stderr，MCP stdio 协议约束）
skills/                Reasonix Skill（.md 格式，即斜杠命令）
  sales_trend.md       sales_trend 趋势分析 playbook
  customer_segment.md  RFM 客户分层 playbook（PII 红线）
  anomaly_detect.md    异常检测 playbook（周同比判定基准）
scripts/seed_sales.py  种子数据 v5（确定性 Poisson，含注入异常信号）
tests/
  unit/                守卫/PII 单测
  golden/              20 题问题集 + L1 回测 + L2 prompts + 结果落盘
  benchmark/           压测（python -m tests.benchmark）
  poc/                 POC 与探针（stdio/http/raw-wire/checkpoint/HA/信号核对）
deploy/
  docker/mysql-init/   建表 SQL（含只读账户）
  systemd/             生产服务单元（参考工件）
  nginx/               API Key 认证网关（参考工件）
docs/
  architecture.md      逐日决策记录 + 根因复盘
  performance_report.md 量化指标 + 限制说明 + 容量规划
  runbook.md            运维手册（部署/故障排查/备份）
```

## 关键决策与复盘（详见 docs/architecture.md）

1. **自研 MCP Server**（替代 mysql-mcp-server：Requires-Python 冲突 + 生产控制点自持）
2. **PostgresSaver** 分布式 Checkpointer（本地自托管；DynamoDBSaver 为云端替代，接口可切换）
3. **只读 MySQL 账户**（DB 层兜底：Agent 生成的 SQL 再错也写不了库）
4. **readOnlyHint 协议坑**：mcp 1.29.1 序列化 snake_case 违反规范 → 子类化修复 + raw wire 探针方法论
5. **种子信号工程 v2→v5**：分布选型决定信号可检测性（几何 σ≈μ → Poisson σ=√μ）
6. **连接池泄漏**：`try/except/else` + `return` 跳过 else → `try/finally`（症状伪装成容量不足）

## 已知限制（如实）

- 全部实测于 **Windows 单机 + 本机 MySQL**；多实例水平扩展线性度（≥80%）与集群级 99.9% 可用性需 2+ 台 Linux 补测（deploy/ 工件已备）
- 压测口径为**纯工具最坏情况**；真实端到端含 LLM 推理（秒级），需接入 Reasonix/Langfuse 观测给出完整链路
- Token 成本 / 缓存命中率依赖 DeepSeek 计费侧数据，官方口径长会话 90%+，本机未量化
