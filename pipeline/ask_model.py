#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A' 流水线 · 第 1 步：让模型生成构建脚本。

模型在这里扮演"生成者"——它只负责写出生成文件的代码；
真正的二进制文件由 build.py 在本地执行产生（等价于 ChatGPT 的沙箱，只是搬到了本机）。

投料口：input/ 目录下的所有文件都会作为输入材料送给模型。
  · .md/.txt/.json/.csv…  → 原文并入提示词
  · .xlsx/.xlsm           → 本机用 openpyxl 转成 Markdown 表格后并入
  · .png/.jpg/.jpeg/.webp → 作为多模态图片附件真发给模型（支持看图）
  其余格式会跳过并打印说明，不会静默丢失。

任务越复杂，模型思考越久，可通过 --timeout 放宽单次等待上限。

用法:
    python pipeline/ask_model.py
    python pipeline/ask_model.py --timeout 40m          # 单次最长等 40 分钟
    python pipeline/ask_model.py --dry-run              # 只看投料与提示词
    python pipeline/ask_model.py --timeout 90m --attempts 1
"""

import argparse
import base64
import io
import json
import mimetypes
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # E:\GPTAPI3
HERE = Path(__file__).resolve().parent             # E:\GPTAPI3\pipeline
GEN = HERE / "generated"                           # 模型产出的脚本存放处

# 投料口：这个目录下的所有文件都会作为输入材料送给模型。
INPUT_DIR = ROOT / "input"

# build.py 依赖这两个固定文件名，不可改。
WANTED = ["make_xlsx.py", "make_blend.py"]
XLSX_NAME = "final_quote.xlsx"
BLEND_NAME = "final_model.blend"

# 按扩展名分派处理方式
TEXT_EXTS = {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".py", ".log", ".ini"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",".pdf"}
XLSX_EXTS = {".xlsx", ".xlsm"}

# 单张图片上限；网关对超大 base64 会拒绝，也需要控制 token
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# 默认值；可被 .env 或命令行覆盖
DEFAULT_TIMEOUT = 180          # 单次请求超时（秒）
DEFAULT_MAX_TOKENS = 16000     # 复杂脚本代码较长
DEFAULT_ATTEMPTS = 4

INSTRUCTION = """\
根据任务材料生成两个可直接执行的 Python 脚本和一份依赖声明。只输出指定格式，不要解释。

════════════════ 任务材料 ════════════════
{task}
══════════════════════════════════════════

图片也是任务材料。业务内容、造型、尺寸、材质和报价规则以材料为准；未说明的信息可合理补全，
但不得把推测的尺寸或价格伪装成确定事实。Excel 的结构与内容、Blender 的场景内容不作额外限制。

==================== 运行环境与交付契约 ====================
当前项目预装的第三方库如下，可以直接使用：
{available_packages}

当前为快速直出模式，但不限制创作能力。优先使用上述已安装库；需要其他 PyPI 库时可在
DEPENDENCIES 中声明，由执行系统安装到本任务的独立环境。允许按任务需要联网获取字体、贴图
或其他公开资源，也允许脚本调用 pip；应记录来源并在失败时提供不依赖下载的降级方案。

1. make_xlsx.py 执行后必须生成 {xlsx_name}。
2. make_blend.py 执行后必须生成 {blend_name}，并支持 Blender 无界面运行。
3. 两个脚本都从环境变量 OUT_DIR 获取输出目录，未设置时使用 "output"。
4. 只能把交付文件写入 OUT_DIR；完成后打印最终文件的绝对路径。
5. 不得依赖人工交互；新增 Python 能力优先通过 DEPENDENCIES 声明，也允许脚本按需调用 pip。
6. 必须生成两个主交付物；允许额外生成贴图、渲染预览、组件清单和其他有助于交付质量的文件。
7. Blender API 的可变枚举、节点输入和可选属性必须通过运行时探测、存在性检查或异常处理兼容。
8. 保存 Blender 文件前设置 bpy.context.preferences.filepaths.save_version = 0，
   并使用 bpy.ops.wm.save_as_mainfile(filepath=blend_path)。
9. 可以下载字体或贴图、创建摄影棚、渲染预览并生成附属文件；同时合理控制代码长度，确保响应完整。
10. make_blend.py 必须严格保存为 {blend_name}，make_xlsx.py 必须严格保存为 {xlsx_name}。
11. 执行系统固定先运行 make_blend.py、再运行 make_xlsx.py。make_blend.py 可把模型统计、物料清单等
    共享数据写入 OUT_DIR，make_xlsx.py 可读取这些数据生成与模型一致的报价。
12. 两个脚本必须是单向独立构建步骤：严禁互相启动、导入或执行；严禁自行调用 blender.exe、
    run_blender_script.py 或另一个交付脚本。Blender 只由执行系统统一启动并注入任务依赖。
13. make_xlsx.py 不得因为可选共享数据不存在而自行启动 Blender；确需共享数据时，应给出清楚错误，
    或在不伪造确定数据的前提下合理降级。
14. Blender 兼容性还必须覆盖以下情况：
    · mathutils.geometry.tessellate_polygon 的三角形元素可能是顶点索引，也可能是 Vector，使用前探测类型；
    · 判断对象是否属于 collection.objects 时用对象名称或 collection.objects.get(name)，不要用对象实例做 in 判断；
    · 所有枚举、节点插槽、操作符和可选属性继续使用运行时探测或异常兜底，不按版本号猜测。
15. save_as_mainfile 成功后立即打印绝对路径并结束。严禁重新打开刚保存的 .blend，严禁检查固定文件头，
    严禁在保存后执行额外断言或自检；交付验证由执行系统负责。
16. 为便于执行系统检查真实空间关系，make_blend.py 必须给场景写入 validation_spec_json（JSON字符串），
    至少包含 overall、expected_counts、ground_z_mm、spatial_rules。只有用户明确给出的尺寸才能标成
    confirmed；图片推测必须标成 assumption，不能作为伪造的确定尺寸。
17. 所有 MESH/CURVE/FONT 对象必须明确分类。工程对象设置 component_id、instance_id、component_type、
    quote_relevant=True、exclude_from_quote=False；同一组合构件可共用 component_id，但 instance_id 必须唯一。
    相机、灯光、摄影棚、地面和控制器设置 quote_relevant=False、exclude_from_quote=True。
18. spatial_rules 可使用：inside_host、near_host、floor_contact、left_of、right_of、above、below、no_overlap。
    规则中的对象用 component_id 引用，并提供必要的 axis、axes、tolerance_mm 或 max_gap_mm。
    只声明能由任务材料确认的关系；不确定关系写入场景 assumptions_json，不得编造为硬约束。

==================== 输出格式（必须严格照此，不得增减）====================
<<<DEPENDENCIES>>>
{
  "python": ["PyPI包名及版本约束"],
  "blender": ["PyPI包名及版本约束"]
}
<<<ENDDEPENDENCIES>>>

<<<FILE:make_xlsx.py>>>
```python
完整代码
```
<<<ENDFILE>>>

<<<FILE:make_blend.py>>>
```python
完整代码
```
<<<ENDFILE>>>
"""


def available_packages() -> str:
    """把两个锁定依赖清单作为模型可使用的能力清单。"""
    def names(path: Path) -> str:
        items = []
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                items.append(line)
        return ", ".join(items)
    return (f"普通 Python / Excel：{names(ROOT / 'requirements.txt')}\n"
            f"Blender：bpy（Blender 自带）, {names(ROOT / 'requirements-blender.txt')}")


def load_env(path: Path) -> dict:
    """极简 .env 解析：忽略注释与空行，不做去引号处理。"""
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip()
    return env


# ───────────────────────── 投料口：input/ 目录 ─────────────────────────

def xlsx_to_text(path: Path) -> str:
    """把工作簿的全部工作表转成 Markdown 表格，供模型阅读。

    模型看不到二进制 xlsx，所以必须在本机先降维成文本。
    所有值都取计算结果（data_only=True），避免把公式当数据读。
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        return f"（无法读取 {path.name}：本机未安装 openpyxl，请先 pip install openpyxl）"

    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as e:
        return f"（无法读取 {path.name}：{type(e).__name__}: {e}）"

    def fmt(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:g}"
        return str(v).replace("|", "\\|").replace("\n", " ").strip()

    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                vals = [fmt(v) for v in row]
                while vals and vals[-1] == "":
                    vals.pop()
                rows.append(vals)
            while rows and not any(rows[-1]):       # 去掉尾部空行
                rows.pop()
            if not rows:
                continue

            width = max(len(r) for r in rows)
            header = rows[0] + [""] * (width - len(rows[0]))
            parts.append(f"### 工作表：{ws.title}（{len(rows) - 1} 行数据 × {width} 列）")
            parts.append("")
            parts.append("| " + " | ".join(header) + " |")
            parts.append("|" + "---|" * len(header))
            for r in rows[1:]:
                parts.append("| " + " | ".join(r + [""] * (width - len(r))) + " |")
            parts.append("")
    finally:
        wb.close()

    return "\n".join(parts).rstrip() or "（空工作簿）"


def image_data_url(path: Path) -> tuple[str | None, str]:
    """生成模型可接收的图片；大图自动转成小于上限的 JPEG 内存副本。"""
    data = path.read_bytes()
    if not data:
        return None, "空文件"
    mime = (mimetypes.guess_type(path.name)[0] or "").lower()
    if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"):
        mime = "image/png"
    if len(data) <= MAX_IMAGE_BYTES:
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", "原图"

    try:
        from PIL import Image, ImageOps

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(io.BytesIO(data)) as opened:
            opened.seek(0)  # GIF 等多帧文件使用第一帧作为模型参考图
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background

        # 先保留分辨率降低 JPEG 质量；仍超限时再逐步缩小尺寸。
        quality = 90
        while True:
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
            optimized = output.getvalue()
            if len(optimized) <= MAX_IMAGE_BYTES:
                width, height = image.size
                url = "data:image/jpeg;base64," + base64.b64encode(optimized).decode("ascii")
                return url, f"自动优化为 JPEG {width}×{height}，{len(optimized)} 字节"
            if quality > 55:
                quality -= 10
                continue
            width, height = image.size
            if width <= 640 and height <= 640:
                return None, "自动优化后仍超过模型图片上限"
            scale = 0.82
            target = (max(1, int(width * scale)), max(1, int(height * scale)))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image = image.resize(target, resampling)
            quality = 85
    except Exception as error:  # noqa: BLE001
        return None, f"图片优化失败：{type(error).__name__}: {error}"


def collect_inputs(directory: Path) -> tuple[str, list[dict], list[str]]:
    """扫描投料口，返回 (文本材料, 图片部件列表, 逐文件说明)。

    分派规则：
      · .xlsx/.xlsm  → 本机转成 Markdown 表格（模型读不了二进制）
      · .png/.jpg…   → 作为多模态图片附件真发给模型
      · .md/.txt…    → 原文并入提示词
      其余格式跳过并记录，不静默丢失。
    """
    if not directory.is_dir():
        return "", [], [f"投料口不存在：{directory}"]

    files = sorted(p for p in directory.rglob("*") if p.is_file())
    if not files:
        return "", [], [f"投料口为空：{directory}"]

    blocks: list[str] = []
    images: list[dict] = []
    notes: list[str] = []

    for p in files:
        rel = p.relative_to(directory).as_posix()
        ext = p.suffix.lower()
        size = p.stat().st_size

        if ext in XLSX_EXTS:
            blocks.append(f"────────── 材料：{rel} ──────────\n{xlsx_to_text(p)}")
            notes.append(f"  [表格→文本] {rel}  ({size} 字节)")

        elif ext in IMAGE_EXTS:
            url, image_note = image_data_url(p)
            if url is None:
                notes.append(f"  [跳过·图片不可用] {rel}  ({size} 字节；{image_note})")
                continue
            images.append({"type": "image_url", "image_url": {"url": url}})
            notes.append(f"  [图片附件]   {rel}  ({size} 字节；{image_note})")

        elif ext in TEXT_EXTS:
            try:
                text = p.read_text(encoding="utf-8-sig").strip()
            except UnicodeDecodeError:
                text = p.read_text(encoding="gbk", errors="replace").strip()
            blocks.append(f"────────── 材料：{rel} ──────────\n{text}")
            notes.append(f"  [文本材料]   {rel}  ({size} 字节)")

        else:
            notes.append(f"  [跳过·格式]  {rel}  ({size} 字节, 扩展名 {ext or '无'})")

    return "\n\n".join(blocks), images, notes


def parse_duration(text: str) -> int:
    """
    把时长写法统一解析成秒。

    支持：2400 / 2400s / 40m / 40min / 1.5h / 40分
    """
    s = str(text).strip().lower()
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(s|sec|secs|m|min|mins|分钟|分|h|hr|hrs|小时|时)?", s)
    if not m:
        raise ValueError(f"无法解析的时长: {text!r}（示例：40m / 2400 / 1.5h）")
    value = float(m.group(1))
    unit = m.group(2) or "s"
    if unit in ("s", "sec", "secs"):
        seconds = value
    elif unit in ("m", "min", "mins", "分钟", "分"):
        seconds = value * 60
    else:  # h / hr / hrs / 小时 / 时
        seconds = value * 3600
    if seconds < 1:
        raise ValueError(f"时长过短: {text!r}")
    return int(seconds)


def fmt_duration(seconds: float) -> str:
    """把秒数格式化成便于阅读的形式。"""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class Heartbeat:
    """
    等待响应期间周期性输出已等待时长，避免长请求看起来像卡死。

    间隔会随超时上限自适应：180s 时每 5 秒报一次，
    2400s（40 分钟）时约每 60 秒报一次，否则会刷屏数百行。
    """

    def __init__(self, label: str, timeout: int = DEFAULT_TIMEOUT):
        self.label = label
        self.interval = max(5.0, min(60.0, timeout / 40.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()
        return self

    def _tick(self):
        while not self._stop.wait(self.interval):
            print(f"  {self.label} 已等待 {fmt_duration(time.time() - self._t0)}", flush=True)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        return False


def post_json(url: str, payload: dict, api_key: str,
              timeout: int = DEFAULT_TIMEOUT, max_attempts: int = DEFAULT_ATTEMPTS) -> dict:
    """
    发送请求。

    两个已知约束：
      1. 该网关对 JSON 格式敏感——带缩进的多行 JSON 会偶发 400（且错误体为空），
         因此固定使用 separators 压缩，并把「空响应体的 400」视为可重试。
      2. 网络偶发抖动（会看到 proxy / SSL 握手超时），所以对超时与连接错误重试。

    单次超时默认 180s：太短会误杀正常的长推理，太长则中断前干等太久。
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    last_err = None

    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", f"Bearer {api_key}")
        label = f"第 {attempt}/{max_attempts} 次尝试"
        print(f"  {label}，单次超时 {fmt_duration(timeout)}", flush=True)
        attempt_t0 = time.time()

        try:
            with Heartbeat(label, timeout):
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()

            took = time.time() - attempt_t0
            print(f"  ↳ 服务端耗时 {fmt_duration(took)}", flush=True)
            return json.loads(raw.decode("utf-8"))

        except KeyboardInterrupt:
            # Ctrl+C 是用户主动取消，不该被当成网络错误重试。
            raise

        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            last_err = f"HTTP {e.code}: {detail.strip() or '(响应体为空)'}"
            retryable = e.code >= 500 or e.code == 429 or not detail.strip()
            if retryable and attempt < max_attempts:
                print(f"  ↻ 可重试错误：{last_err[:180]}")
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(last_err) from None

        except (TimeoutError, OSError, json.JSONDecodeError) as e:
            # URLError / socket.timeout / SSL 错误都是 OSError 子类
            last_err = f"{type(e).__name__}: {e}"
            if attempt < max_attempts:
                print(f"  ↻ 网络层错误：{last_err[:180]}")
                time.sleep(2 * attempt)
                continue

    raise RuntimeError(f"请求失败（已尝试 {max_attempts} 次）: {last_err}")


def extract_files(text: str) -> dict:
    """从模型输出中按标记提取各脚本源码。"""
    blocks = re.findall(r"<<<FILE:(.*?)>>>(.*?)<<<ENDFILE>>>", text, re.S)
    out = {}
    for name, body in blocks:
        name = name.strip()
        code = body.strip()
        code = re.sub(r"^```[A-Za-z0-9_+-]*\s*\n", "", code)   # 去掉起始围栏
        code = re.sub(r"\n```\s*$", "", code)                  # 去掉结束围栏
        out[name] = code
    return out


def extract_dependencies(text: str) -> dict[str, list[str]]:
    """提取任务级 PyPI 依赖；缺省时保持向后兼容。"""
    match = re.search(r"<<<DEPENDENCIES>>>(.*?)<<<ENDDEPENDENCIES>>>", text, re.S)
    if not match:
        return {"python": [], "blender": []}
    body = match.group(1).strip()
    body = re.sub(r"^```(?:json)?\s*\n", "", body, flags=re.I)
    body = re.sub(r"\n```\s*$", "", body)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError(f"DEPENDENCIES 不是有效 JSON：{error}") from error
    if not isinstance(value, dict):
        raise ValueError("DEPENDENCIES 必须是 JSON 对象")
    result: dict[str, list[str]] = {}
    for runtime in ("python", "blender"):
        items = value.get(runtime, [])
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError(f"DEPENDENCIES.{runtime} 必须是字符串数组")
        result[runtime] = list(dict.fromkeys(item.strip() for item in items))
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ask_model.py",
        description="让模型生成 make_xlsx.py 与 make_blend.py 两个构建脚本。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "时长写法示例:\n"
            "  --timeout 40m        40 分钟（推荐用于大任务）\n"
            "  --timeout 2400       2400 秒（等价于 40m）\n"
            "  --timeout 1.5h       1.5 小时\n"
            "\n"
            "优先级: 命令行参数 > .env 中的 CNXMAI_TIMEOUT / CNXMAI_MAX_TOKENS > 内置默认值"
        ),
    )
    p.add_argument("-t", "--timeout", default=None,
                   help=f"单次请求超时，支持 90 / 90s / 40m / 1.5h（默认 {DEFAULT_TIMEOUT}s）")
    p.add_argument("-a", "--attempts", type=int, default=None,
                   help=f"最大尝试次数（默认 {DEFAULT_ATTEMPTS}）")
    p.add_argument("-m", "--max-tokens", type=int, default=None,
                   help=f"单次回复最大 token 数（默认 {DEFAULT_MAX_TOKENS}）")
    p.add_argument("--input-dir", default=None,
                   help="投料口目录（默认 <仓库>/input）。Web 后端为每个任务指定独立目录")
    p.add_argument("--gen-dir", default=None,
                   help="模型产出脚本的存放目录（默认 <仓库>/pipeline/generated）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将发送的提示词与参数，不实际请求")
    p.add_argument("--installed-only", action="store_true",
                   help="快速模式兼容标记；仅允许使用当前已安装库")
    return p


def resolve_settings(args: argparse.Namespace, env: dict) -> tuple[int, int, int]:
    """按「命令行 > .env > 默认值」的顺序确定 超时 / 尝试次数 / max_tokens。"""
    # 超时
    if args.timeout is not None:
        timeout = parse_duration(args.timeout)
    elif env.get("CNXMAI_TIMEOUT"):
        timeout = parse_duration(env["CNXMAI_TIMEOUT"])
    else:
        timeout = DEFAULT_TIMEOUT

    # 尝试次数
    attempts = args.attempts if args.attempts is not None else (
        int(env["CNXMAI_ATTEMPTS"]) if env.get("CNXMAI_ATTEMPTS") else DEFAULT_ATTEMPTS
    )
    # 大任务不该叠加重试：单次 40 分钟再试 4 次就是 160 分钟，通常没必要
    if args.attempts is None and not env.get("CNXMAI_ATTEMPTS") and timeout >= 600:
        attempts = 2
    attempts = max(1, attempts)

    # max_tokens
    if args.max_tokens is not None:
        max_tokens = args.max_tokens
    elif env.get("CNXMAI_MAX_TOKENS"):
        max_tokens = int(env["CNXMAI_MAX_TOKENS"])
    else:
        max_tokens = DEFAULT_MAX_TOKENS

    return timeout, attempts, max_tokens


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Web 后端可为每个任务指定独立的投料口与产出目录
    global INPUT_DIR, GEN
    if args.input_dir:
        INPUT_DIR = Path(args.input_dir).expanduser().resolve()
    if args.gen_dir:
        GEN = Path(args.gen_dir).expanduser().resolve()

    env = load_env(ROOT / ".env")
    url = env.get("CNXMAI_CHAT_URL")
    key = env.get("CNXMAI_API_KEY")
    model = env.get("CNXMAI_MODEL")
    if not (url and key and model):
        print("错误：.env 缺少 CNXMAI_CHAT_URL / CNXMAI_API_KEY / CNXMAI_MODEL")
        return 1

    # 防呆：该网关的 /v1/responses 实测返回 404，必须走 chat/completions。
    if "/responses" in url:
        print(f"错误：CNXMAI_CHAT_URL 指向了 Responses API（{url}），")
        print("      但该网关不支持该端点（实测 404）。请改为 .../v1/chat/completions")
        return 1

    try:
        timeout, attempts, max_tokens = resolve_settings(args, env)
    except (ValueError, TypeError) as e:
        print(f"错误：{e}")
        return 1

    materials, images, notes = collect_inputs(INPUT_DIR)
    if not materials and not images:
        supported = ", ".join(sorted(TEXT_EXTS | IMAGE_EXTS | XLSX_EXTS))
        print(f"错误：投料口 {INPUT_DIR} 里没有可用材料。")
        print(f"      支持的扩展名：{supported}")
        return 1

    prompt = INSTRUCTION.replace("{task}", materials) \
                        .replace("{xlsx_name}", XLSX_NAME) \
                        .replace("{blend_name}", BLEND_NAME) \
                        .replace("{available_packages}", available_packages())

    # 多模态 content：文字在前，图片在后并带一句引导
    content: list[dict] = [{"type": "text", "text": prompt}]
    if images:
        content.append({
            "type": "text",
            "text": "以下是投料口提供的参考图片，请据此理解造型、比例、材质与颜色：",
        })
        content.extend(images)

    print(f"模型    : {model}")
    print(f"端点    : {url}")
    print(f"投料口  : {INPUT_DIR}")
    for n in notes:
        print(n)
    print(f"材料长度: {len(materials)} 字符    图片: {len(images)} 张")
    print(f"超时    : {fmt_duration(timeout)} / 次    尝试次数: {attempts}    max_tokens: {max_tokens}")
    print(f"最坏耗时: {fmt_duration(timeout * attempts)}")

    if args.dry_run:
        print("\n[dry-run] 以下是将要发送的提示词，未实际请求：\n")
        print("-" * 60)
        print(prompt)
        print("-" * 60)
        if images:
            print(f"\n[dry-run] 另附 {len(images)} 张图片（base64 已省略）：")
            for i, im in enumerate(images, 1):
                print(f"  图 {i}: {im['image_url']['url'][:48]}…"
                      f"（{len(im['image_url']['url'])} 字符）")
        return 0

    t0 = time.time()
    try:
        result = post_json(
            url,
            {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": max_tokens,
            },
            key,
            timeout=timeout,
            max_attempts=attempts,
        )
    except KeyboardInterrupt:
        print("\n\n已手动取消（Ctrl+C）。未产生任何文件变更。")
        print("提示：生成完整脚本的请求通常需要 20~60 秒，请耐心等待心跳输出。")
        return 130
    except RuntimeError as e:
        print(f"\n错误：{e}")
        return 1

    elapsed = time.time() - t0

    GEN.mkdir(parents=True, exist_ok=True)
    (GEN / "raw_model_response.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        print("错误：响应结构异常，详见 generated/raw_model_response.json")
        return 1

    usage = result.get("usage", {})
    print(f"完成    : {fmt_duration(elapsed)}   tokens={usage.get('total_tokens')}")

    files = extract_files(content)
    missing = [n for n in WANTED if n not in files]
    if missing:
        print(f"错误：模型未按要求输出这些脚本 -> {missing}")
        (GEN / "model_text.txt").write_text(content, encoding="utf-8")
        print("模型原始文本已存至 generated/model_text.txt")
        return 1

    for name in WANTED:
        p = GEN / name
        p.write_text(files[name], encoding="utf-8")
        print(f"已生成 : {p}  ({len(files[name].splitlines())} 行)")

    try:
        dependencies = extract_dependencies(content)
    except ValueError as error:
        print(f"错误：{error}")
        (GEN / "model_text.txt").write_text(content, encoding="utf-8")
        return 1
    dependency_path = GEN / "dependencies.json"
    dependency_path.write_text(
        json.dumps(dependencies, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"任务依赖: {dependency_path}")
    print(f"  普通 Python: {dependencies['python'] or ['无新增依赖']}")
    print(f"  Blender: {dependencies['blender'] or ['无新增依赖']}")

    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
