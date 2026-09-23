#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
针对「SSE 快照 + 事件游标」的一处回归测试。

背景：最初 job_events 先发一帧完整快照，然后又从 idx=0 重放全部历史事件，
于是客户端看到的日志会整段重复一遍。修法是让快照与游标一起取。

这个测试不联网、不跑模型，纯内存验证帧序列。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

# tools/ 下的脚本要能 import 到上一层的 server.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class FakeRequest:
    """最小的 Request 替身：永不认为客户端断开。"""

    async def is_disconnected(self) -> bool:
        return False


async def collect(job_id: str) -> list[dict]:
    resp = await server.job_events(job_id, FakeRequest())
    frames: list[dict] = []
    async for chunk in resp.body_iterator:
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
        for block in text.split("\n\n"):
            block = block.strip()
            if not block.startswith("data: "):
                continue
            frames.append(json.loads(block[6:]))
    return frames


async def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    job = server.Job(id="test-1", created=0.0, dir=tmp)
    job.status = "queued"

    # 建立一堆历史事件，模拟「任务早就开始了，客户端才连上来」
    for i in range(6):
        job.log(f"历史日志 {i}")
    job.set_stage("模型生成脚本中", 30.0)

    server.JOBS[job.id] = job

    # 流开始之后还会继续产生事件
    async def produce():
        await asyncio.sleep(0.05)
        job.log("新日志 A")
        job.set_stage("生成 Excel 报价表", 80.0)
        await asyncio.sleep(0.05)
        job.log("新日志 B")
        job.finish("done", stage="已完成")

    task = asyncio.create_task(produce())
    frames = await asyncio.wait_for(collect(job.id), timeout=15)
    await task

    kinds = [f.get("type") for f in frames]
    ok = True

    def check(label: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        print(("  [OK]   " if cond else "  [FAIL] ") + label + (f"  {extra}" if extra else ""))
        ok = ok and cond

    print("帧序列:", kinds)

    # 1) 第一帧必须是快照
    check("首帧为 snapshot", kinds[0] == "snapshot", str(kinds[:1]))

    # 2) 快照里带上了历史日志
    snap = frames[0]
    check("快照含 6 条历史日志", len(snap.get("logs", [])) == 6,
          f"实际 {len(snap.get('logs', []))}")

    # 3) 快照之后不得再出现历史日志（这正是当初的 bug）
    after = frames[1:]
    hist_replay = [f for f in after if f.get("type") == "log"
                   and str(f.get("line", "")).startswith("历史日志")]
    check("快照后未重放历史日志", not hist_replay,
          f"重放了 {len(hist_replay)} 条")

    # 4) 快照之后的日志恰好是新增的两条，且顺序正确
    new_logs = [f["line"] for f in after if f.get("type") == "log"]
    check("新增日志完整且有序", new_logs == ["新日志 A", "新日志 B"], str(new_logs))

    # 5) 末尾必须是 end，且状态为 done
    check("末帧为 end/done",
          kinds[-1] == "end" and frames[-1].get("status") == "done",
          str(frames[-1].get("status")))

    # 6) 全程无重复行
    check("全程无重复日志行", len(new_logs) == len(set(new_logs)))

    server.JOBS.pop("test-1", None)
    print("\n结论：" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
