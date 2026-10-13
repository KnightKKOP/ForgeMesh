"""全新虚拟环境中的包导入检查（CI package-imports job 调用）。

对 ``packages/`` 下的每个包：

1. 新建一个虚拟环境，只安装这个包（启用它自己的全部 extras）和它声明的依赖：
   ``forgemesh-*`` 依赖用本仓库 ``packages/`` 中的源码构建，其余依赖由 pip 从包索引安装；
2. 以隔离模式（``python -I``）导入包的根模块，用 :func:`pkgutil.walk_packages`
   遍历并逐个导入全部子模块（``__main__`` 除外）；
3. 任一模块导入失败（例如引用了没有声明的依赖），或者导入到的模块与源码树
   不一致，这个包即判为失败。

逐包输出 OK/FAIL 明细与汇总，任一包失败即以退出码 1 结束。命令行上可以给出
包目录名（例如 ``control-plane``），只检查这几个包。需要访问包索引。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import venv
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
NAMESPACE = "forgemesh"

_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")

# 在被检查的虚拟环境中执行。argv[1] 为结果文件，其余为根模块名；结果写文件而不是
# stdout，避免模块导入时的输出混进结果。
_PROBE = """
import importlib, json, pkgutil, sys

imported, failures = [], {}

def load(name):
    try:
        importlib.import_module(name)
    except Exception as exc:
        failures[name] = f"{type(exc).__name__}: {exc}"
        return False
    imported.append(name)
    return True

for root in sys.argv[2:]:
    if not load(root):
        continue
    walk = pkgutil.walk_packages(sys.modules[root].__path__, root + ".", lambda name: None)
    for info in walk:
        if info.name.rpartition(".")[2] != "__main__":
            load(info.name)

with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"imported": imported, "failures": failures}, fh)
"""


@dataclass
class LocalPackage:
    """``packages/`` 下的一个包。"""

    name: str
    path: Path
    dependencies: list[str]
    extras: dict[str, list[str]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)


@dataclass
class ProbeResult:
    imported: set[str]
    failures: dict[str, str]


def normalize(name: str) -> str:
    """PEP 503 规范化的分发名。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(requirement: str) -> tuple[str, set[str]]:
    """从 PEP 508 依赖串中取出规范化的分发名与 extras（版本约束与环境标记不关心）。"""
    match = _REQUIREMENT.match(requirement)
    if match is None:
        raise ValueError(f"无法解析依赖 {requirement!r}")
    extras = {e.strip() for e in (match.group(2) or "").split(",") if e.strip()}
    return normalize(match.group(1)), extras


def load_packages(packages_dir: Path = PACKAGES_DIR) -> dict[str, LocalPackage]:
    """读取 ``packages/*/pyproject.toml``，按规范化的分发名索引。"""
    packages: dict[str, LocalPackage] = {}
    for pyproject in sorted(packages_dir.glob("*/pyproject.toml")):
        with pyproject.open("rb") as fh:
            project = tomllib.load(fh)["project"]
        namespace_dir = pyproject.parent / "src" / NAMESPACE
        roots = []
        if namespace_dir.is_dir():
            roots = [
                f"{NAMESPACE}.{d.name}"
                for d in sorted(namespace_dir.iterdir())
                if d.is_dir() and d.name != "__pycache__"
            ]
        name = normalize(project["name"])
        packages[name] = LocalPackage(
            name=name,
            path=pyproject.parent,
            dependencies=list(project.get("dependencies", [])),
            extras={k: list(v) for k, v in project.get("optional-dependencies", {}).items()},
            roots=roots,
        )
    return packages


def resolve_local(target: str, packages: dict[str, LocalPackage]) -> dict[str, set[str]]:
    """需要从本仓库安装的包及各自启用的 extras；目标包启用它的全部 extras。

    ``forgemesh`` 开头却不在 ``packages/`` 中的依赖直接报错，不能让 pip 去包索引上
    找同名的包。
    """
    selected: dict[str, set[str]] = {}
    pending = [(target, set(packages[target].extras))]
    while pending:
        name, extras = pending.pop()
        known = selected.get(name)
        if known is not None and extras <= known:
            continue
        selected[name] = extras | (known or set())
        package = packages[name]
        requirements = list(package.dependencies)
        for extra in sorted(extras):
            if extra not in package.extras:
                raise ValueError(f"{name} 没有 extra {extra!r}")
            requirements.extend(package.extras[extra])
        for requirement in requirements:
            dependency, dependency_extras = parse_requirement(requirement)
            if dependency in packages:
                pending.append((dependency, dependency_extras))
            elif dependency.startswith(NAMESPACE):
                raise ValueError(f"{name} 依赖 {dependency}，但 packages/ 中没有这个包")
    return selected


def source_modules(package: LocalPackage) -> set[str]:
    """源码树中的全部模块名（含各根模块，不含 ``__main__``）。"""
    src = package.path / "src"
    modules = set(package.roots)
    for root in package.roots:
        for path in (src / root.replace(".", "/")).rglob("*.py"):
            parts = path.relative_to(src).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts[-1] != "__main__":
                modules.add(".".join(parts))
    return modules


def probe(python: Path, roots: list[str], workdir: Path) -> ProbeResult:
    """用给定的解释器导入根模块及其全部子模块。"""
    result_file = workdir / "probe-result.json"
    proc = subprocess.run(
        [str(python), "-I", "-c", _PROBE, str(result_file), *roots],
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"导入探针退出码 {proc.returncode}: {_tail(proc.stderr)}")
    data = json.loads(result_file.read_text(encoding="utf-8"))
    return ProbeResult(imported=set(data["imported"]), failures=data["failures"])


def compare(package: LocalPackage, result: ProbeResult) -> list[str]:
    """把探针结果与源码树对照，返回错误列表（空列表表示通过）。

    有模块导入失败时只报告失败：导入失败的子包无法遍历，再对照只会多出一串
    “缺少”的模块。
    """
    if result.failures:
        return [f"{name}: {reason}" for name, reason in sorted(result.failures.items())]
    expected = source_modules(package)
    errors = [
        f"{name}: 源码树中有这个模块，但没有从安装结果中导入到"
        for name in sorted(expected - result.imported)
    ]
    errors.extend(
        f"{name}: 导入到的模块不在源码树中" for name in sorted(result.imported - expected)
    )
    return errors


def _tail(output: str, lines: int = 20) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def _run(command: list[str]) -> None:
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command[:4])} ... 退出码 {proc.returncode}:\n"
            f"{_tail(proc.stdout + proc.stderr)}"
        )


def build_wheels(packages: list[LocalPackage], workdir: Path) -> dict[str, Path]:
    """为给定的包构建 wheel，返回规范化分发名到 wheel 路径的映射。

    在副本上构建：setuptools 会在源码目录中写 ``build/`` 与 ``*.egg-info``，还会把
    ``build/lib`` 中残留的旧模块打进 wheel。
    """
    copies = []
    for package in packages:
        copy = workdir / "src" / package.path.name
        shutil.copytree(
            package.path,
            copy,
            ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "build", "dist"),
        )
        copies.append(str(copy))
    wheel_dir = workdir / "wheels"
    _run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", str(wheel_dir), *copies])
    return {normalize(w.name.split("-", 1)[0]): w for w in wheel_dir.glob("*.whl")}


def check_package(
    package: LocalPackage,
    packages: dict[str, LocalPackage],
    wheels: dict[str, Path],
    workdir: Path,
) -> list[str]:
    """在全新的虚拟环境中安装并导入一个包，返回错误列表。"""
    if not package.roots:
        return [f"src/{NAMESPACE}/ 下没有任何包"]
    env_dir = workdir / "venvs" / package.path.name
    venv.EnvBuilder(with_pip=True, symlinks=True).create(env_dir)
    python = env_dir / "bin" / "python"
    specs = [
        f"{wheels[name]}[{','.join(sorted(extras))}]" if extras else str(wheels[name])
        for name, extras in sorted(resolve_local(package.name, packages).items())
    ]
    _run([str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", *specs])
    return compare(package, probe(python, package.roots, env_dir))


def main(argv: list[str] | None = None, packages_dir: Path = PACKAGES_DIR) -> int:
    """入口：逐包输出 OK/FAIL 明细，任一包失败以退出码 1 结束。"""
    parser = argparse.ArgumentParser(description="在全新的虚拟环境中安装并导入 packages/ 下的包")
    parser.add_argument("dirs", nargs="*", metavar="DIR", help="包目录名，默认检查全部包")
    args = parser.parse_args(argv)

    packages = load_packages(packages_dir)
    if not packages:
        print(f"ERROR: {packages_dir} 下未找到任何 pyproject.toml", file=sys.stderr)
        return 1
    by_dir = {p.path.name: p for p in packages.values()}
    unknown = sorted(set(args.dirs) - by_dir.keys())
    if unknown:
        print(f"ERROR: {packages_dir} 下没有这些包: {', '.join(unknown)}", file=sys.stderr)
        return 1
    targets = [by_dir[d] for d in sorted(set(args.dirs))] if args.dirs else list(by_dir.values())

    failed = 0
    with tempfile.TemporaryDirectory(prefix="forgemesh-imports-") as tmp:
        workdir = Path(tmp)
        try:
            needed: set[str] = set()
            for target in targets:
                needed.update(resolve_local(target.name, packages))
            wheels = build_wheels([packages[name] for name in sorted(needed)], workdir)
        except (ValueError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        for target in targets:
            label = f"packages/{target.path.name} ({target.name})"
            try:
                errors = check_package(target, packages, wheels, workdir)
            except RuntimeError as exc:
                errors = [str(exc)]
            if errors:
                failed += 1
                for error in errors:
                    print(f"FAIL: {label}: {error}")
            else:
                print(f"OK: {label}: {len(source_modules(target))} 个模块全部可导入")

    print(f"summary: {len(targets) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
