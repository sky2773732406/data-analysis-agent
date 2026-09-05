# 运维手册（runbook）

> 适用对象：data-analysis-agent 的 MCP Server 生产部署（Linux 参考；本地 Windows 开发直接用终端前台跑）。
> 分层故障排查原则见 docs/architecture.md 末「根因分析流程」。

## 1. 部署拓扑（生产）

```
客户端(Reasonix/API) → nginx(API Key 认证 + HTTPS) → MCP Server ×N (systemd, streamable-http)
                                                      └─→ MySQL(只读账户 mcp_reader)
```

## 2. 环境准备

- Linux + conda env `data-analysis-agent`（Python 3.10，依赖见 requirements.txt）
- MySQL：执行 `deploy/docker/mysql-init/01-init.sql`（建库/表 + 只读账户 mcp_reader，纯新增）
- 体积数据（可选）：`python scripts/seed_sales.py`
- 配置：复制 `.env.example` → `.env`，按需改 `MYSQL_*` / `MYSQL_POOL_SIZE` / `PII_MASKING` / `MCP_SSE_*` / `MCP_API_KEY`

## 3. 服务管理（systemd）

```bash
sudo cp deploy/systemd/mysql-mcp-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mysql-mcp-server

# 常用操作
systemctl status mysql-mcp-server      # 状态
journalctl -u mysql-mcp-server -f      # 日志（JSON 行，含 trace_id）
systemctl restart mysql-mcp-server     # 重启（优雅停机 30s 存量请求）
```

多实例水平扩展：复制 service 文件（改 `ExecStart` 端口或前置 LB）+ nginx upstream 加实例；每实例池大小按并发配（池/实例 ≈ 并发/实例，本机实测 50 并发可用池 20）。

## 4. 网关（nginx，参考 deploy/nginx/mcp-server.conf）

- 认证：`Authorization: Bearer <MCP_API_KEY>`，map 校验失败返 401（密钥文件仅 root 可读）
- 转发要求：`proxy_buffering off`（streamable-http 事件流）、`proxy_read_timeout 300s`
- HTTPS 证书 + `proxy_set_header Host $host`

## 5. 升级与回滚

1. 备份 `.env` 与数据库（见 §6）
2. 拉新代码 → `pip install -r requirements.txt`（若有依赖变更）
3. `systemctl restart mysql-mcp-server`（优雅停机：存量请求最多等 30s）
4. 冒烟：`python tests/poc/mcp_http_poc.py`（指向新实例）
5. 回滚：git checkout 旧版本 + 重启

## 6. 备份

- MySQL：`mysqldump analytics > analytics_$(date +%F).sql`（销售/客户为种子数据，可随时 seed 重建；生产真实库按业务备份策略）
- 配置：`.env`（含密钥）单独加密备份
- 演练：每季度在临时实例恢复一次 dump 验证

## 7. 故障排查（按层）

| 症状 | 检查 | 处置 |
| --- | --- | --- |
| 客户端 Connection closed / 超时 | `journalctl` 看 tool_error（显式错误已替代空错误）；池是否耗尽 | 调 `MYSQL_POOL_SIZE` / 加实例；查是否又引入连接泄漏（见根因 #3） |
| 慢查询 | `mcp__mysql__explain_query` 分析执行计划；MySQL slow log | 加索引 / 改 SQL |
| 只读会话仍拦工具 | 确认工具声明 readOnlyHint（raw wire 探针 `python tests/poc/raw_tools_list.py` 看线上键名） | server 代码问题查 git 历史 |
| 中文乱码 | Windows 控制台 GBK（展示层） | `chcp 65001`；数据层确认连接 charset=utf8mb4 |
| 写入被拒（预期） | 只读白名单 + DB 只读账户双层 | 非故障 |
| MySQL 连接失败 | `mysql -u mcp_reader -p -e "SELECT 1"` | 检查账户/网络/SSL 模式 |

## 8. 监控与告警

- 基础设施：Prometheus + Grafana（CPU/内存/磁盘/网络）——参考工件，尚未提交配置
- 业务：MCP Server 结构化日志（stderr）集中采集；按 `tool` / `latency_ms` / `error` 建面板
- 告警建议：tool_error 率 >1%（5min）、P95 >3s（5min）、进程重启次数 >0
- LLM 层（Reasonix/Langfuse）：待接入，见性能报告 §5.2
