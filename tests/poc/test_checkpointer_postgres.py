"""Day 1 POC：PostgresSaver 分布式 Checkpointer 可行性验证。

先决条件：docker compose up -d postgres（或本机 PostgreSQL）
运行（conda activate data-analysis-agent 后）：
    python tests/poc/test_checkpointer_postgres.py

验证点（对应 Day 1 量化指标「状态存储方案可行性」）：
  1. setup() 建表成功 —— 状态落盘到 Postgres
  2. 同一 thread_id 多轮 invoke —— 从 checkpoint 恢复旧状态后继续
  3. 第二个 saver 实例读同一 thread —— 模拟另一应用实例共享状态（多实例部署前提）

注：langgraph-checkpoint-postgres 版本演进可能微调 API，若报错以报错信息为准修正。
"""
from __future__ import annotations

import os
import sys
from typing import TypedDict

from dotenv import load_dotenv

load_dotenv()

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

CONN = os.environ.get("POSTGRES_DSN", "postgresql://agent:agent_pw@127.0.0.1:5432/agent_state")
THREAD = "poc-thread-1"


class State(TypedDict):
    value: int


def inc(state: State) -> dict:
    return {"value": state["value"] + 1}


def build_graph(saver):
    builder = StateGraph(State)
    builder.add_node("inc", inc)
    builder.add_edge(START, "inc")
    builder.add_edge("inc", END)
    return builder.compile(checkpointer=saver)


def main() -> int:
    with PostgresSaver.from_conn_string(CONN) as saver:
        saver.setup()  # 建 checkpoints / checkpoint_writes 表

        graph = build_graph(saver)
        config = {"configurable": {"thread_id": THREAD}}

        r1 = graph.invoke({"value": 1}, config)              # 1 -> 2
        r2 = graph.invoke({"value": r1["value"]}, config)    # 2 -> 3（恢复旧状态后继续）

        if r2["value"] != 3:
            print(f"[poc] FAIL: 状态恢复异常 r1={r1} r2={r2}")
            return 1

        # 模拟第二个应用实例：新连接读同一 thread，验证多实例共享状态
        with PostgresSaver.from_conn_string(CONN) as saver2:
            snap = saver2.get_tuple(config)
            if snap is None:
                print("[poc] FAIL: 第二个实例读不到 checkpoint")
                return 1
            got = snap.checkpoint["channel_values"]["value"]
            if got != 3:
                print(f"[poc] FAIL: 跨实例状态不一致 got={got}")
                return 1

        print(f"[poc] PASS: PostgresSaver 多实例共享状态 OK (value={r2['value']}, thread={THREAD})")
        return 0


if __name__ == "__main__":
    sys.exit(main())
