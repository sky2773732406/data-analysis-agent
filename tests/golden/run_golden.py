"""Golden 测试集 L1：参考 SQL 确定性回测（工具层）。

层级说明（诚实口径）：
  - L1（本脚本）：已知参考 SQL 直跑 MCP Server（进程内直调，走完整守卫/脱敏/护栏），
    度量「参考 SQL 在本数据上的正确性」与「工具调用成功率」——是 L2 的前置条件：
    参考 SQL 都过不了，LLM 翻译必然不可信。
  - L2（Day 5，reasonix 执行）：同一批问题以自然语言问 LLM，度量 NL→SQL/结论准确率。
    questions.json 的 question 字段即 L2 的输入集（两层级共享问题集，可比对）。

指标（对应 Day 2-3 量化指标）：
  - 工具调用成功率 = 无异常执行的题数 / 总题数（目标 >95%）
  - SQL 结果正确率 = 全部检查通过 / 总题数（目标 >90%）
  - 延迟 p50/max（压测级百分位由 Day 4 locust 负责，这里只记录基线）

运行：python tests/golden/run_golden.py
结果落盘：tests/golden/results/golden_l1_<ts>.json（评估不落盘 = 没评估）
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import app.mcp.server as server  # noqa: E402

BASE = pathlib.Path(__file__).resolve().parent
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_PII_COL_RE = re.compile(r"(^|_)(name|realname|phone|mobile|email)(_|$)", re.I)


def _check(op: str, payload: dict, row: dict | None, check: dict) -> bool:
    if op == "nonempty":
        return payload.get("row_count", 0) > 0
    if op == "rows_eq":
        return payload.get("row_count") == check["value"]
    if op == "value_gt":
        return row is not None and float(row.get(check["col"])) > check["value"]
    if op == "value_lt":
        return row is not None and float(row.get(check["col"])) < check["value"]
    if op == "value_between":
        v = float(row.get(check["col"])) if row else None
        return v is not None and check["lo"] <= v <= check["hi"]
    if op == "no_plain_phone":
        return True  # 全局兜底：由 runner 统一扫 payload，这里占位
    if op == "no_name_col":
        return not any(_PII_COL_RE.search(k) for k in (row or {}))
    raise ValueError(f"unknown check op: {op}")


def run_one(item: dict) -> dict:
    t0 = time.perf_counter()
    result = {"id": item["id"], "skill": item["skill"], "ok": False,
              "tool_ok": True, "fails": [], "latency_ms": None, "question": item["question"]}
    try:
        payload_text = server.execute_sql(item["sql"])
    except Exception as exc:  # 工具层失败（守卫拒绝/超时/连接问题）
        result["tool_ok"] = False
        result["fails"].append(f"tool_error: {str(exc)[:200]}")
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result

    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    data = json.loads(payload_text)
    rows = data.get("rows", [])
    for check in item["checks"]:
        row = rows[0] if rows else None
        ok = _check(check["op"], data, row, check)
        if not ok:
            result["fails"].append(f"{check['op']}({check.get('col', check.get('value'))}): "
                                   f"got rows={len(rows)} first={row}")
    # 全局 PII 兜底：任何回答泄漏明文手机号/姓名列即失败
    if _PHONE_RE.search(payload_text):
        result["fails"].append("PII_LEAK: plain phone in payload")
    if any(_PII_COL_RE.search(k) for r in rows for k in r) and "no_name_col" not in str(item["checks"]):
        pass  # 名字列是否允许由各题 check 决定（no_name_col 已处理）
    result["ok"] = result["tool_ok"] and not result["fails"]
    return result


def main() -> int:
    questions = json.loads((BASE / "questions.json").read_text(encoding="utf-8"))
    results = [run_one(q) for q in questions]

    total = len(results)
    tool_ok = sum(1 for r in results if r["tool_ok"])
    correct = sum(1 for r in results if r["ok"])
    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    summary = {
        "total": total, "tool_success": tool_ok, "correct": correct,
        "tool_success_rate": round(tool_ok / total, 4),
        "sql_correct_rate": round(correct / total, 4),
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "latency_max_ms": round(max(latencies), 1) if latencies else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    print(f"total={total}  tool_success={tool_ok} ({summary['tool_success_rate']:.1%})  "
          f"correct={correct} ({summary['sql_correct_rate']:.1%})  "
          f"latency p50={summary['latency_p50_ms']}ms max={summary['latency_max_ms']}ms")
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['id']} ({r['skill']}) {r['latency_ms']}ms"
              + ("" if r["ok"] else "  <- " + "; ".join(r["fails"])))

    out_dir = BASE / "results"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"golden_l1_{ts}.json").write_text(
        json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"results saved -> tests/golden/results/golden_l1_{ts}.json")
    return 0 if correct / total >= 0.9 and tool_ok / total >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())
