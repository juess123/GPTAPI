#!/usr/bin/env python3
"""Manage the two project-local, locked dependency layers."""
from __future__ import annotations
import argparse
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from packaging.requirements import InvalidRequirement, Requirement

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
BLENDER_PACKAGES = ROOT / ".blender_packages"
LOCK_DIR = ROOT / ".dependency-install.lock"
PYTHON_REQUIREMENTS = ROOT / "requirements.txt"
BLENDER_REQUIREMENTS = ROOT / "requirements-blender.txt"
BLENDER_CANDIDATES = [Path(fr"C:\Program Files\Blender Foundation\Blender {v}\blender.exe")
                      for v in ("5.2", "5.1", "5.0", "4.5", "4.2")]

def find_blender() -> Path:
    found = shutil.which("blender")
    if found:
        return Path(found)
    for candidate in BLENDER_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到 Blender；请先安装支持的 Blender 版本")

def locked_packages(path: Path) -> list[str]:
    result = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            if "==" not in line:
                raise RuntimeError(f"依赖没有锁定精确版本：{line}")
            result.append(line)
    return result

def package_names(path: Path) -> list[str]:
    return [re.split(r"\[|==", item, maxsplit=1)[0] for item in locked_packages(path)]

def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

def required_versions(path: Path) -> dict[str, tuple[str, str]]:
    result = {}
    for item in locked_packages(path):
        left, version = item.rsplit("==", 1)
        name = left.split("[", 1)[0]
        result[canonical(name)] = (name, version)
    return result

def missing_requirements(path: Path, search_path: Path | None = None) -> list[str]:
    wanted = required_versions(path)
    if search_path is None:
        installed = {canonical(d.metadata["Name"]): d.version
                     for d in importlib.metadata.distributions() if d.metadata.get("Name")}
    else:
        installed = {canonical(d.metadata["Name"]): d.version
                     for d in importlib.metadata.distributions(path=[str(search_path)])
                     if d.metadata.get("Name")}
    return [f"{name}=={version}" for key, (name, version) in wanted.items()
            if installed.get(key) != version]

def lock_owner_is_alive() -> bool:
    try:
        text = (LOCK_DIR / "owner.txt").read_text(encoding="utf-8")
        pid = int(dict(line.split("=", 1) for line in text.splitlines()).get("pid", "0"))
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        return False

@contextmanager
def install_lock(timeout: int = 3600):
    start = time.time()
    while True:
        try:
            LOCK_DIR.mkdir()
            (LOCK_DIR / "owner.txt").write_text(f"pid={os.getpid()}\ntime={time.time()}\n", encoding="utf-8")
            break
        except FileExistsError:
            try:
                if not lock_owner_is_alive():
                    shutil.rmtree(LOCK_DIR)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - start > timeout:
                raise RuntimeError("等待依赖安装锁超时")
            time.sleep(1)
    try:
        yield
    finally:
        shutil.rmtree(LOCK_DIR, ignore_errors=True)

def run_checked(cmd: list[str]) -> None:
    print("$ " + " ".join(f'\"{part}\"' if " " in part else part for part in cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)

def blender_python(blender: Path) -> Path:
    marker = "GPTAPI3_BLENDER_PYTHON="
    proc = subprocess.run(
        [str(blender), "--background", "--factory-startup", "--python-expr",
         f"import sys; print('{marker}' + sys.executable)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith(marker):
            path = Path(line[len(marker):].strip())
            if path.is_file():
                return path
    raise RuntimeError("无法确定 Blender 内置 Python 的路径")

def ensure_python_packages() -> None:
    expected = VENV / "Scripts" / "python.exe"
    if Path(sys.executable).resolve() != expected.resolve():
        raise RuntimeError(f"请使用项目环境运行依赖检查：{expected}")
    missing = missing_requirements(PYTHON_REQUIREMENTS)
    if missing:
        print("普通 Python 缺失或版本不符：" + ", ".join(missing))
        run_checked([str(expected), "-m", "pip", "install", "--disable-pip-version-check",
                     "--requirement", str(PYTHON_REQUIREMENTS)])
    else:
        print("普通 Python 依赖已满足")

def ensure_blender_packages() -> None:
    BLENDER_PACKAGES.mkdir(parents=True, exist_ok=True)
    missing = missing_requirements(BLENDER_REQUIREMENTS, BLENDER_PACKAGES)
    if missing:
        print("Blender 环境缺失或版本不符：" + ", ".join(missing))
        python = blender_python(find_blender())
        run_checked([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade",
                     "--target", str(BLENDER_PACKAGES), "--requirement", str(BLENDER_REQUIREMENTS)])
    else:
        print("Blender 第三方依赖已满足")

def ensure_all() -> None:
    with install_lock():
        ensure_python_packages()
        ensure_blender_packages()

def validate_task_requirements(items: object, runtime: str) -> list[str]:
    if not isinstance(items, list):
        raise RuntimeError(f"dependencies.{runtime} 必须是数组")
    result = []
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(f"dependencies.{runtime} 包含无效项目")
        text = raw.strip()
        try:
            requirement = Requirement(text)
        except InvalidRequirement as error:
            raise RuntimeError(f"无效的 PyPI requirement：{text}（{error}）") from error
        if requirement.url:
            raise RuntimeError(f"任务依赖不接受 URL/Git 来源：{text}")
        result.append(text)
    return list(dict.fromkeys(result))

def installed_manifest(target: Path) -> list[dict[str, str]]:
    packages = []
    for distribution in importlib.metadata.distributions(path=[str(target)]):
        name = distribution.metadata.get("Name")
        if name:
            packages.append({"name": name, "version": distribution.version})
    return sorted(packages, key=lambda item: item["name"].lower())

def installed_versions(search_path: Path | None = None) -> dict[str, tuple[str, str]]:
    """返回规范化包名 -> (显示名, 版本)。None 表示当前 .venv。"""
    distributions = (importlib.metadata.distributions()
                     if search_path is None
                     else importlib.metadata.distributions(path=[str(search_path)]))
    result = {}
    for distribution in distributions:
        name = distribution.metadata.get("Name")
        if name:
            result[canonical(name)] = (name, distribution.version)
    return result

def split_reusable(requirements: list[str], base: dict[str, tuple[str, str]],
                   source: str) -> tuple[list[str], list[dict[str, str]]]:
    """把基础环境已满足的声明剔除，只留下确实需要安装到任务目录的依赖。"""
    install = []
    reused = []
    for text in requirements:
        requirement = Requirement(text)
        present = base.get(canonical(requirement.name))
        # extras 可能引入基础包之外的新依赖，保守地仍交给 pip 处理。
        satisfied = (present is not None and not requirement.extras
                     and (not requirement.specifier
                          or requirement.specifier.contains(present[1], prereleases=True)))
        if satisfied:
            reused.append({
                "requirement": text,
                "name": present[0],
                "version": present[1],
                "source": source,
            })
        else:
            install.append(text)
    return install, reused

def install_task_requirements(python: Path, requirements: list[str], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if not requirements:
        return
    run_checked([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                 "--upgrade", "--ignore-installed", "--target", str(target), *requirements])

def ensure_task(dependencies_path: Path, task_packages: Path) -> None:
    import json
    try:
        data = json.loads(dependencies_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取任务依赖声明：{error}") from error
    python_requirements = validate_task_requirements(data.get("python", []), "python")
    blender_requirements = validate_task_requirements(data.get("blender", []), "blender")
    python_target = task_packages / "python"
    blender_target = task_packages / "blender"
    task_packages.mkdir(parents=True, exist_ok=True)
    python_install, python_reused = split_reusable(
        python_requirements, installed_versions(), ".venv")
    blender_install, blender_reused = split_reusable(
        blender_requirements, installed_versions(BLENDER_PACKAGES), ".blender_packages")
    for item in python_reused + blender_reused:
        print(f"复用 {item['source']}：{item['name']}=={item['version']}")
    with install_lock():
        install_task_requirements(VENV / "Scripts" / "python.exe", python_install, python_target)
        if blender_install:
            install_task_requirements(blender_python(find_blender()), blender_install, blender_target)
        else:
            blender_target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "requested": {"python": python_requirements, "blender": blender_requirements},
        "installed": {
            "python": installed_manifest(python_target),
            "blender": installed_manifest(blender_target),
        },
        "reused": {
            "python": python_reused,
            "blender": blender_reused,
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (task_packages / "environment.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"任务级依赖已就绪：{task_packages}")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--ensure-task", action="store_true")
    parser.add_argument("--dependencies")
    parser.add_argument("--task-packages")
    parser.add_argument("--show-allowlist", action="store_true")
    args = parser.parse_args()
    if args.show_allowlist:
        print("普通 Python：" + ", ".join(package_names(PYTHON_REQUIREMENTS)))
        print("Blender：bpy, " + ", ".join(package_names(BLENDER_REQUIREMENTS)))
    elif args.ensure_task:
        if not args.dependencies or not args.task_packages:
            parser.error("--ensure-task 需要 --dependencies 和 --task-packages")
        ensure_task(Path(args.dependencies).resolve(), Path(args.task_packages).resolve())
    elif args.ensure:
        ensure_all()
        print("依赖环境已就绪")
    else:
        parser.print_help()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
