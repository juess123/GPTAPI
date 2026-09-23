#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等待指定任务结束，并打印最终状态。用于流水线验收。"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

# 服务换成局域网监听后，这个工具仍然打本机回环最省事；
# 要在别的机器上跑就设 WEBAPP_BASE=http://192.168.x.x:8000
BASE = os.environ.get("WEBAPP_BASE", "http://127.0.0.1:8000")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    jobs = get("/api/jobs")["jobs"]
    if not jobs:
        print("没有任务")
        return 1
    jid = jobs[0]["id"]
    print(f"跟踪任务 {jid}")

    last_stage = None
    t0 = time.time()
    while True:
        j = get(f"/api/jobs/{jid}")
        if j["stage"] != last_stage:
            last_stage = j["stage"]
            print(f"  [{int(time.time()-t0):4d}s] {j['progress']:5.1f}%  {j['stage']}")
        if j["status"] in ("done", "failed"):
            print("\n最终状态 :", j["status"])
            print("耗时     :", j["elapsed"], "秒")
            print("阶段     :", j["stage"])
            if j["error"]:
                print("错误     :", j["error"])
            print("文件     :")
            for f in j["files"]:
                tag = "交付物" if f.get("primary") else "附属  "
                print(f"    [{tag}] {f['name']}  {f['size']} 字节")
            print("\n日志尾部：")
            for line in j["logs"][-14:]:
                print("   ", line[:200])
            return 0 if j["status"] == "done" else 2
        if time.time() - t0 > 1800:
            print("超时（30 分钟）")
            return 3
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
