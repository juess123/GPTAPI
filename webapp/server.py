#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A' 流水线 · Web 后端

一个任务的生命周期：

    POST /api/jobs  （multipart 上传 图片 / md / xlsx）
        ↓  写入 workspaces/<job_id>/input/
    ① python pipeline/ask_model.py --input-dir … --gen-dir …
        ↓  产出 workspaces/<job_id>/generated/make_xlsx.py + make_blend.py
    ② python pipeline/build.py --gen-dir … --out-dir …
        ↓  产出 workspaces/<job_id>/output/<时间戳>/final_quote.xlsx + final_model.blend

    GET  /api/jobs/<id>/events          SSE 实时进度与日志
    GET  /api/jobs/<id>/download/<name> 下载成品
    GET  /api/jobs                     历史任务列表

设计取舍：
  · 固定并发 N 个任务（默认 2，WEBAPP_PARALLEL 可调），共用一个队列。
    多出来的任务在队列里排队，先到先做。每个任务的目录、脚本、产物完全隔离，
    所以并行不会互相踩；但真正跑 Blender 那一段会吃满 CPU —— 所以 N 不宣大。
  · 复用 ask_model.py / build.py 作为子进程，而不是 import，
    这样这两个脚本始终是唯一事实来源，Web 层只做编排与呈现。
  · 任务的元数据落盘成 job.json，重启服务后历史仍可查、成品仍可下载。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# 注意：手动解析 multipart 表单时，拿到的是 Starlette 的 UploadFile，
# 而 fastapi.UploadFile 只是它的子类 —— 用 fastapi 的那个做 isinstance 会永远为假。
from starlette.datastructures import UploadFile

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent            # E:\GPTAPI3\webapp
ROOT = HERE.parent                                # E:\GPTAPI3
PIPELINE = ROOT / "pipeline"
STATIC = HERE / "static"
WORKSPACES = HERE / "workspaces"

XLSX_NAME = "final_quote.xlsx"
BLEND_NAME = "final_model.blend"
DELIVERABLES = [XLSX_NAME, BLEND_NAME]

MAX_PROMPT_CHARS = 20_000

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXTS = {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".ini", ".log"}
XLSX_EXTS = {".xlsx", ".xlsm"}
ALLOWED_EXTS = IMAGE_EXTS | TEXT_EXTS | XLSX_EXTS

# 网关单次请求超时（与 .env 的 CNXMAI_TIMEOUT 保持一致即可）
MODEL_TIMEOUT_HINT = 40 * 60

# 整条流水线的最长墙钟时间，超时判定失败
JOB_DEADLINE = 3 * 60 * 60


def _read_parallel() -> int:
    """同一时刻最多几个任务在跑。多余的在队列里排队，先到先做。

    一个任务绝大部分时间在等模型返回（纯网络 I/O，几乎不吃本机），
    所以稍微开大是净收益；但真正跑 Blender 那一段会吃满 CPU，
    开太多反而每个任务都变慢，也更容易撞上网关的并发限流。
    """
    raw = os.environ.get("WEBAPP_PARALLEL", "").strip()
    try:
        n = int(raw) if raw else 2
    except ValueError:
        n = 2
    return max(1, min(8, n))


MAX_PARALLEL = _read_parallel()

# 进度分配：模型生成是大头
P_UPLOAD = 6
P_SCAN = 12
P_MODEL_LO = 14
P_MODEL_HI = 72
P_XLSX = 80
P_BLEND = 92
P_DONE = 100


# --------------------------------------------------------------------------
# 控制台播报
#
# 只打「任务级」的信息：谁提交了什么、跑到哪一步、成没成、谁下载了。
# 前端每 15 秒来一次的 /api/jobs 轮询已经在 QuietAccessLog 里压掉了，
# 否则那些 200 刷屏几秒钟就把真正要看的东西顶出屏幕。
# --------------------------------------------------------------------------

def note(msg: str, tag: str = "") -> None:
    """打一条带时间戳的播报。flush 一下，重定向到文件时也能实时看到。

    tag 是任务号后 6 位。并发跑的时候几个任务的阶段行会交错，
    带上短号才分得清哪一行是属于谁的；系统级消息不带号。
    """
    who = f"[{tag}] " if tag else ""
    print(f"[{time.strftime('%H:%M:%S')}] {who}{msg}", flush=True)


def human_size(n: float) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def human_secs(s: float) -> str:
    s = max(0, int(s))
    h, m = divmod(s // 60, 60)
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s % 60} 秒"
    return f"{s} 秒"


def rel(p: Path) -> str:
    """路径尽量显示成相对项目根目录的，短一点也好找。"""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def client_of(request: Request) -> str:
    """请求来自哪台机器。回环直接写成「本机」，比 127.0.0.1 一眼就懂。"""
    host = request.client.host if request.client else "?"
    return "本机" if host in ("127.0.0.1", "::1", "localhost") else host


# --------------------------------------------------------------------------
# 任务模型
# --------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    created: float
    dir: Path
    status: str = "queued"          # queued | running | done | failed
    stage: str = "排队中"
    progress: float = 0.0
    error: str = ""
    started: float | None = None
    finished: float | None = None
    files: list[dict] = field(default_factory=list)   # [{name, size, kind}]
    inputs: list[dict] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def tag(self) -> str:
        """控制台里显示用的短号：任务号最后 6 位。"""
        return self.id[-6:]

    # ---- 事件流 ----
    def emit(self, **kw) -> dict:
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, **kw}
            self.events.append(ev)
            return ev

    def log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line)
            try:
                with (self.dir / "pipeline.log").open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line + "\n")
            except OSError:
                pass
        self.emit(type="log", line=line)

    def set_stage(self, stage: str, progress: float | None = None) -> None:
        with self.lock:
            self.stage = stage
            if progress is not None:
                self.progress = progress
        self.emit(type="stage", stage=self.stage, progress=self.progress)
        # 「排队中」在提交那一行已经说过了，不重复
        if stage != "排队中":
            note(f"· {stage}　（已用时 {human_secs(self.elapsed)}）", self.tag)

    def finish(self, status: str, *, error: str = "", stage: str = "") -> None:
        with self.lock:
            self.status = status
            if error:
                self.error = error
            if stage:
                self.stage = stage
            if status == "done":
                self.progress = P_DONE
                self.stage = stage or "已完成"
            self.finished = time.time()
        self.emit(
            type="end",
            status=self.status,
            stage=self.stage,
            progress=self.progress,
            error=self.error,
            files=self.files,
            started=self.started,
            finished=self.finished,
            elapsed=self.elapsed,
        )
        if status == "done":
            note(f"✔ 完成　历时 {human_secs(self.elapsed)}", self.tag)
            if self.files:
                note("  产物: " + "　·　".join(
                    f"{f['name']} {human_size(f['size'])}" for f in self.files),
                    self.tag)
        else:
            note(f"✘ 失败　历时 {human_secs(self.elapsed)}", self.tag)
            note(f"  原因: {self.error or '未知'}", self.tag)

    @property
    def elapsed(self) -> float:
        end = self.finished or time.time()
        return max(0.0, end - (self.started or self.created))

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 1),
            "error": self.error,
            "created": self.created,
            # 开始/结束的墙上时间，前端结果卡要显示「几点开始、几点结束」
            "started": self.started,
            "finished": self.finished,
            "elapsed": round(self.elapsed, 1),
            "files": self.files,
            "inputs": self.inputs,
            "logs": self.logs[-400:],
            "downloadable": self.status == "done",
        }

    def to_disk(self) -> None:
        try:
            (self.dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": self.id,
                        "status": self.status,
                        "stage": self.stage,
                        "progress": self.progress,
                        "error": self.error,
                        "created": self.created,
                        "started": self.started,
                        "finished": self.finished,
                        "files": self.files,
                        "inputs": self.inputs,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
QUEUE: "queue.Queue[str]" = queue.Queue()


def running_count() -> int:
    """当前真正在跑的任务数。"""
    with JOBS_LOCK:
        items = list(JOBS.values())
    return sum(1 for j in items if j.status == "running")


def queue_state() -> dict:
    """队列概况，给控制台和接口用。"""
    return {
        "running": running_count(),
        "waiting": QUEUE.qsize(),
        "parallel": MAX_PARALLEL,
    }


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def safe_name(name: str) -> str:
    """把上传的文件名洗成安全的相对文件名（保留中文，去掉路径分量）。"""
    base = Path(name or "").name
    base = base.replace("\\", "/").split("/")[-1]
    base = _SAFE.sub("_", base).strip("._") or "file"
    return base[:120]


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in XLSX_EXTS:
        return "xlsx"
    return "text"


def kind_label(kind: str) -> str:
    return {"image": "参考图片", "xlsx": "材料价目表"}.get(kind, "规格文档")


def pct(payload: dict) -> dict:
    return payload


def dedupe_name(used: set[str], name: str) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        cand = f"{stem}({n})" + (f".{ext}" if ext else "")
        if cand not in used:
            used.add(cand)
            return cand
        n += 1


# --------------------------------------------------------------------------
# 子进程执行
# --------------------------------------------------------------------------

def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_streamed(cmd: list[str], job: Job, *, cwd: Path | None = None) -> int:
    """执行子进程，把stdout/stderr逐行转发到任务日志。"""
    import subprocess

    job.log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
            env=child_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as e:
        job.log(f"[错误] 无法启动子进程: {e}")
        return 127

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.strip():
                job.log(line)
    except Exception as e:                                  # noqa: BLE001
        job.log(f"[警告] 读取子进程输出中断: {e}")
    proc.wait()
    return proc.returncode


# --------------------------------------------------------------------------
# 单个任务的完整流程
# --------------------------------------------------------------------------

def collect_deliverables(job: Job, run_dir: Path) -> None:
    """扫描运行目录，把成品登记进 job.files。"""
    found: list[dict] = []
    for p in sorted(run_dir.iterdir()):
        if not p.is_file():
            continue
        found.append({
            "name": p.name,
            "size": p.stat().st_size,
            "kind": "deliverable" if p.name in DELIVERABLES else "extra",
            "primary": p.name in DELIVERABLES,
        })
    # 两个主交付物排前面
    found.sort(key=lambda f: (not f["primary"], f["name"]))
    with job.lock:
        job.files = found


def run_job(job: Job) -> None:
    job.started = time.time()
    job.status = "running"
    job.set_stage("解析上传材料", P_SCAN)

    for f in job.inputs:
        job.log(f"  [投料] {f['name']}  ({f['size']} 字节 · {kind_label(f['kind'])})")

    gen_dir = job.dir / "generated"
    out_root = job.dir / "output"
    task_packages = job.dir / "task_packages"

    # 每个任务开始前检查锁定白名单。安装操作由跨进程目录锁串行化；
    # 已满足时只读取本地元数据，不访问网络，也不运行 pip。
    job.log("  [环境] 检查项目本地依赖")
    rc = run_streamed(
        [sys.executable, str(PIPELINE / "dependency_manager.py"), "--ensure"],
        job, cwd=ROOT,
    )
    if rc != 0:
        job.finish("failed", error="项目依赖检查或自动安装失败")
        return
    gen_dir.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    job.set_stage("快速生成中", P_MODEL_LO)
    job.log("")
    job.log("─" * 58)
    job.log("一次生成两个脚本 → Blender 实体与数据 → Excel 报价")
    job.log("─" * 58)
    job.log("  [模式] 快速直出模式")
    rc = run_streamed(
        [sys.executable, str(PIPELINE / "fast_workflow.py"),
         "--input-dir", str(job.dir / "input"),
         "--gen-dir", str(gen_dir),
         "--out-dir", str(out_root),
         "--task-packages", str(task_packages)],
        job, cwd=ROOT,
    )
    latest = out_root / "LATEST_RUN.txt"
    run_dir = Path(latest.read_text(encoding="utf-8").strip()) if latest.is_file() else None
    if rc != 0 or run_dir is None or not run_dir.is_dir():
        job.finish("failed", error=f"快速生成流水线失败（exit={rc}）")
        return
    have = {p.name for p in run_dir.iterdir() if p.is_file()}
    missing = [name for name in DELIVERABLES if name not in have]
    if missing:
        job.finish("failed", error=f"缺少交付物 {', '.join(missing)}")
        return
    collect_deliverables(job, run_dir)
    (job.dir / "RUN_DIR.txt").write_text(str(run_dir), encoding="utf-8")
    job.set_stage("已完成", P_DONE)
    job.finish("done", stage="已完成")
    note(f"  目录: {rel(run_dir)}", job.tag)
    return

    # ---- 步骤 1: 让模型写生成脚本 ----
    job.set_stage("模型生成脚本中", P_MODEL_LO)
    job.log("")
    job.log("─" * 58)
    job.log("步骤 1/2  请求模型生成 make_xlsx.py 与 make_blend.py")
    job.log("─" * 58)

    t0 = time.time()
    stop_monitor = threading.Event()

    def monitor() -> None:
        """模型阶段没有真实进度可读，用时间做渐近估算，仅用于界面观感。"""
        while not stop_monitor.wait(1.0):
            el = time.time() - t0
            # 渐近曲线：tau=180s 时约到 63%，不会顶到上限，完成时再跳到 P_MODEL_HI
            frac = 1.0 - pow(2.718281828, -el / 180.0)
            p = P_MODEL_LO + (P_MODEL_HI - P_MODEL_LO) * frac
            with job.lock:
                job.progress = round(p, 1)
            job.emit(type="stage", stage="模型生成脚本中",
                     progress=job.progress, elapsed=round(el, 1))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        rc = run_streamed(
            [sys.executable, str(PIPELINE / "ask_model.py"),
             "--input-dir", str(job.dir / "input"),
             "--gen-dir", str(gen_dir)],
            job, cwd=ROOT,
        )
    finally:
        stop_monitor.set()
        mon.join(timeout=2)

    if rc != 0:
        job.log("")
        job.finish("failed", error=f"模型生成脚本失败（exit={rc}）")
        return

    for name in ("make_xlsx.py", "make_blend.py"):
        p = gen_dir / name
        if not p.exists():
            job.finish("failed", error=f"模型没有产出 {name}")
            return
    job.log(f"  ↳ 模型耗时 {time.time() - t0:.0f}s")
    job.set_stage("模型脚本已就绪", P_MODEL_HI)

    # 模型可以为本任务声明任意 PyPI 依赖；分别安装到任务自己的
    # Python / Blender 目录，不改变共享基础环境。
    dependencies = gen_dir / "dependencies.json"
    if not dependencies.is_file():
        dependencies.write_text('{"python": [], "blender": []}\n', encoding="utf-8")
    job.log("  [环境] 准备任务专属动态工具箱")
    rc = run_streamed(
        [sys.executable, str(PIPELINE / "dependency_manager.py"),
         "--ensure-task", "--dependencies", str(dependencies),
         "--task-packages", str(task_packages)],
        job, cwd=ROOT,
    )
    if rc != 0:
        job.finish("failed", error="任务级依赖自动安装失败")
        return

    # ---- 步骤 2: 本地执行，落盘成品 ----
    job.log("")
    job.log("─" * 58)
    job.log("步骤 2/2  本机执行脚本，生成 xlsx 与 blend")
    job.log("─" * 58)
    job.set_stage("生成 Excel 报价表", P_XLSX)

    rc = run_streamed(
        [sys.executable, str(PIPELINE / "build.py"),
         "--gen-dir", str(gen_dir),
         "--out-dir", str(out_root),
         "--task-packages-dir", str(task_packages)],
        job, cwd=ROOT,
    )

    job.set_stage("校验成品", P_BLEND)

    # 找到本次运行目录（build.py 每次新建一个时间戳目录）
    runs = [d for d in out_root.iterdir() if d.is_dir()]
    runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    run_dir = runs[0] if runs else None

    if run_dir is None:
        job.finish("failed", error="build.py 未产生任何输出目录")
        return

    have = {p.name for p in run_dir.iterdir() if p.is_file()}
    missing = [n for n in DELIVERABLES if n not in have]
    if missing:
        job.log("")
        job.log(f"[错误] 缺少交付物: {missing}")
        job.finish("failed",
                   error=f"缺少交付物 {', '.join(missing)}"
                         + (f"（build.py exit={rc}）" if rc != 0 else ""))
        return

    collect_deliverables(job, run_dir)
    (job.dir / "RUN_DIR.txt").write_text(str(run_dir), encoding="utf-8")

    if rc != 0:
        job.log("")
        job.log(f"[提示] build.py 返回 {rc}，但两个交付物都已生成，判定为成功。")

    job.set_stage("已完成", P_DONE)
    job.finish("done", stage="已完成")
    note(f"  目录: {rel(run_dir)}", job.tag)


def worker() -> None:
    while True:
        job_id = QUEUE.get()
        job = JOBS.get(job_id)
        if job is None:
            QUEUE.task_done()
            continue
        try:
            # +1：本任务是刚拿到、马上一要变 running 的那个
            note(f"▶ 开始执行　（并发 {running_count() + 1}/{MAX_PARALLEL}）",
                 job.tag)
            run_job(job)
        except Exception as e:                              # noqa: BLE001
            import traceback
            job.log("[异常] " + traceback.format_exc())
            job.finish("failed", error=f"内部错误: {type(e).__name__}: {e}")
        finally:
            job.to_disk()
            QUEUE.task_done()


# --------------------------------------------------------------------------
# 启动时恢复历史
# --------------------------------------------------------------------------

def restore_history() -> None:
    if not WORKSPACES.is_dir():
        return
    for d in sorted(WORKSPACES.iterdir(), reverse=True):
        meta = d / "job.json"
        if not d.is_dir() or not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        job = Job(id=data.get("id", d.name), created=data.get("created", d.stat().st_mtime),
                  dir=d)
        job.status = data.get("status", "done")
        job.stage = data.get("stage", "")
        job.progress = data.get("progress", 0.0)
        job.error = data.get("error", "")
        job.started = data.get("started")
        job.finished = data.get("finished")
        job.files = data.get("files", [])
        job.inputs = data.get("inputs", [])
        log_file = d / "pipeline.log"
        if log_file.is_file():
            try:
                job.logs = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            except OSError:
                pass
        if job.status in ("running", "queued"):
            job.status = "failed"
            job.error = job.error or "服务重启导致任务中断"
            job.stage = "已中断"
        JOBS[job.id] = job


# --------------------------------------------------------------------------
# FastAPI
# --------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动：准备目录、恢复历史、拉起后台工作线程。"""
    WORKSPACES.mkdir(parents=True, exist_ok=True)
    restore_history()

    # 起 MAX_PARALLEL 个线程一起消费同一个队列。
    # queue.Queue 自带锁：每个线程 get() 一个任务、做完 task_done()，
    # 所以「同时最多 N 个在跑、多出来的排队」是天然成立的，不需要额外调度。
    for i in range(MAX_PARALLEL):
        threading.Thread(target=worker, daemon=True,
                         name=f"worker-{i + 1}").start()

    note(f"服务就绪　并发上限 {MAX_PARALLEL}"
         + (f"　（WEBAPP_PARALLEL={MAX_PARALLEL}）" if MAX_PARALLEL != 2 else ""))
    yield


app = FastAPI(title="模型生成流水线", docs_url=None, redoc_url=None,
              lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/config")
async def config() -> dict:
    return {
        "accept": sorted(ALLOWED_EXTS),
        "primary": {
            "图片": sorted(IMAGE_EXTS),
            "Markdown": [".md"],
            "Excel": sorted(XLSX_EXTS),
        },
        "imageLimitMB": 4,
        "queue": QUEUE.qsize(),
        "parallel": MAX_PARALLEL,
    }


@app.post("/api/jobs")
async def create_job(request: Request) -> JSONResponse:
    form = await request.form()
    uploads: list[UploadFile] = [v for k, v in form.multi_items()
                                 if isinstance(v, UploadFile)]
    prompt_value = form.get("prompt", "")
    prompt = prompt_value.strip() if isinstance(prompt_value, str) else ""
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(400, f"补充提示词不能超过 {MAX_PROMPT_CHARS} 个字符")
    if not uploads and not prompt:
        raise HTTPException(400, "没有收到文件或文字提示词")

    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    job_dir = WORKSPACES / job_id
    in_dir = job_dir / "input"
    in_dir.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    total = 0
    inputs: list[dict] = []
    rejected: list[str] = []

    if prompt:
        prompt_data = prompt.encode("utf-8")
        prompt_name = "用户补充提示词.md"
        (in_dir / prompt_name).write_bytes(prompt_data)
        total += len(prompt_data)
        used.add(prompt_name)
        inputs.append({"name": prompt_name, "size": len(prompt_data), "kind": "text"})

    for up in uploads:
        name = dedupe_name(used, safe_name(up.filename or "file"))
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXTS:
            rejected.append(f"{name}（不支持的格式 {ext or '无扩展名'}）")
            continue

        target = in_dir / name
        size = 0
        with target.open("wb") as stream:
            while chunk := await up.read(1024 * 1024):
                stream.write(chunk)
                size += len(chunk)
        if size == 0:
            target.unlink(missing_ok=True)
            rejected.append(f"{name}（空文件）")
            continue
        total += size
        inputs.append({"name": name, "size": size, "kind": kind_of(target)})

    if not inputs:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "没有可用的文件：" + "；".join(rejected))

    job = Job(id=job_id, created=time.time(), dir=job_dir)
    job.inputs = inputs
    job.emit(type="init", job=job.snapshot())

    with JOBS_LOCK:
        JOBS[job_id] = job
    job.log(f"任务 {job_id} 已创建，共 {len(inputs)} 个文件 / {total} 字节")
    for r in rejected:
        job.log(f"  [跳过] {r}")
    job.to_disk()

    tag = job.tag
    note(f"▶ 新任务　来自 {client_of(request)}", tag)
    note(f"  {len(inputs)} 个文件 · {human_size(total)}", tag)
    for f in inputs:
        note(f"    · {f['name']}　{kind_label(f['kind'])}　{human_size(f['size'])}",
             tag)
    for r in rejected:
        note(f"    [跳过] {r}", tag)

    running = running_count()
    ahead = QUEUE.qsize()          # 已经排在这个任务前面的任务数
    if ahead == 0 and running < MAX_PARALLEL:
        note("  队列: 有空位，马上开始", tag)
    else:
        note(f"  队列: {running} 个在跑，前面还有 {ahead} 个在等"
             f"（上限 {MAX_PARALLEL}）", tag)

    QUEUE.put(job_id)
    job.set_stage("排队中", P_UPLOAD)

    return JSONResponse({
        "id": job_id,
        "warnings": rejected,
        "inputs": inputs,
        **queue_state(),
    })


@app.get("/api/jobs")
async def list_jobs() -> dict:
    items = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
    return {"jobs": [j.snapshot() for j in items[:40]]}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")

    async def gen():
        # 快照与事件游标必须一起取，否则会把快照里已有的日志再重放一遍
        with job.lock:
            snap = job.snapshot()
            idx = len(job.events)

        yield ("data: "
               + json.dumps({"type": "snapshot", **snap}, ensure_ascii=False)
               + "\n\n")

        while True:
            if await request.is_disconnected():
                return
            with job.lock:
                pending = job.events[idx:]
                idx += len(pending)
                terminal = job.status in ("done", "failed")
            for ev in pending:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if terminal and not pending:
                return
            await asyncio.sleep(0.35)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.get("/api/jobs/{job_id}/download/{name}")
async def download(job_id: str, name: str, request: Request) -> FileResponse:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status != "done":
        raise HTTPException(409, "任务尚未完成")

    run_ptr = job.dir / "RUN_DIR.txt"
    if not run_ptr.is_file():
        raise HTTPException(404, "找不到本次运行目录")
    run_dir = Path(run_ptr.read_text(encoding="utf-8").strip())

    target = (run_dir / safe_name(name)).resolve()
    if not str(target).startswith(str(run_dir.resolve())) or not target.is_file():
        raise HTTPException(404, "文件不存在")

    note(f"↓ {client_of(request)} 下载 {target.name}"
         f"　（{human_size(target.stat().st_size)}）", job.tag)

    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
    )


# 静态资源挂在最后，避免覆盖 /api 路由
#
# 默认的 StaticFiles 会带上长期缓存头，改完 style.css / app.js 刷新页面看不到变化，
# 很容易误以为改动没生效。这里改成 no-cache：仍然走 ETag / Last-Modified 协商，
# 命中就是 304，代价很小，但永远拿到最新版本。
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        # 用 get_response 而不是 file_response：304 也要带上这个头，
        # 否则浏览器会一直沿用旧副本，改动看起来"没生效"。
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", NoCacheStaticFiles(directory=str(STATIC)), name="static")


def lan_ipv4() -> list[str]:
    """找本机的局域网 IPv4，用来告诉用户该把哪个地址发给同事。"""
    found: list[str] = []

    # ① 默认路由探测：最准。UDP 的 connect 不会真发包，只是让内核挑一张出口网卡。
    #    断网 / 没有默认网关时这里会抛错，所以下面还留了兜底。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass

    # ② 主机名解析：兜底。可能夹带 VMware / VPN / WSL 的虚拟网卡，所以排在后面。
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    out: list[str] = []
    for ip in found:
        # 回环和 0.0.0.0 对同事没用；169.254.x.x 是网卡没拿到 DHCP 时的自动私有地址
        if ip == "0.0.0.0" or ip.startswith(("127.", "169.254.")):
            continue
        if ip not in out:
            out.append(ip)
    return out


class QuietAccessLog(logging.Filter):
    """
    压掉「正常轮询」的访问日志。4xx / 5xx 一律放行 —— 那种才是要看的。

    为什么压：前端每 15 秒来一次 /api/jobs，SSE 还一直挂着一个长连接，
    每次刷新又取一遍 style.css / app.js。这些 200 几秒钟就能把屏幕刷满，
    真正有用的信息（谁提交了任务、跑到哪一步、谁下载了什么）全被顶走。
    压掉之后，控制台上剩下的都是我们自己用 note() 播报的任务级信息。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn 的访问日志参数固定是 (client, method, path, http_version, status)
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, _method, path, _ver, status = args
        try:
            if int(status) >= 400:
                return True
        except (TypeError, ValueError):
            return True
        if not isinstance(path, str):
            return True
        return not (path == "/" or path.startswith(("/static/", "/api/")))


def logging_config() -> dict:
    """在 uvicorn 默认日志配置上挂一个过滤器，只改访问日志这一路。"""
    import copy

    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["quiet-access"] = {"()": QuietAccessLog}
    cfg["loggers"]["uvicorn.access"]["filters"] = ["quiet-access"]
    return cfg


def main() -> int:
    import uvicorn

    # 默认绑 0.0.0.0 —— 监听全部网卡，局域网里其他人用本机 IP 就能打开。
    # 只想自己用：加 --local，或把 WEBAPP_HOST 设成 127.0.0.1。
    host = os.environ.get("WEBAPP_HOST", "")
    if not host:
        host = "127.0.0.1" if "--local" in sys.argv else "0.0.0.0"
    port = int(os.environ.get("WEBAPP_PORT", "8000"))

    print()
    print("  规格 → 模型　服务已启动")
    print(f"  本机访问:   http://127.0.0.1:{port}")

    if host in ("127.0.0.1", "localhost"):
        print("  当前只监听本机；要去掉 --local 才能被局域网访问")
    else:
        ips = lan_ipv4()
        if ips:
            for ip in ips:
                print(f"  局域网访问: http://{ip}:{port}")
        else:
            print("  局域网访问: 没探测到局域网 IP，检查一下网线 / Wi-Fi")
        print()
        print("  注意: 局域网内任何人打开地址都能看到全部历史任务里的报价，没有登录。")
        print("  同事打不开的话，多半是 Windows 防火墙拦了入站；")
        print("  用管理员身份执行一次即可（只需一次）:")
        print(f'    netsh advfirewall firewall add rule name="规格转模型 {port}" '
              f'dir=in protocol=TCP localport={port} action=allow')

    print()
    print(f"  并发上限:   {MAX_PARALLEL} 个任务同时跑，其余排队"
          f"（WEBAPP_PARALLEL 可调）")
    print()
    print("  下面只播报任务级信息：提交 / 阶段 / 完成 / 下载 / 错误。")
    print("  前端每 15 秒一次的轮询已压掉；并发时每行带任务短号。")
    print()
    uvicorn.run(app, host=host, port=port, log_level="info",
                log_config=logging_config())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
