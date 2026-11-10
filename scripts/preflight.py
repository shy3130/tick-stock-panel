#!/usr/bin/env python
"""基础代码预检: 只查「机器能判定」的低级错误。

为什么存在这个脚本
------------------
本仓库 CONTRIBUTING.md 有 864 行规范, ruff 也配了规则集, 但 **CI 只跑 pytest**,
没有任何一道门真正执行这些文字 —— 于是同一个坑反复踩。本脚本把每一次真实事故
固化成一条可执行的检查, 让「下次注意」变成「过不去」。

每条规则都标注了出处(真实事故), 不要因为「看起来多余」删掉它。

用法::

    python scripts/preflight.py              # 全量
    python scripts/preflight.py --staged     # 只查 git 暂存文件(pre-commit 用)
    python scripts/preflight.py --strict     # WARN 也当失败

退出码: 0 通过 / 1 有 ERROR(--strict 时含 WARN)
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY_ROOTS = ("backend/app", "backend/tests", "backend/scripts", "scripts", "mcp-server")
FRONTEND_SRC = "frontend/src"
DATA_ROOT = "data"

# bat/cmd 里的非 ASCII 只在「文件名」允许; 文件内容必须纯 ASCII, 否则 cmd 用 GBK
# 解析 UTF-8 中文会乱码, 连 pause 都不执行(2026-11-10 双击闪退事故根因之一)。
TEXT_FILE_EXTS = {".bat", ".cmd"}

# 同名组件白名单: 这些文件名按约定天然多份, 不视为重复造轮子。
COMPONENT_STEM_ALLOW = {
    "index", "types", "constants", "utils", "main", "App", "vite-env",
}

NOQA_RE = re.compile(r"#\s*noqa", re.IGNORECASE)


@dataclass
class Issue:
    level: str  # ERROR | WARN
    rule: str
    path: str
    line: int
    msg: str

    def render(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.level}] {self.rule} {loc}  {self.msg}"


# ---------------------------------------------------------------- 文件收集


def _iter_files(roots: tuple[str, ...], exts: set[str], staged: list[str] | None):
    """遍历指定根目录下的匹配文件; staged 非空时只返回暂存文件。"""
    if staged is not None:
        for rel in staged:
            p = ROOT / rel
            if p.suffix.lower() in exts and p.is_file():
                yield p
        return
    for root in roots:
        base = ROOT / root
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d for d in dirnames
                if d not in {"__pycache__", "node_modules", ".venv", ".git", "dist", "build"}
            ]
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() in exts:
                    yield p


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------- 规则 1: 语法


def check_python_syntax(files: list[Path]) -> list[Issue]:
    """R001 语法: 能不能 import。改完不跑一遍就交付 = 必踩。"""
    issues: list[Issue] = []
    for p in files:
        try:
            ast.parse(_read_text(p), filename=str(p))
        except SyntaxError as exc:
            issues.append(Issue("ERROR", "R001-syntax", _rel(p), exc.lineno or 0, str(exc.msg)))
    return issues


# ---------------------------------------------------------------- 规则 2: 吞异常


_HANDLED = "handled"      # 有实际处理逻辑(含调用包装函数 re-raise), 不报
_SKIP = "skip"            # continue/break: 跳过当前项但无痕迹
_DEFAULT = "default"      # return 常量: 降级返回默认值
_SILENT = "silent"        # pass / ... / 只有 docstring: 完全静默


def _classify_handler(handler: ast.ExceptHandler) -> str:
    """给 except 体分级。

    关键教训: 初版只认 `ast.Raise` 节点, 于是 `_raise_candidate_error(exc)` 这种
    「调用包装函数把异常转成业务异常抛出」被误判成吞异常 —— 本仓 45 条告警是误报。
    尺子不准时先去校准尺子, 不要拿它去改业务代码。
    """
    body = [
        node for node in handler.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    if not body:
        return _SILENT
    if all(isinstance(node, ast.Pass) for node in body):
        return _SILENT
    if all(isinstance(node, (ast.Continue, ast.Break)) for node in body):
        return _SKIP
    if all(isinstance(node, ast.Return) for node in body):
        if all(
            node.value is None
            or isinstance(node.value, (ast.Constant, ast.Name, ast.List, ast.Dict, ast.Tuple))
            for node in body
        ):
            return _DEFAULT
    return _HANDLED


def _staged_new_lines() -> dict[str, set[int]]:
    """解析 `git diff --cached -U0`, 返回 {文件: 新增行号集合}。

    棘轮必须只卡新增行: 只改一个文件的某个字符, 不该连带把该文件里所有既有
    静默点都判成新增违规 —— 那样改老文件会被无差别拦截, 人就会开始用
    `--no-verify` 绕过, 门禁随之失效。
    """
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    result: dict[str, set[int]] = {}
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            result.setdefault(current, set())
        elif line.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count:
                result[current].update(range(start, start + count))
    return result


def check_swallowed_exception(
    files: list[Path],
    *,
    new_code: bool,
    new_lines: dict[str, set[int]] | None = None,
) -> list[Issue]:
    """R002 吞异常: except 捕获了却不留痕。

    事故: mining_process_lock 抛 PermissionError, 调用方只 `except MiningProcessLockError`
    + 兜底 `except Exception: pass`, 降级没生效、进程直接退出且无日志。

    棘轮设计: 全仓存量有 64 处 `except ...: pass`, 多数是有意的(清理临时文件、
    可选操作忽略失败)。一并判 ERROR 会让 CI 永久变红 —— 那又是一条没人看的规则。
    因此: 存量判 WARN, 新增代码判 ERROR。新代码必须留痕, 存量逐步还。
    既有代码若确实有意静默, 在该行加 `# noqa: R002` 显式承认。
    """
    issues: list[Issue] = []
    for p in files:
        src = _read_text(p)
        lines = src.splitlines()
        try:
            tree = ast.parse(src, filename=str(p))
        except SyntaxError:
            continue  # R001 已报
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            line_txt = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
            if NOQA_RE.search(line_txt):
                continue
            if node.type is None:
                issues.append(Issue(
                    "ERROR", "R002-bare-except", _rel(p), node.lineno,
                    "裸 `except:` 会连 KeyboardInterrupt/SystemExit 一起吞掉, 必须指名异常类型",
                ))
                continue
            kind = _classify_handler(node)
            is_broad = isinstance(node.type, ast.Name) and node.type.id in {
                "Exception", "BaseException",
            }
            if kind == _SILENT:
                touched = bool(new_lines) and node.lineno in new_lines.get(_rel(p), set())
                level = "ERROR" if (new_code and touched) else "WARN"
                issues.append(Issue(
                    level, "R002-silent-catch", _rel(p), node.lineno,
                    "捕获后什么都不做(pass): 故障被彻底掩埋, 至少记一条日志"
                    + ("" if level == "ERROR" else "; 若确属有意静默, 加 `# noqa: R002`"),
                ))
            elif kind == _SKIP and is_broad:
                issues.append(Issue(
                    "WARN", "R002-skip-without-trace", _rel(p), node.lineno,
                    f"`except {node.type.id}` 后直接跳过, 无任何痕迹",
                ))
            elif kind == _DEFAULT and is_broad:
                issues.append(Issue(
                    "WARN", "R002-default-without-trace", _rel(p), node.lineno,
                    f"`except {node.type.id}` 后静默返回默认值, "
                    "调用方无法区分「确实为空」与「读失败」",
                ))
    return issues


# ---------------------------------------------------------------- 规则 3: Int8 溢出


def check_int8_overflow(files: list[Path]) -> list[Issue]:
    """R003 Int8 溢出: polars dt.hour()/month() 是 Int8, 直接乘大于 1 的常数会回绕。

    事故: `dt.hour() * 100` 在 14 点后变成负数, 尾盘统计恒为空且不报错。
    """
    issues: list[Issue] = []
    narrow_attrs = {"hour", "minute", "second", "month", "day", "weekday", "ordinal_day"}
    for p in files:
        src = _read_text(p)
        lines = src.splitlines()
        try:
            tree = ast.parse(src, filename=str(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, (ast.Mult, ast.Add)):
                continue
            left = node.left
            target = None
            # 形如 pl.col("x").dt.hour() * 100  ->  Call(func=Attribute(attr="hour"))
            if isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute):
                if left.func.attr in narrow_attrs:
                    target = left.func.attr
            if not target:
                continue
            const = node.right
            if isinstance(const, ast.Constant) and isinstance(const.value, int) and const.value > 9:
                lineno = getattr(node, "lineno", 0)
                line_txt = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
                if "cast" in line_txt or NOQA_RE.search(line_txt):
                    continue
                issues.append(Issue(
                    "ERROR", "R003-int8-overflow", _rel(p), lineno,
                    f"`dt.{target}()` 是 Int8, 直接运算 {const.value} 会溢出回绕, "
                    "先 .cast(pl.Int32) 再算",
                ))
    return issues


# ---------------------------------------------------------------- 规则 4: 文本文件


def check_text_encoding(paths: list[Path]) -> list[Issue]:
    """R004 启动脚本编码: bat/cmd 必须 CRLF + 内容纯 ASCII。

    事故: 用 UTF-8 中文 + LF 写 .bat, cmd 按 GBK 解析直接把整行吃掉,
    连末尾 pause 都没执行 —— 表现为「双击闪退, 窗口不留」。
    """
    issues: list[Issue] = []
    for p in paths:
        raw = p.read_bytes()
        if not raw:
            continue
        if b"\n" not in raw:
            continue
        crlf = raw.count(b"\r\n")
        lf = raw.count(b"\n")
        if crlf != lf:
            issues.append(Issue(
                "ERROR", "R004-bat-lineending", _rel(p), 0,
                f"{lf - crlf} 行是裸 LF, Windows cmd 需要 CRLF, 必须整文件转换",
            ))
        try:
            raw.decode("ascii")
        except UnicodeDecodeError:
            has_utf8_page = b"chcp 65001" in raw
            issues.append(Issue(
                "ERROR" if not has_utf8_page else "WARN", "R004-bat-nonascii",
                _rel(p), 0,
                "文件含非 ASCII 但未 `chcp 65001`; cmd 会按 GBK 解析成乱码。"
                "约定: bat 内容纯 ASCII, 中文只放在被调用的文件名里",
            ))
    return issues


def check_ps1_bom(paths: list[Path]) -> list[Issue]:
    """R005 ps1 编码: 含中文的 .ps1 建议 UTF-8 with BOM。

    分级理由: 非 ASCII 只在 `#` 注释里时, WinPS 5.1 按 GBK 解出来是乱码但注释会被
    丢弃, 不改变语义 -> WARN; 一旦非 ASCII 出现在字符串字面量/变量名里就是真错误
    -> ERROR。不做这个区分的话, 本仓 dev.ps1 会被判死, 门禁反而失去可信度。
    """
    issues: list[Issue] = []
    for p in paths:
        raw = p.read_bytes()
        if not raw:
            continue
        if raw.startswith(b"\xef\xbb\xbf"):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            issues.append(Issue(
                "ERROR", "R005-ps1-encoding", _rel(p), 0,
                "文件不是合法 UTF-8, 且无 BOM; 中文字符串会变成乱码",
            ))
            continue
        if text.isascii():
            continue
        code_lines: list[int] = []
        for idx, line in enumerate(text.splitlines(), start=1):
            if line.isascii():
                continue
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith("<#"):
                continue
            code_lines.append(idx)
        if code_lines:
            issues.append(Issue(
                "ERROR", "R005-ps1-nonascii-code", _rel(p), code_lines[0],
                "非 ASCII 出现在代码(非注释)中且无 UTF-8 BOM; WinPS 5.1 按 GBK 解码会改语义",
            ))
        else:
            issues.append(Issue(
                "WARN", "R005-ps1-comment-only", _rel(p), 0,
                "中文只存在于注释, 无 BOM 仅是注释乱码; 便于阅读可加 BOM(不影响执行)",
            ))
    return issues


# ---------------------------------------------------------------- 规则 5: parquet 重复


def check_parquet_dup(staged: list[str] | None) -> list[Issue]:
    """R006 分区内重复文件: 同一 date= 目录下并存 part.parquet 与 <symbol>.parquet。

    事故: 通配 `**/*.parquet` 视图把两份都读进去, 11-09 出现 11149 行 / 5575 标的
    (实际只有 5575 行), 指标全部翻倍污染。写入端必须先查同目录已有 symbol 再落盘。
    """
    if staged is not None:
        return []  # 数据文件不进版本库, pre-commit 阶段无意义
    base = ROOT / DATA_ROOT
    if not base.exists():
        return []
    issues: list[Issue] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "_trash"]
        pq = {f for f in filenames if f.lower().endswith(".parquet")}
        if len(pq) <= 1:
            continue
        if "part.parquet" not in {f.lower() for f in pq}:
            continue
        other = sorted(f for f in pq if f.lower() != "part.parquet")
        issues.append(Issue(
            "ERROR", "R006-parquet-dup", _rel(Path(dirpath)), 0,
            f"part.parquet 与 {', '.join(other[:3])} 并存 -> 通配读取会重复计数",
        ))
        if len(issues) >= 20:
            issues.append(Issue("WARN", "R006-parquet-dup", _rel(base), 0, "重复目录过多, 已截断"))
            break
    return issues


# ---------------------------------------------------------------- 规则 6: 组件重复


def _git_lines(args: list[str]) -> list[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _is_component(path: str) -> bool:
    if not path.startswith(FRONTEND_SRC + "/"):
        return False
    if not path.endswith((".tsx", ".ts")):
        return False
    name = path.rsplit("/", 1)[-1]
    if ".test." in name or ".spec." in name or name.endswith(".d.ts"):
        return False
    return name.rsplit(".", 1)[0] not in COMPONENT_STEM_ALLOW


def check_duplicate_components(staged: list[str] | None) -> list[Issue]:
    """R007 重复造轮子: 新增的前端文件与仓库里已存在的文件同名。

    事故: 我给「分时小图」新建了 components/MiniIntraday.tsx, 而项目早有
    components/stock-table/MiniIntraday.tsx(还带 4 个测试), 白写一套还制造二义。

    只在「新增」方向上检查: 既有同名(如各 custom/*/Page.tsx 插槽约定)属正常分层,
    一并报会让 123 条噪声淹没真问题 —— 门禁没人看的规则等于没有规则。
    """
    tracked = [f for f in _git_lines(["ls-files"]) if _is_component(f)]
    if staged is not None:
        candidates = [f for f in staged if _is_component(f)]
        existing = set(tracked)
    else:
        candidates = [f for f in _git_lines(["ls-files", "--others", "--exclude-standard"])
                      if _is_component(f)]
        existing = set(tracked)

    by_stem: dict[str, list[str]] = {}
    for f in existing:
        by_stem.setdefault(f.rsplit("/", 1)[-1].rsplit(".", 1)[0], []).append(f)

    issues: list[Issue] = []
    for f in candidates:
        stem = f.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        hits = [e for e in by_stem.get(stem, []) if e != f]
        if hits:
            issues.append(Issue(
                "ERROR", "R007-dup-component", f, 0,
                f"新增 `{stem}` 与已有 {', '.join(hits[:3])} 同名 —— "
                "先确认既有实现能否复用, 否则就是重复造轮子",
            ))
    return issues


# ---------------------------------------------------------------- 入口


def _staged_files() -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return files or None


def main() -> int:
    ap = argparse.ArgumentParser(description="基础代码预检")
    ap.add_argument("--staged", action="store_true", help="只检查 git 暂存文件")
    ap.add_argument("--strict", action="store_true", help="WARN 也视为失败")
    ap.add_argument("--verbose", action="store_true", help="列出全部 WARN 明细")
    args = ap.parse_args()

    staged = _staged_files() if args.staged else None

    py_files = list(_iter_files(PY_ROOTS, {".py"}, staged))
    bat_files = list(_iter_files((".",), TEXT_FILE_EXTS, staged))
    ps1_files = list(_iter_files((".",), {".ps1"}, staged))

    issues: list[Issue] = []
    issues += check_python_syntax(py_files)
    issues += check_swallowed_exception(
        py_files,
        new_code=staged is not None,
        new_lines=_staged_new_lines() if staged is not None else None,
    )
    issues += check_int8_overflow(py_files)
    issues += check_text_encoding(bat_files)
    issues += check_ps1_bom(ps1_files)
    issues += check_parquet_dup(staged)
    issues += check_duplicate_components(staged)

    errors = [i for i in issues if i.level == "ERROR"]
    warns = [i for i in issues if i.level == "WARN"]

    scope = f"暂存 {len(py_files)} 个 py 文件" if staged else (
        f"{len(py_files)} 个 py / {len(bat_files)} 个 bat / {len(ps1_files)} 个 ps1"
    )
    print(f"预检范围: {scope}")

    for issue in errors:
        print(issue.render())

    shown = warns if args.verbose else warns[:5]
    for issue in shown:
        print(issue.render())
    if len(warns) > len(shown):
        print(f"... 另有 {len(warns) - len(shown)} 条 WARN 未列出, 加 --verbose 查看")

    print(f"\n结果: {len(errors)} ERROR / {len(warns)} WARN")
    if errors:
        print("ERROR 必须修掉再提交。逐条对应 CONTRIBUTING.md / 提交历史里的真实事故。")
        return 1
    if warns and args.strict:
        print("--strict: WARN 视为失败。")
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
