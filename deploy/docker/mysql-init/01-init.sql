-- ============================================================
-- MySQL 初始化脚本（本机 MySQL 手动执行；幂等，可重复执行）
-- 1. 示例表：customers（客户，name/phone 为 PII —— 供 PII 脱敏演示与 RFM 分层）
--            sales（订单流水，含 customer_id 客户维度 —— 供三个 skill 使用）
-- 2. 只读账户 mcp_reader：仅授予 SELECT —— 生产安全底线（MCP Server 只读模式）
-- 执行：cmd /c "mysql -u root -p < deploy/docker/mysql-init/01-init.sql"
-- 体积种子（~2800 行）另行执行：python scripts/seed_sales.py
-- ============================================================

-- 强制会话字符集为 utf8mb4：文件是 UTF-8，若客户端/服务器默认字符集非 UTF-8
-- （中文 Windows 常见 latin1/gbk），中文会被错误解码导致 Data too long 等报错
SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS analytics;
USE analytics;

-- ---------- 客户表（name/phone 为 PII，进入 LLM 前必须脱敏） ----------
DROP TABLE IF EXISTS customers;
CREATE TABLE customers (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(32) NOT NULL COMMENT '客户姓名（PII）',
  phone       VARCHAR(20) NOT NULL COMMENT '手机号（PII）',
  region      VARCHAR(32) NOT NULL,
  created_at  DATE        NOT NULL,
  KEY idx_region (region)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 销售流水表（customer_id = RFM 分层依据） ----------
DROP TABLE IF EXISTS sales;
CREATE TABLE sales (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT          NOT NULL,
  order_date  DATE         NOT NULL,
  region      VARCHAR(32)  NOT NULL,
  product     VARCHAR(64)  NOT NULL,
  quantity    INT          NOT NULL,
  amount      DECIMAL(10,2) NOT NULL,
  KEY idx_order_date (order_date),
  KEY idx_customer_id (customer_id),
  KEY idx_region (region)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 最小 POC 种子（8 客户 + 16 单；体积种子见 scripts/seed_sales.py） ----------
INSERT INTO customers (id, name, phone, region, created_at) VALUES
  (1, '张伟',   '13800000001', '华东', '2025-08-12'),
  (2, '李娜',   '13900000002', '华南', '2025-09-03'),
  (3, '王强',   '13700000003', '华北', '2025-10-21'),
  (4, '刘敏',   '13600000004', '华东', '2025-11-05'),
  (5, '陈杰',   '13500000005', '华南', '2025-12-15'),
  (6, '杨丽',   '13800000006', '华北', '2026-01-08'),
  (7, '赵磊',   '13900000007', '西南', '2026-02-14'),
  (8, '黄静',   '13700000008', '西南', '2026-03-01');

INSERT INTO sales (customer_id, order_date, region, product, quantity, amount) VALUES
  (1, '2026-04-01', '华东', '标准版', 12,  24000.00),
  (2, '2026-04-02', '华南', '标准版', 8,   16000.00),
  (3, '2026-04-03', '华北', '高级版', 5,   15000.00),
  (4, '2026-04-07', '华东', '企业版', 2,   12000.00),
  (2, '2026-04-10', '华南', '高级版', 6,   18000.00),
  (3, '2026-04-14', '华北', '标准版', 10,  20000.00),
  (1, '2026-04-18', '华东', '高级版', 4,   12000.00),
  (5, '2026-04-22', '华南', '企业版', 1,    6000.00),
  (6, '2026-04-25', '华北', '高级版', 7,   21000.00),
  (1, '2026-04-29', '华东', '标准版', 15,  30000.00),
  (2, '2026-05-06', '华南', '标准版', 9,   18000.00),
  (7, '2026-05-08', '西南', '企业版', 3,   18000.00),
  (8, '2026-05-12', '西南', '高级版', 5,   15000.00),
  (5, '2026-05-15', '华南', '高级版', 8,   24000.00),
  (6, '2026-05-19', '华北', '标准版', 11,  22000.00),
  (4, '2026-05-20', '华东', '企业版', 2,   12000.00);

-- ---------- 只读账户（MCP Server 专用；生产环境同样用最小权限只读账户） ----------
CREATE USER IF NOT EXISTS 'mcp_reader'@'%' IDENTIFIED BY 'mcp_reader_pw';
GRANT SELECT ON analytics.* TO 'mcp_reader'@'%';
-- 本账户仅 SELECT，无 INSERT/UPDATE/DELETE/DDL —— 即使 Agent 生成恶意/错误 SQL 也无法写库
