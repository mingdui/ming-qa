#!/usr/bin/env python3
"""Deterministic helpers for the quality-assurance-agent skill."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SKILL_VERSION = "2.0.0"
# 8 个 skill 目录（主 skill + 7 个子 skill），安装时按此清单覆盖
SKILL_DIRS = [
    "quality-assurance-agent",
    "qa-code-reviewer",
    "qa-context-profiler",
    "qa-report-generator",
    "qa-risk-analyzer",
    "qa-test-runner",
    "qa-test-script-generator",
    "qa-testcase-designer",
]
DEFAULT_MODELS = ["gpt-5.4", "claude-sonnet-5", "deepseek-v4-pro"]
# 无内置默认值：LLM 网关地址必须由用户提供（QA_AGENT_LLM_BASE_URL 环境变量或 --base-url）。
# 未配置时多模型交叉审查会跳过并给出明确提示，不会向任何地址发起请求。
DEFAULT_BASE_URL = ""
DEFAULT_API_KEY_ENV = "QA_AGENT_LLM_API_KEY"
DEFAULT_BASE_URL_ENV = "QA_AGENT_LLM_BASE_URL"

# ── Agent 取值 ────────────────────────────────────────────────────────────
# 规范名是 claude-code：Agent Skills 生态（npx skills 等）就是这么指代 Claude Code 的，
# 用户在本 CLI 里要写的字面量应与之一致，否则同一个概念要记两套写法。
#
# 历史名 claude 保留为别名。QA_AGENT=claude 已经写进了既有项目的
# .qa-agent/local/.env，直接改名会让那些项目静默失效。别名在 CLI 入口归一，
# 内部一律按规范名比较。
AGENT_CLAUDE = "claude-code"
AGENT_CODEX = "codex"
AGENT_BOTH = "both"
AGENT_CHOICES = (AGENT_CLAUDE, AGENT_CODEX, AGENT_BOTH)
_AGENT_NAME_ALIASES = {"claude": AGENT_CLAUDE}


def normalize_agent_name(value: str) -> str:
    """把 agent 取值归一为规范名（claude → claude-code；未知值原样返回）。"""
    v = str(value or "").strip().lower()
    return _AGENT_NAME_ALIASES.get(v, v)


# Playwright 自己的 `init-agents` 用的是**另一套** loop 词表
# （claude / codex / copilot / opencode / vscode / vscode-legacy）。
# 本 CLI 的 agent 规范名是 claude-code，直接透传会被 npx 拒绝：
#   option '--loop <loop>' argument 'claude-code' is invalid.
#   Allowed choices are claude, codex, copilot, opencode, vscode, vscode-legacy.
# 所以在本 CLI 边界内统一用 claude-code，只在调用 Playwright 时翻译回它的写法。
_PLAYWRIGHT_LOOP_NAMES = {AGENT_CLAUDE: "claude", AGENT_CODEX: "codex"}


def playwright_loop_name(agent: str) -> str:
    """把本 CLI 的 agent 名翻译成 Playwright init-agents 能接受的 --loop 取值。

    未知取值原样返回——Playwright 支持的 loop 不止 claude/codex
    （copilot / opencode / vscode 等），不应该在本 CLI 里把它们拦掉。
    """
    name = normalize_agent_name(agent)
    return _PLAYWRIGHT_LOOP_NAMES.get(name, name)
PLAYWRIGHT_TEMPLATE_ASSET_MAP = {
    ".codex/agents/README.md": ASSETS / "playwright" / "README.md",
    ".codex/agents/playwright_test_planner.toml": ASSETS / "playwright" / "playwright_test_planner.toml",
    ".codex/agents/playwright_test_generator.toml": ASSETS / "playwright" / "playwright_test_generator.toml",
    ".codex/agents/playwright_test_healer.toml": ASSETS / "playwright" / "playwright_test_healer.toml",
    ".qa-agent/fixtures/playwright-e2e-layout.example.md": ASSETS / "playwright" / "playwright-e2e-layout.example.md",
}


def configure_utf8_stdio() -> None:
    """Prefer UTF-8 for user-facing Chinese CLI output on Windows and shells."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass


def zh_readiness_label(value: Any) -> str:
    labels = {
        "Ready": "就绪（Ready）",
        "ready": "就绪（Ready）",
        "Conditionally Ready": "有条件就绪（Conditionally Ready）",
        "conditionally-ready": "有条件就绪（Conditionally Ready）",
        "Not Ready": "未就绪（Not Ready）",
        "not-ready": "未就绪（Not Ready）",
        "Incomplete": "未完成（Incomplete）",
        "complete": "完成（complete）",
        "complete-with-allowed-gaps": "有条件完成（complete-with-allowed-gaps）",
        "complete_not_ready": "完成但未就绪（complete_not_ready）",
        "complete-not-ready": "完成但未就绪（complete-not-ready）",
        "incomplete": "未完成（incomplete）",
        "passed": "通过（passed）",
        "failed": "失败（failed）",
        "missing": "缺失（missing）",
        "N/A": "不适用（N/A）",
    }
    text = str(value or "N/A")
    return labels.get(text, text)
CASE_LAYERS = {
    "frontend-unit",
    "backend-unit",
    "api",
    "integration",
    "e2e",
    "manual",
    "unknown",
}
CASE_PRIORITIES = {"P0", "P1", "P2", "P3"}
CASE_AUTOMATION = {"automated", "candidate", "manual", "blocked"}
CASE_STATUS = {
    "draft",
    "confirmed",
    "implemented",
    "passed",
    "failed",
    "skipped",
    "blocked",
}
SPEC_TASK_LAYERS = {"unit", "integration", "api", "e2e"}
SPEC_TASK_STATUS = {"pending", "implemented", "passed", "failed", "blocked", "skipped"}
SPEC_TASK_UNIMPLEMENTED_STATUS = {"", "pending", "not-started", "not_started", "todo", "planned"}
SPEC_TASK_TERMINAL_IMPLEMENTATION_STATUS = {
    "implemented",
    "passed",
    "failed",
    "blocked",
    "skipped",
    "deferred",
    "explicitly-deferred",
    "explicitly_deferred",
}
SPEC_TASK_UNEXECUTED_STATUS = {"", "not-run", "not_run", "pending", "todo", "planned"}
SPEC_TASK_TERMINAL_EXECUTION_STATUS = {
    "passed",
    "failed",
    "blocked",
    "skipped",
    "deferred",
    "explicitly-deferred",
    "explicitly_deferred",
}
DEFAULT_SPEC_TASK_TARGET_RATIO = {"unit": 0.60, "integration": 0.20, "api": 0.15, "e2e": 0.05}
DEFAULT_SPEC_TASK_MIN_BY_PRIORITY = {"P0": 8, "P1": 5, "P2": 3, "P3": 1}
MOJIBAKE_TEXT_EXTENSIONS = {
    ".cjs",
    ".css",
    ".csv",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MOJIBAKE_SKIP_DIRS = {
    ".git",
    ".next",
    ".playwright-mcp",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "test-results",
}
MOJIBAKE_PROBES = [
    "\u6d4b\u8bd5",
    "\u6d4b\u8bd5\u7528\u4f8b",
    "\u751f\u6210\u65f6\u95f4",
    "\u8d28\u91cf\u95e8",
    "\u6267\u884c\u8bc1\u636e",
    "\u64cd\u4f5c\u6210\u529f",
    "\u767b\u5f55",
    "\u5ba1\u6838",
    "\u73af\u5883\u68c0\u6d4b",
    "\u4e1a\u52a1\u6d4b\u8bd5\u7528\u4f8b",
    "\u4ee3\u7801\u5ba1\u67e5",
    "\u6d4b\u8bd5\u7528\u4f8b\u786e\u8ba4\u9875",
    "\u901a\u8fc7",
    "\u5931\u8d25",
    "\u963b\u585e",
    "\u6458\u8981",
]
TECHNICAL_CASE_TOKENS = {
    "maven",
    "surefire",
    "vitest",
    "node:test",
    "playwright test agents",
    "playwright installed",
    "playwright 安装",
    "playwright 用例可枚举",
    "compile",
    "build",
    "npm run build",
    "测试套件",
    "全量测试",
    "构建基线",
    "可枚举",
    "自动安装",
    "环境检测",
    "环境阻塞",
    "运行环境阻塞",
    "上下文采集",
    "qa agent",
    "smoke",
}
BUSINESS_CASE_HINTS = {
    "提交",
    "审核",
    "回调",
    "结算",
    "发布",
    "告警",
    "通知",
    "登录",
    "权限",
    "提现",
    "素材",
    "方案",
    "采集",
    "用户",
    "订单",
    "支付",
    "business",
    "callback",
    "settlement",
    "publish",
    "notify",
    "audit",
    "permission",
}

NARRATIVE_CASE_FIELDS = [
    "title",
    "preconditions",
    "steps",
    "expected",
    "businessActor",
    "operationPath",
    "businessStateBefore",
    "businessAction",
    "businessStateAfter",
    "businessAssertions",
    "risk",
]
ROOT_NARRATIVE_FIELDS = ["assumptions", "openQuestions"]
CHINESE_TEXT_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


class QaAgentError(RuntimeError):
    """Raised for expected CLI failures."""


_CN_TZ = dt.timezone(dt.timedelta(hours=8))  # 报告统一使用东八区北京时间（UTC+8）


def utc_now() -> str:
    return dt.datetime.now(_CN_TZ).replace(microsecond=0).isoformat()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# ── 项目经验记忆（knowledge） ─────────────────────────────

def save_knowledge(repo: Path, module: str, entry: dict[str, Any]) -> Path:
    """向 .qa-agent/knowledge/<module>.json 追加一条经验记录。"""
    knowledge_dir = repo / ".qa-agent" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / f"{module}.json"
    data: dict[str, Any] = {"module": module, "entries": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass
    entry.setdefault("recordedAt", utc_now())
    data["entries"].append(entry)
    write_json(path, data)
    return path


def load_knowledge(repo: Path, module: str | None = None) -> dict[str, Any]:
    """加载 .qa-agent/knowledge/ 目录下的经验记录。module 为空时加载全部。"""
    knowledge_dir = repo / ".qa-agent" / "knowledge"
    if not knowledge_dir.exists():
        return {}
    if module:
        path = knowledge_dir / f"{module}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}
    all_knowledge: dict[str, Any] = {}
    for path in sorted(knowledge_dir.glob("*.json")):
        mod_name = path.stem
        try:
            all_knowledge[mod_name] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass
    return all_knowledge


def cmd_save_knowledge(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    entry = {}
    if args.category:
        entry["category"] = args.category
    if args.summary:
        entry["summary"] = args.summary
    if args.detail:
        entry["detail"] = args.detail
    if args.tags:
        entry["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if not entry:
        print("错误：至少需要 --category 和 --summary", file=sys.stderr)
        sys.exit(1)
    path = save_knowledge(repo, args.module, entry)
    print(f"已保存经验：{path}")


def cmd_show_knowledge(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    module_arg = args.module if getattr(args, "module", None) else None
    data = load_knowledge(repo, module_arg)

    if not data:
        print("暂无经验记录")
        return

    # 规范化为统一格式：{module: [entries]}
    if module_arg:
        # 单模块模式：load_knowledge 返回 list，需要包装成 dict
        if isinstance(data, list):
            data = {module_arg: data}
        else:
            # 如果返回的是 dict（旧格式兼容），保持不变
            pass
    else:
        # 全模块模式：load_knowledge 已经返回 {module: list}
        pass

    # 如果指定了 --category，过滤结果
    if getattr(args, "category", None):
        category = args.category
        filtered_data = {}
        for module, entries in data.items():
            if isinstance(entries, list):
                filtered_entries = [e for e in entries if e.get("category") == category]
                if filtered_entries:
                    filtered_data[module] = filtered_entries
            else:
                # 旧格式兼容：单个 entry 对象
                if entries.get("category") == category:
                    filtered_data[module] = entries

        if not filtered_data:
            print(f"未找到分类为 '{category}' 的经验记录")
            return
        data = filtered_data

    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# manifest.json — 子 skill 间结构化数据传递
# ---------------------------------------------------------------------------

MANIFEST_PATH = ".qa-agent/current/manifest.json"

MANIFEST_STAGES = [
    "context-profiler",
    "risk-analyzer",
    "testcase-designer",
    "test-script-generator",
    "test-runner",
    "code-reviewer",
    "report-generator",
]


def _repo_rel(repo: Path, path: Path) -> str:
    """返回相对于 repo 根目录的路径字符串。"""
    try:
        return str(path.relative_to(repo)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def update_manifest(repo: Path, *, stage: str | None = None,
                    artifact: dict[str, str] | None = None,
                    status: dict[str, str] | None = None) -> Path:
    """更新 .qa-agent/current/manifest.json，供下游 skill 读取上游产物路径和阶段状态。

    用法示例：
        update_manifest(repo, stage="risk-analyzer",
                        artifact={"risk-analysis": ".qa-agent/current/risk-analysis.json"},
                        status={"risksAnalyzed": "done"})
    """
    manifest_file = repo / MANIFEST_PATH
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if manifest_file.exists():
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            data = {}
    data.setdefault("version", "1.0")
    data.setdefault("artifacts", {})
    data.setdefault("status", {})
    if stage:
        data["currentStage"] = stage
    if artifact:
        data["artifacts"].update(artifact)
    if status:
        data["status"].update(status)
    data["updatedAt"] = utc_now()
    write_json(manifest_file, data)
    return manifest_file


def safe_write_json(path: Path, content: str) -> dict[str, Any]:
    """写入 JSON 字符串 → 校验合法性 → 检查编码 → 返回校验结果。

    用于规避 AI Write/Edit 工具在 Windows 上写中文 JSON 时偶发 U+FFFD
    编码损坏的问题。调用方应通过 Python 脚本而非 AI 工具写文件内容。
    """
    result: dict[str, Any] = {"path": str(path), "ok": False, "mojibake": [], "json_valid": False}
    # 防御性清理：移除孤立代理字符（stdin 管道中可能残留）
    content = content.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    # 校验 JSON 合法性
    try:
        data = json.loads(content)
        result["json_valid"] = True
        result["top_keys"] = list(data.keys())[:10] if isinstance(data, dict) else ["<list>"]
    except json.JSONDecodeError as exc:
        result["error"] = f"JSON 解析失败: {exc}"
        return result
    # 校验编码
    mojibake: list[dict[str, Any]] = []
    for idx, ch in enumerate(content):
        if ch == "\ufffd":
            start = max(0, idx - 40)
            end = min(len(content), idx + 40)
            mojibake.append({"position": idx, "context": repr(content[start:end])})
        if len(mojibake) >= 10:
            break
    result["mojibake"] = mojibake
    if mojibake:
        result["error"] = f"发现 {len(mojibake)} 处 U+FFFD 替换字符"
    else:
        result["ok"] = True
    return result


def safe_write_json_cmd(args: argparse.Namespace) -> None:
    """safe-write-json 命令处理函数"""
    if args.from_stdin:
        content = sys.stdin.read()
    elif args.json_string:
        content = args.json_string
    else:
        print("错误：需要 --from-stdin 或 --json-string", file=sys.stderr)
        sys.exit(1)
    result = safe_write_json(Path(args.path).resolve(), content)
    if result["ok"]:
        print(f"安全写入成功: {result['path']}")
    else:
        print(f"写入异常: {result.get('error', 'unknown')}")
        if result.get("mojibake"):
            for item in result["mojibake"]:
                print(f"  位置 {item['position']}: {item['context']}")
        sys.exit(1)


def read_text(path: Path, limit: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > limit:
        return text[:limit] + "\n...[truncated]..."
    return text


def mojibake_variants(value: str) -> set[str]:
    variants: set[str] = set()
    for encoding in ("gbk", "cp936", "latin1"):
        try:
            variant = value.encode("utf-8").decode(encoding, errors="ignore")
        except (LookupError, UnicodeError):
            continue
        if variant and variant != value:
            variants.add(variant)
    return variants


MOJIBAKE_MARKERS = sorted(
    {
        marker
        for probe in MOJIBAKE_PROBES
        for marker in mojibake_variants(probe)
        if len(marker) >= 2
    },
    key=len,
    reverse=True,
)
LATIN1_MOJIBAKE_RE = re.compile(r"(?:\u00c3.|\u00c2.|\u00e2[\u0080-\u2122]|\u00f0[\u009f-\u0178])")


def text_position(text: str, start: int) -> tuple[int, int]:
    line = text.count("\n", 0, start) + 1
    last_newline = text.rfind("\n", 0, start)
    column = start + 1 if last_newline < 0 else start - last_newline
    return line, column


def mojibake_snippet(text: str, start: int, end: int, radius: int = 42) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return text[left:right].replace("\r", "\\r").replace("\n", "\\n")


def detect_mojibake_text(text: str, path: str = "", max_findings: int = 20) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    issue_count = 0
    gbk_ranges: list[tuple[int, int]] = []

    def add(kind: str, start: int, end: int, match: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(findings) >= max_findings:
            return
        line, column = text_position(text, start)
        findings.append(
            {
                "path": path,
                "kind": kind,
                "line": line,
                "column": column,
                "match": match,
                "snippet": mojibake_snippet(text, start, end),
            }
        )

    for match in re.finditer("\ufffd", text):
        add("replacement-character", match.start(), match.end(), match.group(0))
    for match in re.finditer(r"\?{4,}", text):
        add("question-mark-run", match.start(), match.end(), match.group(0))
    for match in re.finditer(r"[\u0080-\u009f]", text):
        add("control-character", match.start(), match.end(), f"U+{ord(match.group(0)):04X}")
    for match in LATIN1_MOJIBAKE_RE.finditer(text):
        add("utf8-decoded-as-latin1", match.start(), match.end(), match.group(0))
    for marker in MOJIBAKE_MARKERS:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            end = index + len(marker)
            if any(not (end <= used_start or index >= used_end) for used_start, used_end in gbk_ranges):
                start = index + 1
                continue
            add("utf8-decoded-as-gbk", index, end, marker)
            gbk_ranges.append((index, end))
            start = index + len(marker)

    return {
        "ok": issue_count == 0,
        "path": path,
        "issueCount": issue_count,
        "findings": findings,
    }


def iter_mojibake_scan_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix.lower() in MOJIBAKE_TEXT_EXTENSIONS:
                files.append(path)
            continue
        if not path.is_dir():
            continue
        for root, dirs, names in os.walk(path):
            dirs[:] = [name for name in dirs if name not in MOJIBAKE_SKIP_DIRS]
            root_path = Path(root)
            for name in names:
                candidate = root_path / name
                if candidate.suffix.lower() in MOJIBAKE_TEXT_EXTENSIONS:
                    files.append(candidate)
    return files


def scan_mojibake_paths(
    paths: list[Path],
    max_bytes: int = 2_000_000,
    max_findings_per_file: int = 20,
) -> dict[str, Any]:
    scanned_files: list[dict[str, Any]] = []
    issue_files: list[dict[str, Any]] = []
    skipped_files: list[dict[str, Any]] = []
    files = iter_mojibake_scan_files(paths)
    for file_path in files:
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            skipped_files.append({"path": str(file_path), "reason": str(exc)})
            continue
        if size > max_bytes:
            skipped_files.append({"path": str(file_path), "reason": f"larger than {max_bytes} bytes"})
            continue
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            skipped_files.append({"path": str(file_path), "reason": str(exc)})
            continue
        if b"\x00" in raw[:4096]:
            skipped_files.append({"path": str(file_path), "reason": "binary file"})
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            finding = {
                "path": str(file_path),
                "kind": "utf8-decode-error",
                "line": 1,
                "column": exc.start + 1,
                "match": str(exc),
                "snippet": "",
            }
            issue = {"ok": False, "path": str(file_path), "issueCount": 1, "findings": [finding]}
            scanned_files.append({"path": str(file_path), "size": size})
            issue_files.append(issue)
            continue
        result = detect_mojibake_text(text, str(file_path), max_findings=max_findings_per_file)
        scanned_files.append({"path": str(file_path), "size": size})
        if not result["ok"]:
            issue_files.append(result)
    return {
        "status": "passed" if not issue_files else "failed",
        "scannedFileCount": len(scanned_files),
        "issueFileCount": len(issue_files),
        "skippedFileCount": len(skipped_files),
        "issueFiles": issue_files,
        "skippedFiles": skipped_files,
    }


def print_mojibake_summary(result: dict[str, Any]) -> None:
    def safe(value: Any) -> str:
        text = str(value)
        encoding = sys.stdout.encoding or "utf-8"
        return text.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")

    print(
        "Mojibake check: {status} ({issueFileCount}/{scannedFileCount} files with issues, {skippedFileCount} skipped)".format(
            **result
        )
    )
    for issue_file in result.get("issueFiles", [])[:20]:
        print(safe(f"错误：{issue_file.get('path')}（{issue_file.get('issueCount')} 个问题）"))
        for finding in issue_file.get("findings", [])[:5]:
            line = (
                "  - {kind} at {line}:{column}: {snippet}".format(
                    kind=finding.get("kind", ""),
                    line=finding.get("line", ""),
                    column=finding.get("column", ""),
                    snippet=finding.get("snippet", ""),
                )
            )
            print(safe(line))


def check_mojibake(args: argparse.Namespace) -> None:
    paths = [Path(path).resolve() for path in args.paths]
    result = scan_mojibake_paths(
        paths,
        max_bytes=args.max_bytes,
        max_findings_per_file=args.max_findings_per_file,
    )
    if args.json:
        write_json(Path(args.json).resolve(), result)
    print_mojibake_summary(result)
    if args.strict and result["status"] != "passed":
        raise SystemExit(1)


def enforce_no_mojibake(path: Path, allow_mojibake: bool = False) -> None:
    result = scan_mojibake_paths([path], max_bytes=max(path.stat().st_size + 1, 2_000_000))
    if result["status"] == "passed":
        return
    print_mojibake_summary(result)
    if not allow_mojibake:
        raise SystemExit("Rendered artifact contains mojibake; rewrite it as UTF-8 or pass --allow-mojibake only for known raw evidence.")


def gate_env(repo: Path) -> dict[str, str]:
    """跑配置里的命令时用的环境：进程环境 + 分层 .env。

    与 `run-with-env` 对齐。历史上 `run-commands` / `run-gate` 不加载 `.qa-agent`
    的 .env，依赖数据库凭证的门禁命令第一次跑必然失败（表现为 QA_MYSQL_USER 为空），
    而且和 `run-with-env` 的行为不一致且无文档提示。
    """
    env = os.environ.copy()
    env.update(_load_env(repo))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_cmd(
    command: list[str] | str,
    cwd: Path,
    timeout: int = 120,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    start = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=shell,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        return {
            "command": command if isinstance(command, str) else " ".join(command),
            "cwd": str(cwd),
            "exitCode": completed.returncode,
            "durationSeconds": round(time.time() - start, 3),
            "stdout": completed.stdout[-20000:],
            "stderr": completed.stderr[-20000:],
        }
    except FileNotFoundError as exc:
        # 可执行文件不在 PATH。原先这里不捕获，直接抛裸 traceback
        # （FileNotFoundError: [WinError 2] 系统找不到指定的文件），使用者看不出
        # 缺的是哪个程序、该怎么装。
        missing = exc.filename or (
            command if isinstance(command, str) else (command[0] if command else "")
        )
        return {
            "command": command if isinstance(command, str) else " ".join(command),
            "cwd": str(cwd),
            "exitCode": 127,
            "durationSeconds": round(time.time() - start, 3),
            "stdout": "",
            "stderr": f"command not found: {missing}",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command if isinstance(command, str) else " ".join(command),
            "cwd": str(cwd),
            "exitCode": 124,
            "durationSeconds": round(time.time() - start, 3),
            "stdout": (exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
            "stderr": f"Command timed out after {timeout}s",
        }


def git_output(repo: Path, args: list[str], timeout: int = 60) -> str:
    """执行 git 命令并返回 stdout；命令失败时返回空字符串。

    失败时**不能**把 stderr 当作输出返回——所有调用方都把它当文件名、分支名
    或 diff 内容使用，混入 "fatal: not a git repository" 这类错误文本会产生
    非法路径。在 Windows + Python 3.9 上 Path.resolve() 会因此抛 OSError
    （3.11 起不再抛），从而中断整个风险扫描。
    """
    result = run_cmd(["git", *args], repo, timeout=timeout)
    if result["exitCode"] != 0:
        return ""
    return result.get("stdout", "")


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
    value = value.strip("-")
    return value[:48] or "qa-work"


def ignored_path(path: Path) -> bool:
    return any(part in ignored_dir_names() for part in path.parts)


def ignored_dir_names() -> set[str]:
    return {
        ".git",
        "node_modules",
        "target",
        "dist",
        "build",
        ".next",
        "coverage",
        "test-results",
        ".qa-agent",
    }


def iter_project_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    ignored = ignored_dir_names()
    for root, dirs, names in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in ignored]
        root_path = Path(root)
        for name in names:
            files.append(root_path / name)
    return files


def iter_project_dirs(repo: Path, dirname: str) -> list[Path]:
    found: list[Path] = []
    ignored = ignored_dir_names()
    for root, dirs, _ in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in ignored]
        root_path = Path(root)
        for name in dirs:
            if name == dirname:
                found.append(root_path / name)
    return found


def find_files(repo: Path, names: set[str], suffixes: tuple[str, ...] = ()) -> list[Path]:
    found: list[Path] = []
    for path in repo.rglob("*"):
        if ignored_path(path.relative_to(repo)):
            continue
        if not path.is_file():
            continue
        if path.name in names or (suffixes and path.name.endswith(suffixes)):
            found.append(path)
    return sorted(found)


def detect_stack(repo: Path) -> dict[str, Any]:
    manifests = find_files(
        repo,
        {
            "package.json",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "pytest.ini",
            "pyproject.toml",
            "playwright.config.ts",
            "playwright.config.js",
            "vite.config.ts",
            "vite.config.js",
            "next.config.js",
            "next.config.ts",
        },
    )
    stacks: list[str] = []
    summaries: list[dict[str, Any]] = []
    for manifest in manifests:
        rel = str(manifest.relative_to(repo)).replace("\\", "/")
        text = read_text(manifest, 20000)
        summary: dict[str, Any] = {"path": rel}
        if manifest.name == "package.json":
            try:
                pkg = json.loads(text)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                scripts = pkg.get("scripts", {})
                summary.update(
                    {
                        "name": pkg.get("name"),
                        "scripts": scripts,
                        "dependencies": sorted(deps.keys()),
                    }
                )
                for key in deps:
                    if key in {"react", "next", "vue", "@angular/core", "svelte"}:
                        stacks.append(key)
                    if key in {"vitest", "jest", "@playwright/test", "cypress"}:
                        stacks.append(key)
            except json.JSONDecodeError:
                summary["parseError"] = "invalid package.json"
        elif manifest.name == "pom.xml":
            summary["kind"] = "maven"
            if "spring-boot" in text:
                stacks.append("spring-boot")
            if "junit" in text or "spring-boot-starter-test" in text:
                stacks.append("junit")
        elif manifest.name.startswith("playwright.config"):
            summary["kind"] = "playwright"
            stacks.append("playwright")
        elif manifest.name.startswith("vite.config"):
            summary["kind"] = "vite"
            stacks.append("vite")
        elif manifest.name.startswith("next.config"):
            summary["kind"] = "next"
            stacks.append("next")
        elif manifest.name in {"pytest.ini", "pyproject.toml"}:
            stacks.append("python")
            if "pytest" in text:
                stacks.append("pytest")
        summaries.append(summary)
    return {"stacks": sorted(set(stacks)), "manifests": summaries}


def rel_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def list_existing_patterns(root: Path, patterns: list[str]) -> list[str]:
    files: list[str] = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                files.append(rel_path(path, root))
    return sorted(dict.fromkeys(files))


def package_script(root: Path, script: str) -> bool:
    package_json = root / "package.json"
    if not package_json.exists():
        return False
    try:
        data = json.loads(read_text(package_json, 20000))
    except json.JSONDecodeError:
        return False
    return script in data.get("scripts", {})


def command_join(parts: list[str]) -> str:
    return " ".join(parts)


def _detect_js_test_framework(proj: Path) -> str:
    """从 package.json 依赖判断前端测试框架。"""
    pkg = proj / "package.json"
    if not pkg.exists():
        return "unknown"
    try:
        data = json.loads(read_text(pkg, 20000))
    except Exception:
        return "unknown"
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    if "@playwright/test" in deps or "playwright" in deps:
        return "playwright"
    if "next" in deps:
        return "next"
    return "node:test"


def get_playwright_dir(repo: Path) -> Path:
    """读取 qa-agent.config.yaml 的 playwright.dir（默认 tests/），返回 Playwright 运行时的绝对路径。

    Playwright 运行时（package.json / node_modules / playwright.config.js）统一落在此目录，
    避免根目录 lockfile 干扰前端 Next.js 的 workspace root 推断（导致动态路由 404）。
    """
    dir_str = "tests/"
    config_path = repo / ".qa-agent" / "config" / "qa-agent.config.yaml"
    if config_path.exists():
        try:
            cfg = load_config(config_path)
            pw = cfg.get("playwright") or {}
            if isinstance(pw, dict) and pw.get("dir"):
                dir_str = str(pw["dir"])
        except Exception:
            pass
    return (repo / dir_str).resolve()


def _find_playwright_config(proj: Path) -> str:
    """查找 playwright.config 文件名（优先 proj 根目录，再递归子目录），未找到返回空串。"""
    for name in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs"):
        if (proj / name).exists():
            return name
    # Playwright 运行时可能位于 playwrightDir（如 tests/），递归查找兜底
    for path in find_files(proj, {"playwright.config.ts", "playwright.config.js", "playwright.config.mjs"}):
        return path.name
    return ""


def detect_project_test_profile(repo: Path) -> dict[str, Any]:
    """通用探测项目测试套件：扫描 pom.xml/package.json 子目录，识别前后端测试。"""
    suites: list[dict[str, Any]] = []
    commands: dict[str, list[str]] = {"unit": [], "api": [], "integration": [], "e2e": [], "review": []}

    for proj in _find_project_dirs(repo):
        rel = str(proj.relative_to(repo)).replace("\\", "/")
        sid = proj.name

        # 后端（pom.xml）
        if (proj / "pom.xml").exists():
            java_tests = list_existing_patterns(proj, ["src/test/java/**/*.java"])
            compile_cmd = f"cd {rel} && mvn -q -DskipTests compile"
            commands["api"].append(compile_cmd)
            test_cmd = f"cd {rel} && mvn -q test -DskipITs" if java_tests else None
            if test_cmd:
                commands["integration"].append(test_cmd)
            suites.append({
                "name": f"{sid}-junit",
                "root": rel,
                "framework": "maven/junit",
                "layer": "backend-unit+integration",
                "testDir": "src/test/java",
                "testFilesCount": len(java_tests),
                "compileCommand": compile_cmd,
                "testCommand": test_cmd,
                "targetedCommandPattern": f"cd {rel} && mvn -q -Dtest=<ClassName> test",
            })
            continue

        # 前端（package.json）
        if not (proj / "package.json").exists():
            continue
        unit_files = list_existing_patterns(proj, ["src/**/*.test.*", "src/**/*.spec.*", "tests/**/*.test.*", "tests/**/*.spec.*"])
        if package_script(proj, "test") and unit_files:
            cmd = f"cd {rel} && npm run test"
            commands["unit"].append(cmd)
            suites.append({
                "name": f"{sid}-unit",
                "root": rel,
                "framework": _detect_js_test_framework(proj),
                "layer": "frontend-unit",
                "testFiles": unit_files,
                "command": cmd,
            })
        pw_config = _find_playwright_config(proj)
        if pw_config:
            e2e_files = list_existing_patterns(proj, ["e2e/**/*.spec.*", "tests/e2e/**/*.spec.*"])
            list_cmd = f"cd {rel} && npx playwright test --list"
            commands["e2e"].append(list_cmd)
            suites.append({
                "name": f"{sid}-e2e",
                "root": rel,
                "framework": "playwright",
                "layer": "e2e",
                "config": pw_config,
                "testFiles": e2e_files,
                "listCommand": list_cmd,
                "runtimeCommand": f"cd {rel} && npx playwright test",
                "status": "ready" if e2e_files else "no-tests-found",
            })

    return {"suites": suites, "commands": commands}


def detect_base_branch(repo: Path) -> str:
    branches = git_output(repo, ["branch", "--list", "master"]).strip()
    if branches:
        return "master"
    return "main"


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_config_yaml(base_branch: str, commands: dict[str, list[str]]) -> str:
    lines = [
        'version: "1.0"',
        f"baseBranch: {yaml_quote(base_branch)}",
        'pythonEncoding: "utf-8"',
        "branch:",
        '  pattern: "feature/ming-qa-{slug}"',
        "llm:",
        '  baseUrlEnv: "QA_AGENT_LLM_BASE_URL"',
        '  apiKeyEnv: "QA_AGENT_LLM_API_KEY"',
        "  # 留空则不启用多模型交叉审查；填你自己的 OpenAI 兼容网关地址后生效",
        f"  defaultBaseUrl: {yaml_quote(DEFAULT_BASE_URL)}",
        "  models:",
    ]
    lines.extend(f"    - {yaml_quote(model)}" for model in DEFAULT_MODELS)
    lines.extend(
        [
            "  # 单模型请求超时（秒）",
            "  timeoutSeconds: 300",
            "  # 默认走流式（SSE）。流式是超集，且多数网关默认或只支持流式；",
            "  # 端点确实不支持时自动降级重试一次非流式，通常不需要改这里。",
            "  stream: true",
            "qualityGates:",
            "  maxRepairLoops: 5",
            "  coverage:",
            "    lines: 80",
            "    branches: 70",
            "    functions: 80",
            "    newCode: 90",
            "commands:",
        ]
    )
    for gate in ["unit", "api", "integration", "e2e", "review"]:
        gate_commands = commands.get(gate, [])
        if gate_commands:
            lines.append(f"  {gate}:")
            lines.extend(f"    - {yaml_quote(command)}" for command in gate_commands)
        else:
            lines.append(f"  {gate}: []")
    lines.extend(
        [
            "database:",
            "  mysqlMcp:",
            '    serverName: "mysql_mcp"',
            '    source: ".mcp.json"',
            "    requiredFor:",
            '      - "integration"',
            '      - "e2e"',
            "reports:",
            '  outputDir: ".qa-agent/reports"',
            "  keepHistory: true",
            "notify:",
            "  # 报告质量告警的 webhook 地址，留空则不发送任何通知（默认留空，不发数据）。",
            "  # 兼容 Slack / 飞书 / 钉钉 / 企业微信群机器人，使用通用 markdown POST。",
            '  webhook: ""',
            "",
        ]
    )
    return "\n".join(lines)


def _detect_adb_devices(repo: Path, timeout: int = 15) -> dict[str, Any]:
    """解析 `adb devices`，区分已授权 / 未授权 / 其他状态。

    未授权（手机上的「允许 USB 调试」没点）不能算作可用设备——这是使用者
    首次运行时最常卡住的地方，doctor 要能据此给出可操作的下一步。
    """
    adb = _find_adb()
    info: dict[str, Any] = {
        "available": False, "adbPath": "", "devices": [],
        "authorized": [], "unauthorized": [],
    }
    if not adb:
        return info
    info["available"] = True
    info["adbPath"] = adb
    # adb 会在 cwd 写 adb.log——刻意不在目标 repo 下跑，避免污染工作区
    result = run_cmd([adb, "devices"], Path(tempfile.gettempdir()), timeout=timeout)
    for line in (result.get("stdout") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info["devices"].append(serial)
        if state == "device":
            info["authorized"].append(serial)
        elif state == "unauthorized":
            info["unauthorized"].append(serial)
    return info


def _mobile_platform_tasks(repo: Path) -> list[dict[str, Any]]:
    """读取 current/test-spec-tasks.json，返回 platform == "android-real" 的 task。

    缺省 platform 视为 web（存量数据零迁移）。文件缺失或损坏时返回空列表——
    检测逻辑不能因为读不到文件就抛异常。
    """
    path = repo / ".qa-agent" / "current" / "test-spec-tasks.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return []
    return [
        task for task in tasks
        if isinstance(task, dict) and (task.get("platform") or "web") == "android-real"
    ]


def _mobile_mcp_registered(repo: Path) -> bool:
    """`.mcp.json` 里是否已注册 mobile-mcp。"""
    path = repo / ".mcp.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and "mobile-mcp" in servers


def should_check_mobile(repo: Path, require_flag: bool = False) -> bool:
    """是否进入移动端检测。

    只有项目确有 android-real task 时才检测——否则一个纯后端项目跑 doctor
    会因为「没插手机」而失败。这与 services 探活「被标记为 required 才阻断」
    是同一个模式。
    """
    return bool(require_flag) or bool(_mobile_platform_tasks(repo))


def _register_mobile_mcp(repo: Path, platform: str | None = None) -> dict[str, Any]:
    """把 mobile-mcp 写进 .mcp.json 的 mcpServers，保留已有 server。

    幂等：已注册则跳过。文件损坏时重建而非报错——安装流程不该被一个坏文件打断。
    Windows 上必须用 `cmd /c npx` 启动，否则 MCP 客户端找不到 npx
    （对照 .mcp.json 里 playwright-test 的写法）。
    """
    plat = platform or ("windows" if os.name == "nt" else sys.platform)
    path = repo / ".mcp.json"
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if "mobile-mcp" in servers:
        return {"status": "skipped", "path": str(path), "reason": "mobile-mcp 已注册"}
    if plat == "windows":
        servers["mobile-mcp"] = {
            "command": "cmd",
            "args": ["/c", "npx", "-y", "@mobilenext/mobile-mcp@latest"],
        }
    else:
        servers["mobile-mcp"] = {
            "command": "npx",
            "args": ["-y", "@mobilenext/mobile-mcp@latest"],
        }
    data["mcpServers"] = servers
    write_json(path, data)
    return {"status": "created", "path": str(path)}


def _adb_install_command(platform: str | None = None) -> list[str] | None:
    """按平台返回安装 Android Platform Tools 的命令；无可用包管理器时返回 None。

    platform 参数仅为可测性存在，缺省按当前宿主推断。
    Linux 用 `sudo -n`（非交互）：需要密码时立即失败，而不是挂住等输入。
    """
    plat = platform or ("windows" if os.name == "nt" else sys.platform)
    if plat == "windows":
        if shutil.which("winget"):
            return ["winget", "install", "--id", "Google.PlatformTools", "-e",
                    "--accept-source-agreements", "--accept-package-agreements"]
        return None
    if plat == "darwin":
        if shutil.which("brew"):
            return ["brew", "install", "--cask", "android-platform-tools"]
        return None
    if shutil.which("apt-get"):
        return ["sudo", "-n", "apt-get", "install", "-y", "android-tools-adb"]
    return None


PLATFORM_TOOLS_URLS = {
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}


def platform_tools_dir() -> Path:
    """ming-qa 自管的 platform-tools 目录（用户级缓存，不写入系统 PATH）。"""
    return Path.home() / ".ming-qa" / "platform-tools"


def _find_adb() -> str | None:
    """定位 adb：优先系统 PATH，其次 ming-qa 自管的下载目录。

    没装包管理器的环境（如 Windows 10 LTSC 没有 winget）只能走下载回退，
    而下载目录不在 PATH 里——这里必须显式认它，否则装完 doctor 仍报缺失。
    """
    found = shutil.which("adb")
    if found:
        return found
    managed = platform_tools_dir() / ("adb.exe" if os.name == "nt" else "adb")
    return str(managed) if managed.exists() else None


def _download_platform_tools(
    repo: Path,
    platform: str | None = None,
    timeout: int = 600,
    dry_run: bool = False,
) -> dict[str, Any]:
    """从 Google 官方 CDN 下载 platform-tools 并解压到用户缓存目录。

    仅作为包管理器不可用时的回退。下载源是硬编码的官方地址，不接受任意 URL。
    解压后落在 ~/.ming-qa/platform-tools/，**不修改系统 PATH**。
    """
    plat = platform or ("windows" if os.name == "nt" else sys.platform)
    url = PLATFORM_TOOLS_URLS.get(plat)
    if not url:
        return {"status": "manual",
                "reason": f"不支持的平台：{plat}，请手动安装 Android Platform Tools"}
    target_root = platform_tools_dir().parent
    if dry_run:
        return {"status": "dry-run", "command": f"download {url} -> {target_root}"}
    tmp_path: Path | None = None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = resp.read()
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        target_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp_path) as zf:
            zf.extractall(target_root)
    except Exception as exc:  # 网络、解压、权限都可能失败
        return {"status": "failed", "reason": f"下载或解压失败：{exc}", "url": url}
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    adb_path = platform_tools_dir() / ("adb.exe" if os.name == "nt" else "adb")
    return {"status": "passed", "path": str(adb_path), "url": url}


def _path_with_dir_added(current: str, directory: str) -> str:
    """把 directory 追加到 PATH 字符串末尾（幂等，顺带清掉空条目）。

    PATH 里因尾部分号产生的空条目会拖慢 Windows 的查找，顺手清掉。
    """
    parts = [p for p in (current or "").split(";") if p]
    if directory in parts:
        return current
    parts.append(directory)
    return ";".join(parts)


def _add_to_user_path(
    directory: Path, platform: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """把目录写进用户级 PATH（幂等，只动用户级，不碰系统级）。

    只实现 Windows（写 HKCU\\Environment）。macOS / Linux 返回 manual——
    改 shell 启动文件的侵入性更大，而且该改 bashrc / zshrc / profile 取决于
    用户的 shell，猜错的代价比让使用者自己加一行更高。
    """
    plat = platform or ("windows" if os.name == "nt" else sys.platform)
    target = str(directory)
    if plat != "windows":
        return {"status": "manual",
                "reason": f"非 Windows 平台，请自行把 {target} 加入 PATH"}
    if dry_run:
        return {"status": "dry-run", "command": f"append {target} to user PATH"}
    import winreg  # Windows-only，延迟导入避免其他平台报错

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
    if target in [p for p in (current or "").split(";") if p]:
        return {"status": "skipped", "reason": "已在用户 PATH 中", "path": target}
    new_value = _path_with_dir_added(current or "", target)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, new_value)
    return {"status": "passed", "path": target, "reason": "已写入用户 PATH，需重开终端才生效"}


def _adb_manual_steps(repo: Path) -> list[str]:
    """工具链装完后仍需人工完成的步骤。

    USB 调试授权是物理操作——必须在手机屏幕上点「允许」，任何工具都替代不了。
    显式报出来，而不是静默跳过让使用者自己撞墙。
    """
    info = _detect_adb_devices(repo)
    if not info["available"] or info["authorized"]:
        return []
    return [
        "在手机上开启：设置 → 关于手机 → 连点版本号 7 次 → 开发者选项 → USB 调试",
        "USB 连接电脑后，在手机屏幕上点「允许此调试计算机」（建议勾选一律允许）",
        "确认 `adb devices` 显示 <serial>  device",
    ]


def install_mobile_e2e_in_repo(
    repo: Path,
    *,
    timeout: int = 600,
    dry_run: bool = False,
    with_adb: bool = True,
    with_mobilewright: bool = False,
    add_to_path: bool = False,
) -> dict[str, Any]:
    """准备 Android 真机 E2E 的工具链（幂等）。

    做三件事：
      1. 注册 mobile-mcp 到 .mcp.json（保留已有 server）
      2. 安装 Android Platform Tools（with_adb）
      3. 安装 @mobilewright/test（with_mobilewright）

    无法自动化的部分（手机上的 USB 授权）在 manualSteps 里返回。
    """
    steps: list[dict[str, Any]] = []
    if dry_run:
        steps.append({"step": "mobile-mcp", "status": "dry-run",
                      "command": f"register mobile-mcp in {repo / '.mcp.json'}"})
    else:
        steps.append({"step": "mobile-mcp", **_register_mobile_mcp(repo)})

    adb_info = _detect_adb_devices(repo)
    if not with_adb:
        steps.append({"step": "adb", "status": "skipped", "reason": "未请求安装（--no-adb）"})
    elif adb_info["available"]:
        steps.append({"step": "adb", "status": "skipped", "reason": "adb 已可用",
                      "detail": adb_info["adbPath"]})
    else:
        cmd = _adb_install_command()
        if cmd is None:
            # 包管理器不可用 → 回退到 Google 官方下载
            # （Windows 10 LTSC 没有 winget 就是这种情形）
            steps.append({"step": "adb", **_download_platform_tools(repo, dry_run=dry_run)})
        elif dry_run:
            steps.append({"step": "adb", "status": "dry-run", "command": " ".join(cmd)})
        else:
            result = run_cmd(cmd, repo, timeout=timeout)
            steps.append({
                "step": "adb",
                "status": "passed" if result["exitCode"] == 0 else "failed",
                "command": " ".join(cmd),
                "result": result,
            })

    if with_mobilewright:
        npm = shutil.which("npm") or "npm"
        cmd = [npm, "install", "--save-dev", "@mobilewright/test"]
        if dry_run:
            steps.append({"step": "mobilewright", "status": "dry-run", "command": " ".join(cmd)})
        else:
            result = run_cmd(cmd, repo, timeout=timeout)
            steps.append({
                "step": "mobilewright",
                "status": "passed" if result["exitCode"] == 0 else "failed",
                "command": " ".join(cmd),
                "result": result,
            })

    found_adb = _find_adb()
    managed_adb = platform_tools_dir() / ("adb.exe" if os.name == "nt" else "adb")
    if add_to_path and found_adb and Path(found_adb) == managed_adb:
        steps.append({"step": "user-path",
                      **_add_to_user_path(platform_tools_dir(), dry_run=dry_run)})

    # adb 装在 ming-qa 自管目录时，CLI 自己能找到，但第三方工具（mobile-mcp）只认 PATH。
    # 不提示的话使用者会看到「检测全绿」却在 agent 里用不了——这个坑真实发生过。
    notes: list[str] = []
    if found_adb and not shutil.which("adb"):
        notes.append(
            f"adb 位于 {found_adb}，但不在系统 PATH 中。mobile-mcp 等第三方工具直接调用 adb，"
            "需要该目录在 PATH 里——用 --add-to-path 添加，或手动加入。"
            "注意：环境变量对已运行的进程不生效，改完必须重开会话。"
        )
    if add_to_path and not dry_run:
        notes.append(
            "如果之前运行过 mobile-mcp：mobilecli 的 daemon 可能在 adb 尚不可见时就已启动，"
            "并一直缓存着「零设备」——它不报错，只是静默返回空列表，光修 PATH 不会让它重扫。"
            "执行 `mobilecli daemon stop` 后再列一次设备即可。"
        )

    status = "failed" if any(s.get("status") == "failed" for s in steps) else "passed"
    return {"status": status, "steps": steps,
            "manualSteps": _adb_manual_steps(repo), "notes": notes}


def install_mobile_e2e(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    if not repo.exists():
        raise QaAgentError(f"Repo not found: {repo}")
    install = install_mobile_e2e_in_repo(
        repo,
        timeout=args.timeout,
        dry_run=bool(getattr(args, "dry_run", False)),
        with_adb=not bool(getattr(args, "no_adb", False)),
        with_mobilewright=bool(getattr(args, "with_mobilewright", False)),
        add_to_path=bool(getattr(args, "add_to_path", False)),
    )
    print(f"Android 真机 E2E 工具链：{install['status']}")
    for step in install["steps"]:
        line = f"  [{step['status']}] {step['step']}"
        if step.get("reason"):
            line += f" — {step['reason']}"
        if step.get("command"):
            line += f" — {step['command']}"
        if step.get("path"):
            line += f" — {step['path']}"
        if step.get("detail"):
            line += f" — {step['detail']}"
        print(line)
    for note in install.get("notes", []):
        print(f"  ! {note}")
    if install["manualSteps"]:
        print("")
        print("仍需人工完成（无法自动化）：")
        for item in install["manualSteps"]:
            print(f"  - {item}")
    if install["status"] != "passed":
        raise SystemExit(1)


def detect_playwright_assets(repo: Path) -> dict[str, Any]:
    agent_files = []
    agent_names = {
        "playwright-test-planner.md",
        "playwright-test-generator.md",
        "playwright-test-healer.md",
        "playwright_test_planner.toml",
        "playwright_test_generator.toml",
        "playwright_test_healer.toml",
    }
    for path in sorted(iter_project_files(repo)):
        rel_path = path.relative_to(repo)
        if path.name not in agent_names:
            continue
        agent_files.append(
            {
                "path": str(rel_path).replace("\\", "/"),
                "size": path.stat().st_size,
                "summary": first_heading_or_description(path),
            }
        )
    config_files = []
    for path in sorted(iter_project_files(repo)):
        rel_path = path.relative_to(repo)
        if not path.name.startswith("playwright") or ".config." not in path.name:
            continue
        config_files.append(
            {
                "path": str(rel_path).replace("\\", "/"),
                "size": path.stat().st_size,
                "hints": extract_playwright_config_hints(read_text(path, 12000)),
            }
        )
    spec_files = []
    plan_files = []
    spec_dirs = []
    for specs_dir in sorted(iter_project_dirs(repo, "specs")):
        rel_dir = specs_dir.relative_to(repo)
        if ignored_path(rel_dir):
            continue
        spec_dirs.append(str(rel_dir).replace("\\", "/"))
        for path in sorted(specs_dir.rglob("*")):
            rel_path = path.relative_to(repo)
            if ignored_path(rel_path) or not path.is_file():
                continue
            rel = str(rel_path).replace("\\", "/")
            if path.suffix in {".ts", ".js"} and path.name.endswith((".spec.ts", ".spec.js", ".setup.ts")):
                spec_files.append({"path": rel, "size": path.stat().st_size})
            elif path.suffix.lower() == ".md" and ".plan." in path.name:
                plan_files.append(
                    {"path": rel, "size": path.stat().st_size, "title": first_markdown_title(path)}
                )
    reports = []
    for report_dir in sorted(iter_project_dirs(repo, "playwright-report")):
        report = report_dir / "index.html"
        if not report.exists():
            continue
        rel_path = report.relative_to(repo)
        stat = report.stat()
        reports.append(
            {
                "path": str(rel_path).replace("\\", "/"),
                "size": stat.st_size,
                "modifiedAt": dt.datetime.fromtimestamp(stat.st_mtime, _CN_TZ)
                .replace(microsecond=0)
                .isoformat(),
            }
        )
    runtime = _detect_playwright_runtime(repo)
    return {
        "available": bool(agent_files or config_files or spec_files or reports),
        "agents": agent_files,
        "configs": config_files,
        "specs": {
            "dirs": spec_dirs,
            "count": len(spec_files),
            "sample": spec_files[:60],
            "plans": plan_files[:20],
        },
        "reports": reports[:10],
        "report": reports[0] if reports else None,
        "runtime": runtime,
    }


def _playwright_browser_cache_dirs() -> list[Path]:
    """Playwright 浏览器二进制缓存目录候选（跨平台，多层兜底，不依赖单一环境变量）。"""
    dirs: list[Path] = []

    def add(p: str | None) -> None:
        if p:
            dirs.append(Path(p) / "ms-playwright")

    add(os.environ.get("LOCALAPPDATA"))                                    # Windows: %LOCALAPPDATA%\ms-playwright
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        dirs.append(Path(user_profile) / "AppData" / "Local" / "ms-playwright")  # Windows 兜底
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if username:
        dirs.append(Path("C:/Users") / username / "AppData" / "Local" / "ms-playwright")  # 进一步兜底
    add(os.environ.get("XDG_CACHE_HOME"))                                  # Linux/Mac
    home = os.environ.get("HOME")
    if home:
        dirs.append(Path(home) / "Library" / "Caches" / "ms-playwright")  # macOS
        dirs.append(Path(home) / ".cache" / "ms-playwright")              # Linux
    try:  # 最后兜底：Path.home() 可能因 home 环境变量缺失抛 RuntimeError
        dirs.append(Path.home() / "AppData" / "Local" / "ms-playwright")
        dirs.append(Path.home() / "Library" / "Caches" / "ms-playwright")  # macOS
        dirs.append(Path.home() / ".cache" / "ms-playwright")
    except RuntimeError:
        pass
    return dirs


def _detect_playwright_browsers_via_cli(repo: Path, timeout: int = 30) -> list[str]:
    """用 playwright 自身列出已安装浏览器（跨平台可靠，正确处理 PLAYWRIGHT_BROWSERS_PATH）。

    手动猜缓存路径（_playwright_browser_cache_dirs）在环境变量缺失或自定义缓存路径时可能漏判，
    此时回退到 `playwright install --list`，由 playwright 自己定位浏览器缓存目录。
    """
    npx = find_npx()
    if not npx:
        return []
    result = run_cmd([npx, "playwright", "install", "--list"], repo, timeout=timeout)
    if result["exitCode"] != 0:
        return []
    text = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    browsers: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if "ms-playwright" not in line:
            continue
        if not any(line.lower().startswith(p) for p in ("c:", "/", "~")):
            continue
        name = line.rstrip("\\/").replace("\\", "/").rsplit("/", 1)[-1]
        if name.startswith(("chromium", "firefox", "webkit")):
            browsers.append(name)
    return sorted(set(browsers))


def _detect_playwright_runtime(repo: Path) -> dict[str, Any]:
    """检测 Playwright 运行时（L2 @playwright/test + L3 浏览器），与 agents 定义（L1）分离。

    L1 agents 定义（.claude/agents/playwright-test-*.md）只用于 planner/generator/healer
    生成/修复 spec；真正 `npx playwright test` 跑 spec 依赖 L2 运行时 + L3 浏览器二进制。
    """
    # L2：@playwright/test 是否在 package.json 声明
    declared = False
    declared_paths: list[str] = []
    for manifest in find_files(repo, {"package.json"}):
        try:
            pkg = json.loads(read_text(manifest, 20000))
        except (json.JSONDecodeError, OSError):
            continue
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "@playwright/test" in deps:
            declared = True
            declared_paths.append(str(manifest.relative_to(repo)).replace("\\", "/"))

    # L2：@playwright/test 是否已安装到 node_modules（node_modules 被 iter_* 忽略，需精确探测）
    candidate_dirs = [repo / "node_modules" / "@playwright" / "test"]
    for manifest in find_files(repo, {"package.json"}):
        candidate_dirs.append(manifest.parent / "node_modules" / "@playwright" / "test")
    installed_at = sorted(
        {str(d.relative_to(repo)).replace("\\", "/") for d in candidate_dirs if d.is_dir()}
    )
    installed = bool(installed_at)

    # L3：浏览器二进制缓存（ms-playwright）
    browsers: list[str] = []
    for cache_dir in _playwright_browser_cache_dirs():
        if not cache_dir.exists():
            continue
        for sub in sorted(cache_dir.iterdir()):
            if sub.is_dir() and (
                sub.name.startswith("chromium")
                or sub.name.startswith("firefox")
                or sub.name.startswith("webkit")
            ):
                browsers.append(sub.name)
    browsers = sorted(set(browsers))  # 去重：多个候选目录可能指向同一 ms-playwright 缓存
    # 手动路径没找到时，用 playwright 自身兜底（正确处理自定义缓存路径 / 跨平台差异）
    if not browsers:
        browsers = _detect_playwright_browsers_via_cli(repo)
    browsers_installed = bool(browsers)

    return {
        "declared": declared,
        "declaredPaths": declared_paths,
        "installed": installed,
        "installedAt": installed_at,
        "browsers": browsers,
        "browsersInstalled": browsers_installed,
    }


def required_playwright_agent_files(loop: str) -> set[str]:
    if loop == "codex":
        return {
            ".codex/agents/playwright_test_planner.toml",
            ".codex/agents/playwright_test_generator.toml",
            ".codex/agents/playwright_test_healer.toml",
        }
    if loop == AGENT_CLAUDE:
        return {
            ".claude/agents/playwright-test-planner.md",
            ".claude/agents/playwright-test-generator.md",
            ".claude/agents/playwright-test-healer.md",
        }
    return set()


def has_playwright_test_agents(playwright: dict[str, Any], loop: str | None = None) -> bool:
    agents = playwright.get("agents", [])
    if not loop:
        return bool(agents)
    required = required_playwright_agent_files(loop)
    if not required:
        return bool(agents)
    present = {agent.get("path") for agent in agents}
    return required.issubset(present)


def should_prepare_playwright_agents(repo: Path, playwright: dict[str, Any]) -> bool:
    if playwright.get("configs"):
        return True
    stacks = detect_stack(repo).get("stacks", [])
    if "playwright" in stacks or "@playwright/test" in stacks:
        return True
    specs = playwright.get("specs", {})
    return bool(specs.get("dirs") or specs.get("count"))


def find_npx() -> str | None:
    # On Windows, npx is commonly exposed as npx.cmd; subprocess cannot execute
    # the extensionless shim reliably without shell=True.
    candidates = ["npx.cmd", "npx.exe", "npx"] if os.name == "nt" else ["npx"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def find_npm() -> str | None:
    """定位 npm 可执行文件（Windows 下为 npm.cmd）。"""
    candidates = ["npm.cmd", "npm.exe", "npm"] if os.name == "nt" else ["npm"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def install_playwright_agents_in_repo(
    repo: Path,
    *,
    loop: str,
    timeout: int,
    skip_if_present: bool,
    dry_run: bool,
    no_yes: bool,
) -> dict[str, Any]:
    before = detect_playwright_assets(repo)
    if skip_if_present and has_playwright_test_agents(before, loop):
        return {
            "status": "skipped",
            "reason": "present",
            "before": before,
            "after": before,
            "command": None,
        }
    npx = find_npx()
    if not npx:
        raise QaAgentError("npx was not found in PATH; install Node.js/npm first")
    command = [npx]
    if not no_yes:
        command.append("-y")
    command.extend(["playwright", "init-agents", f"--loop={playwright_loop_name(loop)}"])
    if dry_run:
        return {
            "status": "dry-run",
            "before": before,
            "after": before,
            "command": " ".join(command),
        }
    # 备份 .mcp.json 的 mcpServers，避免 `npx playwright init-agents` 覆盖用户已有的 MCP 配置
    mcp_file = repo / ".mcp.json"
    mcp_servers_backup: dict[str, Any] = {}
    if mcp_file.exists():
        try:
            loaded = json.loads(mcp_file.read_text(encoding="utf-8"))
            if isinstance(loaded.get("mcpServers"), dict):
                mcp_servers_backup = loaded.get("mcpServers", {})
        except Exception:
            mcp_servers_backup = {}

    print(f"正在安装 Playwright Test Agents ({loop})...（可能需几十秒，请稍候）")
    result = run_cmd(command, repo, timeout=timeout)

    # init-agents 会写 .mcp.json，把备份的 mcpServers merge 回来（保留原有 server，只新增/更新 playwright 的）
    if mcp_file.exists() and mcp_servers_backup:
        try:
            new_mcp = json.loads(mcp_file.read_text(encoding="utf-8"))
            merged = dict(mcp_servers_backup)
            if isinstance(new_mcp.get("mcpServers"), dict):
                merged.update(new_mcp.get("mcpServers", {}))
            new_mcp["mcpServers"] = merged
            write_json(mcp_file, new_mcp)
        except Exception:
            pass

    after = detect_playwright_assets(repo)
    return {
        "status": "passed" if result["exitCode"] == 0 else "failed",
        "before": before,
        "after": after,
        "command": " ".join(command),
        "result": result,
    }


def install_playwright_agents(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    if not repo.exists():
        raise QaAgentError(f"Repo not found: {repo}")
    install = install_playwright_agents_in_repo(
        repo,
        loop=args.loop,
        timeout=args.timeout,
        skip_if_present=args.skip_if_present,
        dry_run=args.dry_run,
        no_yes=args.no_yes,
    )
    if install["status"] == "skipped":
        print(f"已检测到 Playwright Test Agents：{summarize_playwright(install['before'])}")
        return
    if install["status"] == "dry-run":
        print(install["command"])
        return
    result = install["result"]
    if result.get("stdout"):
        print(result["stdout"])
    if result.get("stderr"):
        print(result["stderr"], file=sys.stderr)
    if result["exitCode"] != 0:
        raise SystemExit(result["exitCode"])
    print(f"Playwright Test Agents 安装完成：{summarize_playwright(install['after'])}")


def _default_playwright_config_text() -> str:
    """默认 playwright.config.js 模板：testDir 约定 e2e（相对 playwrightDir），baseURL 用 env 占位（与 services 一致）。"""
    return (
        "const { defineConfig } = require('@playwright/test');\n"
        "\n"
        "module.exports = defineConfig({\n"
        "  testDir: './e2e',\n"
        "  timeout: 60_000,\n"
        "  retries: 0,\n"
        "  use: {\n"
        "    baseURL: process.env.QA_WEB_BASE_URL || 'http://127.0.0.1:3000',\n"
        "    headless: true,\n"
        "  },\n"
        "});\n"
    )


def install_playwright_runtime_in_repo(
    repo: Path,
    *,
    timeout: int,
    skip_if_present: bool,
    dry_run: bool,
    install_browsers: bool,
) -> dict[str, Any]:
    """安装 Playwright 运行时（L2 @playwright/test + L3 浏览器二进制）。

    与 install_playwright_agents_in_repo（只装 L1 agents 定义）互补。
    playwright.config 缺失时自动生成默认模板（testDir=./e2e，baseURL 用 env 占位）。
    运行时统一落在 playwrightDir（默认 tests/），避免根目录 lockfile 干扰前端 Next.js workspace root 推断。
    """
    pw_dir = get_playwright_dir(repo)
    pw_dir.mkdir(parents=True, exist_ok=True)
    before = detect_playwright_assets(repo)
    runtime = before.get("runtime") or {}
    config_present = bool(before.get("configs"))
    ready = bool(
        runtime.get("declared")
        and runtime.get("installed")
        and config_present
        and runtime.get("browsersInstalled")
    )
    if skip_if_present and ready:
        return {"status": "skipped", "reason": "present", "before": before, "after": before, "steps": []}

    npm = find_npm()
    if not npm:
        raise QaAgentError("npm was not found in PATH; install Node.js first")
    npx = find_npx()
    if not npx:
        raise QaAgentError("npx was not found in PATH; install Node.js/npm first")

    steps: list[dict[str, Any]] = []

    # 1. 安装 @playwright/test（声明 + 落盘 node_modules）
    if not (runtime.get("declared") and runtime.get("installed")):
        cmd = [npm, "install", "--save-dev", "@playwright/test"]
        if dry_run:
            steps.append({"step": "npm-install", "command": " ".join(cmd), "status": "dry-run"})
        else:
            print(f"正在安装 @playwright/test：{' '.join(cmd)}（可能需几十秒）")
            result = run_cmd(cmd, pw_dir, timeout=timeout)
            steps.append({
                "step": "npm-install",
                "command": " ".join(cmd),
                "status": "passed" if result["exitCode"] == 0 else "failed",
                "result": result,
            })

    # 2. 安装浏览器二进制（chromium）
    if install_browsers and not runtime.get("browsersInstalled"):
        cmd = [npx, "playwright", "install", "chromium"]
        if dry_run:
            steps.append({"step": "install-browsers", "command": " ".join(cmd), "status": "dry-run"})
        else:
            print(f"正在安装浏览器：{' '.join(cmd)}（可能需几十秒到几分钟）")
            result = run_cmd(cmd, pw_dir, timeout=timeout)
            steps.append({
                "step": "install-browsers",
                "command": " ".join(cmd),
                "status": "passed" if result["exitCode"] == 0 else "failed",
                "result": result,
            })

    # 3. 生成默认 playwright.config.js（缺失时；baseURL 用 env 占位，与 services 的 QA_WEB_BASE_URL 一致）
    if not config_present:
        config_path = pw_dir / "playwright.config.js"
        if dry_run:
            steps.append({"step": "write-config", "command": f"write {config_path.name}", "status": "dry-run"})
        else:
            try:
                config_path.write_text(_default_playwright_config_text(), encoding="utf-8")
                steps.append({"step": "write-config", "command": f"write {config_path.name}", "status": "passed"})
            except OSError as exc:
                steps.append({
                    "step": "write-config",
                    "command": f"write {config_path.name}",
                    "status": "failed",
                    "result": {"stderr": str(exc)},
                })

    after = detect_playwright_assets(repo)
    failed = any(s.get("status") == "failed" for s in steps)
    return {
        "status": "failed" if failed else "passed",
        "before": before,
        "after": after,
        "steps": steps,
    }


def install_playwright_runtime(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    if not repo.exists():
        raise QaAgentError(f"Repo not found: {repo}")
    install = install_playwright_runtime_in_repo(
        repo,
        timeout=args.timeout,
        skip_if_present=args.skip_if_present,
        dry_run=args.dry_run,
        install_browsers=not args.skip_browsers,
    )
    if install["status"] == "skipped":
        print(f"Playwright 运行时已就绪：{summarize_playwright(install['before'])}")
        return
    for step in install["steps"]:
        if step.get("status") == "dry-run":
            print(f"[dry-run] {step['command']}")
            continue
        result = step.get("result") or {}
        if result.get("stdout"):
            print(result["stdout"])
        if result.get("stderr"):
            print(result["stderr"], file=sys.stderr)
        if step.get("status") == "failed":
            print(f"[failed] {step['command']}", file=sys.stderr)
    if install["status"] == "failed":
        raise SystemExit(1)
    print(f"Playwright 运行时安装完成：{summarize_playwright(install['after'])}")


SENSITIVE_MCP_ARG_FLAGS = {"--password", "--user", "--username", "--host", "--token", "--api-key", "--apikey", "--secret"}
SENSITIVE_MCP_KEY_RE = re.compile(r"(password|passwd|pwd|secret|token|api[_-]?key|credential|username|user|host)", re.I)
MYSQL_MCP_SUCCESS_PATTERNS = ("Connected to MySQL database", "MySQL connection established successfully", "Server running")


def resolve_home_dir() -> Path:
    """跨平台稳健地定位用户家目录。

    Path.home() 在 Windows 上依赖 USERPROFILE / HOMEDRIVE+HOMEPATH / HOME，
    这些变量全部缺失时会抛 RuntimeError("Could not determine home directory.")。
    此处显式按优先级兜底，避免安装脚本在环境变量错位时崩溃。
    """
    try:
        return Path.home()
    except RuntimeError:
        for key in ("USERPROFILE", "HOME"):
            value = os.environ.get(key)
            if value:
                return Path(value)
        drive = os.environ.get("HOMEDRIVE")
        path = os.environ.get("HOMEPATH")
        if drive and path:
            return Path(drive + path)
        raise


def codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else resolve_home_dir() / ".codex") / "config.toml"


def load_project_mcp_config(repo: Path) -> dict[str, Any]:
    path = repo / ".mcp.json"
    if not path.exists():
        return {"path": str(path), "exists": False, "servers": {}}
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise QaAgentError(f"Failed to read {path}: {exc}") from exc
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
    return {"path": str(path), "exists": True, "servers": servers}


def sanitize_mcp_args(args: Any) -> Any:
    if not isinstance(args, list):
        return args
    redacted: list[Any] = []
    redact_next = False
    for item in args:
        text = str(item)
        flag = text.split("=", 1)[0]
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue
        if flag.lower() in SENSITIVE_MCP_ARG_FLAGS:
            if "=" in text:
                redacted.append(text.split("=", 1)[0] + "=***")
            else:
                redacted.append(text)
                redact_next = True
            continue
        redacted.append(item)
    return redacted


def sanitize_mcp_server(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "args":
                sanitized[key] = sanitize_mcp_args(item)
            elif SENSITIVE_MCP_KEY_RE.search(str(key)):
                sanitized[key] = "***"
            else:
                sanitized[key] = sanitize_mcp_server(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_mcp_server(item) for item in value]
    return value


def sensitive_mcp_values(server: dict[str, Any]) -> list[str]:
    values: list[str] = []
    args = server.get("args", [])
    if isinstance(args, list):
        redact_next = False
        for item in args:
            text = str(item)
            flag = text.split("=", 1)[0].lower()
            if redact_next:
                values.append(text)
                redact_next = False
                continue
            if flag in SENSITIVE_MCP_ARG_FLAGS:
                if "=" in text:
                    values.append(text.split("=", 1)[1])
                else:
                    redact_next = True
    def collect(obj: Any, key: str = "") -> None:
        if isinstance(obj, dict):
            for child_key, child_value in obj.items():
                if SENSITIVE_MCP_KEY_RE.search(str(child_key)) and isinstance(child_value, (str, int, float)):
                    values.append(str(child_value))
                else:
                    collect(child_value, str(child_key))
        elif isinstance(obj, list):
            for child in obj:
                collect(child, key)
    collect(server)
    return sorted({value for value in values if value})


def redact_mcp_text(text: str, server: dict[str, Any]) -> str:
    redacted = text or ""
    for value in sensitive_mcp_values(server):
        redacted = redacted.replace(value, "***")
    return redacted


def toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return json.dumps(key, ensure_ascii=False)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def render_mcp_server_toml(server_name: str, server: dict[str, Any]) -> str:
    lines = [f"[mcp_servers.{server_name}]"]
    for key, value in server.items():
        if key == "env" or isinstance(value, dict):
            continue
        lines.append(f"{toml_key(key)} = {toml_value(value)}")
    env = server.get("env")
    if isinstance(env, dict) and env:
        lines.append("")
        lines.append(f"[mcp_servers.{server_name}.env]")
        for key, value in env.items():
            lines.append(f"{toml_key(key)} = {toml_value(value)}")
    return "\n".join(lines).rstrip() + "\n"


def codex_config_has_mcp_server(config_path: Path, server_name: str) -> bool:
    if not config_path.exists():
        return False
    pattern = re.compile(rf"^\[mcp_servers\.{re.escape(server_name)}\]\s*$", re.M)
    return bool(pattern.search(config_path.read_text(encoding="utf-8", errors="replace")))


def upsert_codex_mcp_server(config_path: Path, server_name: str, server: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    before = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    block = render_mcp_server_toml(server_name, server)
    lines = before.splitlines()
    output: list[str] = []
    index = 0
    removed = False
    header_re = re.compile(rf"^\[mcp_servers\.{re.escape(server_name)}\]\s*$")
    any_header_re = re.compile(r"^\[[^\[][^\]]*\]\s*$")
    while index < len(lines):
        if header_re.match(lines[index].strip()):
            removed = True
            index += 1
            while index < len(lines) and not any_header_re.match(lines[index].strip()):
                index += 1
            continue
        output.append(lines[index])
        index += 1
    new_text = "\n".join(output).rstrip()
    if new_text:
        new_text += "\n\n"
    new_text += block
    if before and before.endswith("\n"):
        new_text = new_text.rstrip() + "\n"
    if dry_run:
        return {"status": "dry-run", "changed": before != new_text, "replaced": removed, "configPath": str(config_path)}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if config_path.exists() and before != new_text:
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = config_path.with_name(config_path.name + f".bak_{server_name}_{stamp}")
        backup_path.write_text(before, encoding="utf-8")
    if before != new_text:
        config_path.write_text(new_text, encoding="utf-8")
    return {
        "status": "passed",
        "changed": before != new_text,
        "replaced": removed,
        "configPath": str(config_path),
        "backupPath": str(backup_path) if backup_path else None,
    }


def mcp_command_for_runtime(server: dict[str, Any]) -> list[str]:
    command = str(server.get("command") or "").strip()
    if not command:
        raise QaAgentError("mysql_mcp command is empty in .mcp.json")
    executable = find_npx() if Path(command).name.lower() in {"npx", "npx.cmd", "npx.exe"} else shutil.which(command)
    executable = executable or command
    args = server.get("args", [])
    if not isinstance(args, list):
        raise QaAgentError("mysql_mcp args must be a JSON array")
    return [executable, *[str(arg) for arg in args]]


def verify_mcp_server_start(repo: Path, server: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    command = mcp_command_for_runtime(server)
    display_command = [Path(command[0]).name, *sanitize_mcp_args(command[1:])]
    start = time.time()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        command,
        cwd=str(repo),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    time.sleep(max(1, timeout))
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True)
        else:
            proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    combined = redact_mcp_text((stdout or "") + "\n" + (stderr or ""), server)
    ok = any(pattern in combined for pattern in MYSQL_MCP_SUCCESS_PATTERNS)
    return {
        "status": "passed" if ok else "blocked",
        "ok": ok,
        "command": " ".join(str(part) for part in display_command),
        "exitCode": proc.returncode,
        "durationSeconds": round(time.time() - start, 3),
        "stdout": redact_mcp_text(stdout or "", server)[-4000:],
        "stderr": redact_mcp_text(stderr or "", server)[-4000:],
        "matchedPatterns": [pattern for pattern in MYSQL_MCP_SUCCESS_PATTERNS if pattern in combined],
    }


# ── MySQL MCP 配置（@executeautomation/database-server 规范）──

def _read_env_mysql(repo: Path) -> dict[str, str]:
    """从 env.shared / local/.env 分层读取数据库配置。"""
    return {
        "QA_MYSQL_HOST": qa_env_value(repo, "QA_MYSQL_HOST"),
        "QA_MYSQL_PORT": qa_env_value(repo, "QA_MYSQL_PORT"),
        "QA_MYSQL_DATABASE": qa_env_value(repo, "QA_MYSQL_DATABASE"),
        "QA_MYSQL_USER": qa_env_value(repo, "QA_MYSQL_USER"),
        "QA_MYSQL_PASS": qa_env_value(repo, "QA_MYSQL_PASS"),
    }


def _build_mysql_mcp_entry(repo: Path) -> dict[str, Any] | None:
    """从 env 读取数据库配置，构建 mysql_mcp 条目；未配置 DB 返回 None。"""
    env_vars = _read_env_mysql(repo)
    host = env_vars.get("QA_MYSQL_HOST", "")
    user = env_vars.get("QA_MYSQL_USER", "")
    if not host or not user:
        return None
    password = env_vars.get("QA_MYSQL_PASS", "") or env_vars.get("QA_MYSQL_PASSWORD", "")
    port = env_vars.get("QA_MYSQL_PORT", "3306")
    db = env_vars.get("QA_MYSQL_DATABASE", "")
    return {
        "type": "stdio",
        "command": "npx",
        "args": [
            "-y", "@executeautomation/database-server",
            "--mysql",
            "--host", host,
            "--port", port,
            "--database", db,
            "--user", user,
            "--password", password,
        ],
    }


def upsert_mysql_mcp_entry(repo: Path) -> dict[str, Any]:
    """从 .env 读取数据库配置，写入/更新 .mcp.json 的 mysql_mcp server 条目。"""
    mcp_file = repo / ".mcp.json"
    result: dict[str, Any] = {"generated": False, "mcpPath": str(mcp_file)}
    new_entry = _build_mysql_mcp_entry(repo)
    if new_entry is None:
        result["reason"] = ".env 中缺少 QA_MYSQL_HOST 或 QA_MYSQL_USER，跳过"
        return result

    mcp = {}
    if mcp_file.exists():
        try:
            mcp = json.loads(mcp_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    servers = mcp.get("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}

    existing = servers.get("mysql_mcp", {})
    if existing == new_entry:
        result["reason"] = "mysql_mcp 条目已是最新，跳过"
        return result

    servers["mysql_mcp"] = new_entry
    mcp["mcpServers"] = servers
    write_json(mcp_file, mcp)
    result["generated"] = True
    return result


# ── Codex MCP 配置生成 ─────────────────────────────────────

def generate_codex_mcp_from_project(repo: Path) -> dict[str, Any]:
    """从 .mcp.json 读取 MCP server 配置，逐个 upsert 到项目级 .codex/config.toml。

    只增删改 .mcp.json 里出现的 server 段，保留 config.toml 中已有的其他 MCP 配置。
    """
    mcp_file = repo / ".mcp.json"
    codex_file = repo / ".codex" / "config.toml"
    result: dict[str, Any] = {"generated": False, "codexConfigPath": str(codex_file)}
    if not mcp_file.exists():
        result["reason"] = ".mcp.json 不存在"
        return result
    try:
        mcp = json.loads(mcp_file.read_text(encoding="utf-8"))
    except Exception:
        result["reason"] = ".mcp.json 解析失败"
        return result
    servers = mcp.get("mcpServers", {})
    if not isinstance(servers, dict) or not servers:
        result["reason"] = ".mcp.json 中无 mcpServers 配置"
        return result
    changed_any = False
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        install = upsert_codex_mcp_server(codex_file, name, cfg)
        if install.get("changed"):
            changed_any = True
    result["generated"] = changed_any
    if not changed_any:
        result["reason"] = "config.toml 中 MCP server 均无变化"
    return result


def generate_codex_mysql_mcp(repo: Path) -> dict[str, Any]:
    """从 env 读取数据库配置，upsert .codex/config.toml 的 mysql_mcp 条目（不覆盖其他 MCP）。"""
    codex_file = repo / ".codex" / "config.toml"
    result: dict[str, Any] = {"generated": False, "codexConfigPath": str(codex_file)}
    entry = _build_mysql_mcp_entry(repo)
    if entry is None:
        result["reason"] = "缺少 QA_MYSQL_HOST 或 QA_MYSQL_USER"
        return result
    install = upsert_codex_mcp_server(codex_file, "mysql_mcp", entry)
    result["generated"] = bool(install.get("changed"))
    result["install"] = install
    return result


# ── MySQL MCP 检测（支持 Claude Code / Codex）────────────────

def detect_mysql_mcp(repo: Path, *, server_name: str = "mysql_mcp", verify: bool = False,
                     timeout: int = 12) -> dict[str, Any]:
    project = load_project_mcp_config(repo)
    server = project.get("servers", {}).get(server_name) if isinstance(project.get("servers"), dict) else None
    # Claude Code 读 .mcp.json，Codex 读 .codex/config.toml，有其一即可
    claude_ok = bool(server)
    codex_path = repo / ".codex" / "config.toml"
    codex_ok = codex_path.exists()
    result: dict[str, Any] = {
        "serverName": server_name,
        "projectMcpPath": project["path"],
        "projectConfigured": claude_ok,
        "codexConfigPath": str(codex_path),
        "codexConfigured": codex_ok,
        "npx": find_npx() or None,
        "server": sanitize_mcp_server(server) if isinstance(server, dict) else None,
        "status": "passed" if (claude_ok or codex_ok) else "blocked",
    }
    if not server and not codex_ok:
        result["reason"] = "未检测到 .mcp.json mysql_mcp 配置或 .codex/config.toml"
        return result
    if isinstance(server, dict) and str(server.get("command", "")).lower().startswith("npx") and not find_npx():
        result["status"] = "blocked"
        result["reason"] = "npx not found"
    if verify:
        result["verify"] = verify_mcp_server_start(repo, server, timeout=timeout)
        if result["verify"].get("ok") and result.get("status") == "passed":
            result["status"] = "passed"
        elif not result["verify"].get("ok"):
            result["status"] = "blocked"
    return result


def install_mysql_mcp_in_codex(
    repo: Path,
    *,
    server_name: str = "mysql_mcp",
    dry_run: bool = False,
    verify: bool = False,
    timeout: int = 12,
) -> dict[str, Any]:
    project = load_project_mcp_config(repo)
    server = project.get("servers", {}).get(server_name) if isinstance(project.get("servers"), dict) else None
    if not isinstance(server, dict):
        raise QaAgentError(f"{project['path']} does not define mcpServers.{server_name}")
    install = upsert_codex_mcp_server(codex_config_path(), server_name, server, dry_run=dry_run)
    detected = detect_mysql_mcp(repo, server_name=server_name, verify=False, timeout=timeout)
    result = {
        "status": install["status"],
        "serverName": server_name,
        "projectMcpPath": project["path"],
        "server": sanitize_mcp_server(server),
        "install": install,
        "detected": detected,
    }
    if verify and not dry_run:
        result["verify"] = verify_mcp_server_start(repo, server, timeout=timeout)
        if not result["verify"].get("ok"):
            result["status"] = "blocked"
    return result


def install_mysql_mcp(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    if not repo.exists():
        raise QaAgentError(f"Repo not found: {repo}")
    # 1. 从 .env 生成凭证
    cred = upsert_mysql_mcp_entry(repo)
    if cred["generated"]:
        print(f"已生成 MySQL MCP 配置：{cred['mcpPath']}")
    elif cred.get("reason", ""):
        print(f"未生成 MySQL MCP 配置：{cred.get('reason')}")
    # 2. 从 .mcp.json 为 Codex 生成项目级配置
    codex = generate_codex_mcp_from_project(repo)
    if codex["generated"]:
        print(f"已为 Codex 生成 MCP 配置：{codex['codexConfigPath']}")
    if args.verify:
        mysql = detect_mysql_mcp(repo, server_name=args.server_name, verify=True, timeout=args.timeout)
        if mysql.get("verify", {}).get("ok"):
            print("MySQL MCP 连接验证通过")
        else:
            print(f"验证失败：{mysql.get('verify', {}).get('reason', mysql.get('reason', 'unknown'))}")


def configure_mysql_mcp(repo: Path, agent: str) -> dict[str, Any]:
    """按 agent 配置 mysql_mcp，返回结构化结果 {claude-code: 结果, codex: 结果}。

    agent: claude-code → 只 .mcp.json；codex → 只 .codex/config.toml；both → 两者。
    两边都是 upsert，不覆盖其他 MCP 配置。
    """
    agent = normalize_agent_name(agent)
    result: dict[str, Any] = {}
    if agent in {AGENT_CLAUDE, AGENT_BOTH}:
        result[AGENT_CLAUDE] = upsert_mysql_mcp_entry(repo)
    if agent in {"codex", "both"}:
        result[AGENT_CODEX] = generate_codex_mysql_mcp(repo)
    return result


def config_mysql_mcp(args: argparse.Namespace) -> None:
    """根据 .env 的 QA_AGENT 配置 mysql_mcp（claude→.mcp.json，codex→.codex/config.toml，both→两者）。"""
    repo = Path(args.repo).resolve()
    agent = normalize_agent_name(qa_env_value(repo, "QA_AGENT") or AGENT_CLAUDE)
    configured = configure_mysql_mcp(repo, agent)
    cred = configured.get(AGENT_CLAUDE)
    if cred is not None:
        if cred["generated"]:
            print(f"已生成 MySQL MCP 配置（Claude Code）：{cred['mcpPath']}")
        elif cred.get("reason"):
            print(f"跳过 MySQL MCP：{cred['reason']}")
    codex = configured.get(AGENT_CODEX)
    if codex is not None:
        if codex.get("generated"):
            print(f"已生成 MySQL MCP 配置（Codex）：{codex['codexConfigPath']}")
        elif codex.get("reason"):
            print(f"跳过 Codex MySQL MCP：{codex['reason']}")


def first_markdown_title(path: Path) -> str | None:
    for line in read_text(path, 4000).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def first_heading_or_description(path: Path) -> str | None:
    text = read_text(path, 5000)
    match = re.search(r"description:\s*['\"]?(.+?)['\"]?\s*$", text, re.M)
    if match:
        return match.group(1)[:240]
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def extract_playwright_config_hints(text: str) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    test_dir = re.search(r"testDir:\s*['\"]([^'\"]+)['\"]", text)
    reporter = re.search(r"reporter:\s*['\"]([^'\"]+)['\"]", text)
    workers = re.search(r"workers:\s*([^,\n]+)", text)
    timeout = re.search(r"timeout:\s*([^,\n]+)", text)
    if test_dir:
        hints["testDir"] = test_dir.group(1)
    if reporter:
        hints["reporter"] = reporter.group(1)
    if workers:
        hints["workers"] = workers.group(1).strip()
    if timeout:
        hints["timeout"] = timeout.group(1).strip()
    base_url = re.search(r"baseURL:\s*['\"]([^'\"]+)['\"]", text)
    if base_url:
        hints["baseURL"] = base_url.group(1)
    env_names = sorted(set(re.findall(r"process\.env\.([A-Z0-9_]+)", text)))
    if env_names:
        hints["env"] = env_names
    return hints


def collect_context(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        raise QaAgentError(f"Not a git repository: {repo}")
    output = Path(args.output).resolve()
    base = args.base or "main"
    scope = build_scope_context(repo, args.scope, base, args.module, args.commit)
    docs = find_files(
        repo,
        {"AGENTS.md", "CLAUDE.md", "README.md", "质量保证Agent.md"},
        suffixes=(".openapi.json", ".swagger.json"),
    )
    docs_payload = []
    for doc in docs[:40]:
        docs_payload.append(
            {
                "path": str(doc.relative_to(repo)).replace("\\", "/"),
                "content": read_text(doc, args.doc_limit),
            }
        )
    context = {
        "version": "1.0",
        "collectedAt": utc_now(),
        "repo": str(repo),
        "baseBranch": base,
        "scope": scope,
        "currentBranch": git_output(repo, ["branch", "--show-current"]).strip(),
        "gitStatus": git_output(repo, ["status", "--short", "--branch"]),
        "gitDiffStat": git_output(repo, ["diff", "--stat", base + "...HEAD"]),
        "gitDiffNameOnly": git_output(repo, ["diff", "--name-only", base + "...HEAD"]).splitlines(),
        "stack": detect_stack(repo),
        "testProfile": detect_project_test_profile(repo),
        "playwright": detect_playwright_assets(repo),
        "existingCaseIndex": summarize_existing_case_index(index_existing_cases_data(repo)),
        "documents": docs_payload,
    }
    write_json(output, context)
    print(f"已写入上下文：{output}")


def build_scope_context(
    repo: Path,
    scope: str,
    base: str,
    module: str | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    scope = (scope or "branch").lower()
    data: dict[str, Any] = {"mode": scope}
    if module:
        data["module"] = module
    if scope in {"requirement", "current-requirement"}:
        data["description"] = "Use active user requirement, linked docs, specs, related code, and current diff."
        data["gitDiff"] = git_output(repo, ["diff", "--stat", base + "...HEAD"])
        data["uncommittedDiff"] = git_output(repo, ["diff", "--stat"])
    elif scope in {"module", "current-module"}:
        data["description"] = "Use module-name search across docs, source, APIs, and tests."
        if module:
            data["matches"] = search_module_matches(repo, module)
    elif scope in {"uncommitted", "current-changes", "working-tree"}:
        data["description"] = "Use unstaged and staged working-tree changes."
        data["unstagedStat"] = git_output(repo, ["diff", "--stat"])
        data["unstagedFiles"] = git_output(repo, ["diff", "--name-only"]).splitlines()
        data["stagedStat"] = git_output(repo, ["diff", "--cached", "--stat"])
        data["stagedFiles"] = git_output(repo, ["diff", "--cached", "--name-only"]).splitlines()
    elif scope in {"latest-commit", "head"}:
        target = commit or "HEAD"
        data["description"] = "Use one commit as QA scope."
        data["commit"] = target
        data["showStat"] = git_output(repo, ["show", "--stat", "--oneline", target])
        data["files"] = git_output(repo, ["show", "--name-only", "--format=", target]).splitlines()
    elif scope in {"branch", "current-branch"}:
        data["description"] = "Use baseBranch...HEAD as QA scope."
        data["diffStat"] = git_output(repo, ["diff", "--stat", base + "...HEAD"])
        data["files"] = git_output(repo, ["diff", "--name-only", base + "...HEAD"]).splitlines()
        data["commits"] = git_output(repo, ["log", "--oneline", base + "..HEAD"]).splitlines()
    else:
        data["description"] = "Unknown scope; fallback to branch diff."
        data["mode"] = "branch"
        data["requestedMode"] = scope
        data["diffStat"] = git_output(repo, ["diff", "--stat", base + "...HEAD"])
        data["files"] = git_output(repo, ["diff", "--name-only", base + "...HEAD"]).splitlines()
    return data


def search_module_matches(repo: Path, module: str) -> dict[str, Any]:
    """用 ripgrep 按模块名抓代码位置。

    ripgrep 是硬依赖，但缺失时原先只是抛裸 traceback，没有任何一句说「需要装 rg」。
    另有一个更隐蔽的坑：Claude Code 环境下 `rg` 常是 **shell 函数**而非二进制
    （`which rg` 找不到、`type -a rg` 显示 function），而 subprocess 不走 shell 函数——
    「我以为装了 rg」完全不可靠。所以这里显式检查二进制是否存在。
    """
    if not shutil.which("rg"):
        raise QaAgentError(
            "未找到 ripgrep（rg）——收集上下文需要它来按模块名检索代码。\n"
            "  安装：winget install BurntSushi.ripgrep.MSVC（Windows）"
            "/ brew install ripgrep（macOS）/ apt install ripgrep（Debian/Ubuntu）\n"
            "  注意：Claude Code 里的 `rg` 可能是 shell 函数，那种 `rg` subprocess 用不了，"
            "必须是 PATH 里的真实二进制。"
        )
    result = run_cmd(
        ["rg", "-n", "--glob", "!node_modules", "--glob", "!.git", "--glob", "!target", module],
        repo,
        timeout=30,
    )
    matches = []
    for line in result.get("stdout", "").splitlines()[:200]:
        parts = line.split(":", 2)
        if len(parts) == 3:
            matches.append({"path": parts[0].replace("\\", "/"), "line": parts[1], "text": parts[2][:300]})
    return {"query": module, "countSampled": len(matches), "matches": matches}


def stable_text_key(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", "", value)
    return value.strip()


def case_match_key(case: dict[str, Any]) -> str:
    actor = stable_text_key(str(case.get("businessActor", "")))
    operation = stable_text_key(str(case.get("operationPath", "")))
    module = stable_text_key(str(case.get("module", "")))
    title = stable_text_key(str(case.get("title", "")))
    steps = stable_text_key(" ".join(map(str, case.get("steps", [])[:4])) if isinstance(case.get("steps"), list) else "")
    if operation:
        return "|".join(["case", module, actor, operation])
    return "|".join(["case", module, actor, title or steps])


def iter_existing_case_files(repo: Path) -> list[Path]:
    roots = [repo / ".qa-agent" / "cases", repo / ".qa-agent"]
    found: list[Path] = []
    skip_dirs = {"current", "runs", "reports", "tmp", "node_modules"}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            rel = path.relative_to(repo)
            if any(part in skip_dirs for part in rel.parts):
                continue
            name = path.name.lower()
            if "test-cases" in name or "cases" in rel.parts:
                found.append(path)
    return sorted(dict.fromkeys(found))


def iter_existing_spec_task_files(repo: Path) -> list[Path]:
    root = repo / ".qa-agent"
    if not root.exists():
        return []
    found: list[Path] = []
    skip_dirs = {"current", "runs", "reports", "tmp", "node_modules"}
    for path in root.rglob("*.json"):
        rel = path.relative_to(repo)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if "spec-task" in path.name.lower() or "spec-tasks" in rel.parts:
            found.append(path)
    return sorted(dict.fromkeys(found))


def load_cases_from_file(path: Path, repo: Path) -> list[dict[str, Any]]:
    try:
        data = read_json(path)
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        return []
    _, normalized = validate_cases_data(data, mutate=True)
    cases = []
    for case in normalized.get("cases", []):
        item = dict(case)
        item["_originFile"] = rel_path(path, repo)
        item["_matchKey"] = case_match_key(item)
        cases.append(item)
    return cases


def load_spec_tasks_from_file(path: Path, repo: Path) -> list[dict[str, Any]]:
    try:
        data = read_json(path)
    except Exception:
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return []
    result = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        item = dict(task)
        item["_originFile"] = rel_path(path, repo)
        result.append(item)
    return result


def extract_test_symbols(path: Path) -> list[str]:
    text = read_text(path, 20000)
    symbols: list[str] = []
    for pattern in [
        r"\b(?:it|test)\s*\(\s*['\"]([^'\"]+)['\"]",
        r"\bdescribe\s*\(\s*['\"]([^'\"]+)['\"]",
        r"@Test\s+(?:public\s+|private\s+|protected\s+)?(?:void|[A-Za-z0-9_<>, ?]+)\s+([A-Za-z0-9_]+)\s*\(",
        r"\bvoid\s+([A-Za-z0-9_]+)\s*\(\s*\)\s*\{",
    ]:
        for match in re.finditer(pattern, text):
            symbols.append(match.group(1)[:180])
    return sorted(dict.fromkeys(symbols))[:80]


def existing_test_files_from_profile(repo: Path) -> list[dict[str, Any]]:
    profile = detect_project_test_profile(repo)
    files: list[dict[str, Any]] = []
    for suite in profile.get("suites", []):
        root = repo / str(suite.get("root", ""))
        for rel in suite.get("testFiles", []) if isinstance(suite.get("testFiles"), list) else []:
            path = root / rel
            files.append(
                {
                    "suite": suite.get("name", ""),
                    "framework": suite.get("framework", ""),
                    "layer": suite.get("layer", ""),
                    "path": rel_path(path, repo) if path.exists() else str(Path(str(suite.get("root", ""))) / rel).replace("\\", "/"),
                    "symbols": extract_test_symbols(path) if path.exists() else [],
                    "command": suite.get("command") or suite.get("runtimeCommand") or suite.get("testCommand") or "",
                }
            )
        test_dir = suite.get("testDir")
        if test_dir and not suite.get("testFiles"):
            full_dir = root / str(test_dir)
            for path in sorted(full_dir.rglob("*"))[:400] if full_dir.exists() else []:
                if path.is_file() and path.suffix in {".java", ".js", ".ts", ".mjs", ".tsx", ".jsx"}:
                    files.append(
                        {
                            "suite": suite.get("name", ""),
                            "framework": suite.get("framework", ""),
                            "layer": suite.get("layer", ""),
                            "path": rel_path(path, repo),
                            "symbols": extract_test_symbols(path),
                            "command": suite.get("testCommand") or suite.get("command") or "",
                        }
                    )
    return files


def index_existing_cases_data(repo: Path) -> dict[str, Any]:
    case_files = iter_existing_case_files(repo)
    spec_task_files = iter_existing_spec_task_files(repo)
    cases: list[dict[str, Any]] = []
    for path in case_files:
        cases.extend(load_cases_from_file(path, repo))
    spec_tasks: list[dict[str, Any]] = []
    for path in spec_task_files:
        spec_tasks.extend(load_spec_tasks_from_file(path, repo))
    test_files = existing_test_files_from_profile(repo)
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "repo": str(repo),
        "summary": {
            "caseFiles": len(case_files),
            "cases": len(cases),
            "specTaskFiles": len(spec_task_files),
            "specTasks": len(spec_tasks),
            "testFiles": len(test_files),
        },
        "caseFiles": [rel_path(path, repo) for path in case_files],
        "cases": cases,
        "specTaskFiles": [rel_path(path, repo) for path in spec_task_files],
        "specTasks": spec_tasks,
        "testFiles": test_files,
    }


def summarize_existing_case_index(index: dict[str, Any]) -> dict[str, Any]:
    return {
        "generatedAt": index.get("generatedAt"),
        "summary": index.get("summary", {}),
        "caseFiles": index.get("caseFiles", [])[:40],
        "specTaskFiles": index.get("specTaskFiles", [])[:40],
        "testFilesSample": index.get("testFiles", [])[:40],
    }


def index_existing_cases(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    index = index_existing_cases_data(repo)
    if args.output:
        write_json(Path(args.output).resolve(), index)
    print(
        "Existing case index: {cases} cases, {specTasks} spec tasks, {testFiles} test files".format(
            **index["summary"]
        )
    )
    for path in index.get("caseFiles", [])[:20]:
        print(f"- case file: {path}")
    for item in index.get("testFiles", [])[:20]:
        print(f"- test file: {item.get('path')} ({item.get('framework')})")


def merge_existing_cases_data(generated_data: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    errors, generated = validate_cases_data(generated_data, mutate=True)
    blocking = [error for error in errors if "technical quality gate" not in error]
    if blocking:
        raise QaAgentError("Invalid generated cases: " + "; ".join(blocking))
    existing_by_key: dict[str, dict[str, Any]] = {}
    for existing in index.get("cases", []):
        if not isinstance(existing, dict):
            continue
        key = existing.get("_matchKey") or case_match_key(existing)
        if key and key not in existing_by_key:
            existing_by_key[key] = existing
    merged_cases: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for case in generated.get("cases", []):
        key = case_match_key(case)
        matched = existing_by_key.get(key)
        if matched:
            clean = {k: v for k, v in matched.items() if not k.startswith("_")}
            clean.setdefault("qaReuse", {})
            if isinstance(clean["qaReuse"], dict):
                clean["qaReuse"].update(
                    {
                        "matchedGeneratedCaseId": case.get("id"),
                        "originFile": matched.get("_originFile"),
                        "matchKey": key,
                        "action": "reuse-existing",
                    }
                )
            merged_cases.append(clean)
            reused.append({"generatedCaseId": case.get("id"), "existingCaseId": matched.get("id"), "originFile": matched.get("_originFile")})
            seen_ids.add(str(clean.get("id", "")))
        else:
            clean = dict(case)
            clean.setdefault("qaReuse", {"action": "new-gap-fill", "matchKey": key})
            if str(clean.get("id", "")) in seen_ids:
                clean["id"] = f"{clean.get('id')}-NEW"
            merged_cases.append(clean)
            added.append({"caseId": clean.get("id"), "title": clean.get("title")})
            seen_ids.add(str(clean.get("id", "")))
    output = dict(generated)
    output["cases"] = merged_cases
    output.setdefault("metadata", {})
    output["metadata"]["existingCaseReuse"] = {
        "generatedAt": utc_now(),
        "existingSummary": index.get("summary", {}),
        "reused": len(reused),
        "added": len(added),
    }
    output["reuseReport"] = {"reused": reused, "added": added}
    return output


def merge_existing_cases(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    generated = read_json(Path(args.generated).resolve())
    index = read_json(Path(args.existing_index).resolve()) if args.existing_index else index_existing_cases_data(repo)
    merged = merge_existing_cases_data(generated, index)
    write_json(Path(args.output).resolve(), merged)
    report = merged.get("reuseReport", {})
    print(
        f"已写入合并后的测试用例：{Path(args.output).resolve()} "
        f"(reused={len(report.get('reused', []))}, added={len(report.get('added', []))})"
    )


def normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(case)
    normalized["priority"] = str(normalized.get("priority", "P2")).upper()
    normalized["layer"] = normalized.get("layer") or infer_layer(normalized)
    normalized["automation"] = normalized.get("automation") or "candidate"
    normalized["status"] = normalized.get("status") or "draft"
    for key in ["source", "preconditions", "steps", "expected", "data", "tags"]:
        value = normalized.get(key)
        if value is None:
            normalized[key] = []
        elif isinstance(value, str):
            normalized[key] = [value]
    return normalized


def flatten_narrative_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(flatten_narrative_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_narrative_text(item) for item in value.values())
    return str(value)


def narrative_letter_count(text: str) -> int:
    return len(CHINESE_TEXT_RE.findall(text)) + len(ASCII_LETTER_RE.findall(text))


def needs_chinese_narrative_warning(text: str, *, minimum_letters: int = 24) -> bool:
    cleaned = text.strip()
    if narrative_letter_count(cleaned) < minimum_letters:
        return False
    return not CHINESE_TEXT_RE.search(cleaned)


def case_language_warnings(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    requirement = flatten_narrative_text(metadata.get("requirement"))
    if needs_chinese_narrative_warning(requirement):
        warnings.append("metadata.requirement \u5efa\u8bae\u4f7f\u7528\u4e2d\u6587\u63cf\u8ff0\uff0c\u4ea7\u54c1\u672f\u8bed\u548c\u6280\u672f\u6807\u8bc6\u53ef\u4fdd\u7559\u539f\u6587")
    for field in ROOT_NARRATIVE_FIELDS:
        value = flatten_narrative_text(data.get(field))
        if needs_chinese_narrative_warning(value):
            warnings.append(f"{field} \u5efa\u8bae\u4f7f\u7528\u4e2d\u6587\u63cf\u8ff0\uff0c\u4ea7\u54c1\u672f\u8bed\u548c\u6280\u672f\u6807\u8bc6\u53ef\u4fdd\u7559\u539f\u6587")
    cases = data.get("cases") if isinstance(data.get("cases"), list) else []
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        cid = str(case.get("id") or f"cases[{idx}]")
        narrative = " ".join(flatten_narrative_text(case.get(field)) for field in NARRATIVE_CASE_FIELDS)
        if needs_chinese_narrative_warning(narrative):
            warnings.append(
                f"{cid} \u6d4b\u8bd5\u7528\u4f8b\u53d9\u8ff0\u5efa\u8bae\u4f7f\u7528\u4e2d\u6587\uff1a"
                "title/preconditions/steps/expected/business*/risk \u7b49\u5b57\u6bb5\u4e0d\u5e94\u662f\u5168\u82f1\u6587\u53d9\u8ff0"
            )
    return warnings


def print_language_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"警告：{warning}", file=sys.stderr)


def looks_like_quality_gate_case(case: dict[str, Any]) -> bool:
    """Detect tooling/readiness checks that should not be business cases."""
    title = str(case.get("title", "")).lower()
    module = str(case.get("module", "")).lower()
    tags = " ".join(map(str, case.get("tags", []))).lower()
    text = " ".join([title, module, tags])
    has_technical_token = any(token.lower() in text for token in TECHNICAL_CASE_TOKENS)
    if not has_technical_token:
        return False
    explicit_business = any(
        case.get(key)
        for key in [
            "businessActor",
            "operationPath",
            "businessAction",
            "businessAssertions",
            "businessStateBefore",
            "businessStateAfter",
        ]
    )
    if explicit_business:
        return False
    if any(
        token in text
        for token in [
            "环境阻塞",
            "运行环境阻塞",
            "上下文采集",
            "qa agent",
            "smoke",
            "playwright test agents",
        ]
    ):
        return True
    return not any(hint.lower() in text for hint in BUSINESS_CASE_HINTS)


def technical_case_kind(case: dict[str, Any]) -> str:
    text = " ".join(
        [
            str(case.get("title", "")),
            str(case.get("module", "")),
            " ".join(map(str, case.get("tags", []))),
        ]
    ).lower()
    if any(token in text for token in ["install", "安装", "environment", "环境", "playwright test agents", "browser"]):
        return "environment"
    return "qualityGate"


def command_from_case(case: dict[str, Any]) -> str:
    result = case.get("result") if isinstance(case.get("result"), dict) else {}
    if result.get("command"):
        return str(result["command"])
    implementation = case.get("implementation") if isinstance(case.get("implementation"), dict) else {}
    commands = implementation.get("commands") if isinstance(implementation.get("commands"), list) else []
    return str(commands[0]) if commands else ""


def technical_case_to_quality_gate(case: dict[str, Any]) -> dict[str, Any]:
    result = case.get("result") if isinstance(case.get("result"), dict) else {}
    implementation = case.get("implementation") if isinstance(case.get("implementation"), dict) else {}
    artifacts = []
    if result.get("artifact"):
        artifacts.append(result["artifact"])
    artifacts.extend(implementation.get("artifacts", []) if isinstance(implementation.get("artifacts"), list) else [])
    return {
        "id": f"QG-{case.get('id', 'UNKNOWN')}",
        "name": case.get("title", ""),
        "category": case.get("layer", "integration"),
        "command": command_from_case(case),
        "status": case.get("status", "blocked"),
        "summary": result.get("summary") or "; ".join(map(str, case.get("expected", [])[:2])),
        "artifacts": artifacts,
        "sourceCaseId": case.get("id", ""),
    }


def technical_case_to_environment_check(case: dict[str, Any]) -> dict[str, Any]:
    result = case.get("result") if isinstance(case.get("result"), dict) else {}
    return {
        "id": f"ENV-{case.get('id', 'UNKNOWN')}",
        "name": case.get("title", ""),
        "status": case.get("status", "blocked"),
        "detail": result.get("summary") or "; ".join(map(str, case.get("expected", [])[:2])),
        "requiredFor": [case.get("layer", "unknown")],
        "sourceCaseId": case.get("id", ""),
    }


def infer_layer(case: dict[str, Any]) -> str:
    text = " ".join(
        str(case.get(key, ""))
        for key in ["title", "type", "module", "risk", "tags", "owner"]
    ).lower()
    if any(token in text for token in ["e2e", "ui flow", "用户流程", "playwright", "browser"]):
        return "e2e"
    if any(token in text for token in ["api", "接口", "endpoint", "contract", "http"]):
        return "api"
    if any(token in text for token in ["integration", "集成", "database", "db", "queue"]):
        return "integration"
    if any(token in text for token in ["frontend", "react", "vue", "组件", "hook", "composable"]):
        return "frontend-unit"
    if any(token in text for token in ["backend", "service", "junit", "算法", "状态机"]):
        return "backend-unit"
    return "unknown"


def validate_cases_data(data: dict[str, Any], mutate: bool = False) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["root must be an object"], data
    cases = data.get("cases")
    if not isinstance(cases, list):
        errors.append("root.cases must be an array")
        cases = []
    seen: set[str] = set()
    normalized_cases: list[dict[str, Any]] = []
    for idx, raw_case in enumerate(cases):
        if not isinstance(raw_case, dict):
            errors.append(f"cases[{idx}] must be an object")
            continue
        case = normalize_case(raw_case)
        cid = str(case.get("id", "")).strip()
        if not cid:
            errors.append(f"cases[{idx}].id is required")
        elif cid in seen:
            errors.append(f"duplicate case id: {cid}")
        seen.add(cid)
        for field in ["title", "module", "type"]:
            if not str(case.get(field, "")).strip():
                errors.append(f"{cid or 'cases[' + str(idx) + ']'} missing {field}")
        if case.get("priority") not in CASE_PRIORITIES:
            errors.append(f"{cid} invalid priority: {case.get('priority')}")
        if case.get("layer") not in CASE_LAYERS:
            errors.append(f"{cid} invalid layer: {case.get('layer')}")
        if case.get("automation") not in CASE_AUTOMATION:
            errors.append(f"{cid} invalid automation: {case.get('automation')}")
        if case.get("status") not in CASE_STATUS:
            errors.append(f"{cid} invalid status: {case.get('status')}")
        if not case.get("source"):
            if case.get("status") in {"confirmed", "passed", "skipped", "blocked"}:
                case["source"] = ["cases/*.json (promoted from prior run)"]
            else:
                errors.append(f"{cid} must include at least one source")
        if case.get("automation") in {"automated", "candidate"} and not case.get("expected"):
            if case.get("status") in {"confirmed", "passed", "skipped", "blocked"}:
                case["expected"] = []
            else:
                errors.append(f"{cid} automated/candidate case needs expected assertions")
        if looks_like_quality_gate_case(case):
            errors.append(
                f"{cid} looks like a technical quality gate; move it to qualityGates/environmentChecks "
                "and replace it with a business operation-path case"
            )
        if not case.get("operationPath") and not case.get("steps"):
            if case.get("status") in {"confirmed", "passed", "skipped", "blocked"}:
                case["operationPath"] = f"参见 spec-task（{cid}，promoted from prior run）"
            else:
                errors.append(f"{cid} must include an operationPath or business steps")
        normalized_cases.append(case)
    if mutate:
        data["cases"] = normalized_cases
        data.setdefault("version", "1.0")
        data.setdefault("metadata", {})
        data.setdefault("assumptions", [])
        data.setdefault("openQuestions", [])
        data.setdefault("environmentChecks", [])
        data.setdefault("qualityGates", [])
    return errors, data


def validate_cases(args: argparse.Namespace) -> None:
    path = Path(args.cases).resolve()
    data = read_json(path)
    errors, normalized = validate_cases_data(data, mutate=args.normalize)
    if args.normalize:
        write_json(path, normalized)
    if getattr(args, "check_mojibake", False):
        mojibake = scan_mojibake_paths([path])
        if mojibake["status"] != "passed":
            print_mojibake_summary(mojibake)
            errors.append(f"{path} contains mojibake/unreadable text")
    language_warnings = case_language_warnings(normalized)
    if language_warnings:
        print_language_warnings(language_warnings)
        if getattr(args, "strict_language", False):
            errors.extend(f"language policy: {warning}" for warning in language_warnings)
    if errors:
        for error in errors:
            print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"校验通过：{path}（{len(normalized.get('cases', []))} 个用例）")
    if getattr(args, "summary", False):
        print_case_summary(normalized)


def promote_cases(args: argparse.Namespace) -> None:
    source = Path(args.cases).resolve()
    data = read_json(source)
    errors, normalized = validate_cases_data(data, mutate=True)
    blocking = [error for error in errors if "technical quality gate" not in error]
    if blocking:
        raise QaAgentError("Invalid cases: " + "; ".join(blocking))
    metadata = normalized.setdefault("metadata", {})
    case_modules = [
        str(case.get("module", "")).strip()
        for case in normalized.get("cases", [])
        if str(case.get("module", "")).strip()
    ]
    name = args.module or metadata.get("module") or metadata.get("requirement") or (case_modules[0] if case_modules else "confirmed-cases")
    output = Path(args.output).resolve() if args.output else Path(args.repo).resolve() / ".qa-agent" / "cases" / f"{spec_slug(str(name))}.json"
    metadata["promotedAt"] = utc_now()
    metadata["promotedFrom"] = str(source)
    metadata["artifactClass"] = "tracked-business-cases"
    # 固化到长期 knowledge base 的用例统一标记为 confirmed，作为明确状态标记
    for case in normalized.get("cases", []):
        case["status"] = "confirmed"
    write_json(output, normalized)
    print(f"已沉淀确认后的业务用例：{output}（{len(normalized.get('cases', []))} 个用例）")
    repo = Path(args.repo).resolve()
    update_manifest(
        repo,
        stage="testcase-designer",
        artifact={"confirmed-cases": _repo_rel(repo, output)},
        status={"casesConfirmed": "done"},
    )


def print_case_summary(data: dict[str, Any]) -> None:
    _, normalized = validate_cases_data(data, mutate=True)
    cases = normalized.get("cases", [])
    if not cases:
        print("没有测试用例。")
        return
    print("\n测试用例摘要：")
    print("| ID | 优先级 | 层级 | 模块 | 状态 | 标题 |")
    print("|---|---|---|---|---|---|")
    for case in cases:
        print(
            "| {id} | {priority} | {layer} | {module} | {status} | {title} |".format(
                id=str(case.get("id", "")),
                priority=str(case.get("priority", "")),
                layer=str(case.get("layer", "")),
                module=str(case.get("module", "")),
                status=str(case.get("status", "")),
                title=str(case.get("title", "")).replace("|", "\\|"),
            )
        )


def summarize_cases(args: argparse.Namespace) -> None:
    print_case_summary(read_json(Path(args.cases).resolve()))


def classify_cases(data: dict[str, Any]) -> dict[str, Any]:
    _, normalized = validate_cases_data(data, mutate=True)
    groups = {layer: [] for layer in sorted(CASE_LAYERS)}
    for case in normalized.get("cases", []):
        layer = case.get("layer") if case.get("layer") in CASE_LAYERS else infer_layer(case)
        if layer == "unknown":
            layer = infer_layer(case)
        case["layer"] = layer
        groups.setdefault(layer, []).append(case["id"])
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "summary": {layer: len(ids) for layer, ids in groups.items() if ids},
        "groups": {layer: ids for layer, ids in groups.items() if ids},
        "cases": normalized.get("cases", []),
    }


def split_plan(args: argparse.Namespace) -> None:
    data = read_json(Path(args.cases).resolve())
    plan = classify_cases(data)
    write_json(Path(args.output).resolve(), plan)
    print(f"已写入拆分计划：{Path(args.output).resolve()}")


def spec_slug(value: str, max_len: int = 48) -> str:
    slug = slugify(value)
    return slug[:max_len].strip("-") or "business-flow"


def parse_ratio(value: str | None) -> dict[str, float]:
    if not value:
        return dict(DEFAULT_SPEC_TASK_TARGET_RATIO)
    ratio = dict(DEFAULT_SPEC_TASK_TARGET_RATIO)
    for part in value.split(","):
        if not part.strip():
            continue
        key, _, raw = part.partition("=")
        key = key.strip()
        if key not in SPEC_TASK_LAYERS or not raw.strip():
            raise QaAgentError(f"Invalid ratio item: {part}")
        ratio[key] = float(raw)
    total = sum(ratio.values())
    if total <= 0:
        raise QaAgentError("coverage ratio total must be positive")
    return {key: value / total for key, value in ratio.items()}


def spec_task_minimum(priority: str, override: str | None = None) -> int:
    minimums = dict(DEFAULT_SPEC_TASK_MIN_BY_PRIORITY)
    if override:
        for part in override.split(","):
            if not part.strip():
                continue
            key, _, raw = part.partition("=")
            key = key.strip().upper()
            if key not in CASE_PRIORITIES or not raw.strip():
                raise QaAgentError(f"Invalid min-specs item: {part}")
            minimums[key] = int(raw)
    return minimums.get(priority, 2)


def distribute_spec_task_layers(total: int, ratio: dict[str, float], max_e2e: int = 2) -> list[str]:
    if total <= 0:
        return []
    layers = ["unit", "integration", "api", "e2e"]
    counts = {layer: int(total * ratio.get(layer, 0)) for layer in layers}
    for layer in layers:
        if ratio.get(layer, 0) > 0 and total >= len(layers):
            counts[layer] = max(counts[layer], 1)
    counts["e2e"] = min(counts.get("e2e", 0), max_e2e)
    while sum(counts.values()) < total:
        candidates = sorted(
            layers,
            key=lambda layer: (counts[layer] / max(ratio.get(layer, 0.01), 0.01), layers.index(layer)),
        )
        for layer in candidates:
            if layer == "e2e" and counts[layer] >= max_e2e:
                continue
            counts[layer] += 1
            break
    while sum(counts.values()) > total:
        for layer in reversed(layers):
            if counts[layer] > 0:
                counts[layer] -= 1
                break
    ordered: list[str] = []
    for layer in ["unit", "integration", "api", "e2e"]:
        ordered.extend([layer] * counts[layer])
    return ordered


def case_text(case: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ["title", "module", "type", "businessActor", "operationPath", "risk"]:
        if case.get(key):
            parts.append(str(case.get(key)))
    for key in ["steps", "expected", "tags"]:
        value = case.get(key)
        if isinstance(value, list):
            parts.extend(map(str, value))
    return " ".join(parts).lower()


def spec_task_focuses(case: dict[str, Any]) -> list[dict[str, str]]:
    text = case_text(case)
    focuses = [
        {"kind": "main", "title": "主成功路径", "assertion": "业务操作按预期完成，核心状态发生正确变化"},
        {"kind": "validation", "title": "输入校验与错误提示", "assertion": "非法或缺失输入被拒绝，并返回用户可理解的错误"},
        {"kind": "state", "title": "状态流转与幂等", "assertion": "重复执行或状态边界不会造成重复写入、重复奖励或错误状态"},
        {"kind": "permission", "title": "权限与身份边界", "assertion": "未授权、越权或身份不匹配的参与者不能完成操作"},
        {"kind": "data", "title": "数据一致性", "assertion": "关键业务数据、关联记录和金额/计数保持一致"},
        {"kind": "boundary", "title": "边界值", "assertion": "最小值、最大值、档位边界和空集合按业务规则处理"},
        {"kind": "contract", "title": "接口契约", "assertion": "请求 payload、响应结构、业务错误码与契约一致"},
        {"kind": "ui", "title": "用户可见结果", "assertion": "页面展示、按钮状态、提示文案和跳转符合业务状态"},
    ]
    if any(token in text for token in ["reward", "settlement", "奖励", "结算", "金额", "播放量"]):
        focuses.insert(1, {"kind": "reward", "title": "奖励/结算规则", "assertion": "播放量档位、奖励金额和结算状态按业务规则计算"})
    if any(token in text for token in ["审核", "approve", "review", "拒绝"]):
        focuses.insert(1, {"kind": "review", "title": "审核结果分支", "assertion": "通过、拒绝和待审核状态均按规则流转"})
    if any(token in text for token in ["callback", "回调", "异步", "scheduler", "queue"]):
        focuses.insert(1, {"kind": "async", "title": "异步/回调一致性", "assertion": "异步任务、回调和重试不会破坏业务终态"})
    return focuses


def _resolve_task_project(case: dict[str, Any], layer: str, repo: Path) -> Any:
    """解析 spec-task 的目标项目，返回 ProjectInfo（含 name/root/kind）。"""
    if repo is None:
        raise QaAgentError("生成 spec-task 必须提供 --repo，以便从 pom.xml/package.json 识别目标项目")
    from qa_core.project_manifest import ProjectDiscoveryError, resolve_target_project

    try:
        return resolve_target_project(repo, case, layer)
    except ProjectDiscoveryError as exc:
        raise QaAgentError(str(exc)) from exc


def _project_display_name(project: Any, repo: Path) -> str:
    try:
        return project.root.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return project.name


def target_project_for_task(case: dict[str, Any], layer: str, repo: Path | None = None) -> str:
    project = _resolve_task_project(case, layer, repo)
    return _project_display_name(project, repo)


def target_file_for_task(case: dict[str, Any], layer: str, target_project: str, task_index: int, project_kind: str = "maven") -> str:
    # generic 项目不硬造测试文件路径，交由上游（AI）按项目实际约定填写
    if project_kind == "generic":
        return ""
    slug = spec_slug(str(case.get("module") or case.get("title") or case.get("id")))
    is_maven = project_kind == "maven"
    if layer == "unit":
        if is_maven:
            class_slug = "".join(part.capitalize() for part in slug.split("-")) or "BusinessFlow"
            return f"{target_project}/src/test/java/qa/{class_slug}Test.java"
        return f"{target_project}/tests/{slug}.test.mjs"
    if layer == "integration":
        if is_maven:
            return f"{target_project}/src/test/java/qa/{''.join(part.capitalize() for part in slug.split('-'))}IntegrationTest.java"
        return f"specs/integration/{slug}.spec.json"
    if layer == "api":
        if is_maven:
            return f"{target_project}/src/test/java/qa/{''.join(part.capitalize() for part in slug.split('-'))}ApiTest.java"
        return f"tests/api/{slug}/{str(case.get('id', '')).lower()}.sh"
    if layer == "e2e":
        module = str(case.get("module", slug))
        return f"tests/e2e/{module}/{str(case.get('id', '')).lower()}.spec.ts"
    return f"specs/{layer}/{slug}-{task_index}.json"


def command_for_spec_task(layer: str, target_project: str, target_file: str, project_kind: str = "maven") -> str:
    # generic 项目不硬造执行命令，交由上游（AI）按项目实际工具链填写；执行/门禁仍照常校验真实性
    if project_kind == "generic":
        return ""
    is_maven = project_kind == "maven"
    # 显式覆盖 pom.xml 的 skipTests/maven.test.skip，避免「命令 exit 0 但 0 测试执行」的假通过
    if layer == "unit":
        if is_maven:
            stem = Path(target_file).stem
            return f"cd {target_project} && mvn -q \"-Dtest={stem}\" -DskipTests=false -Dmaven.test.skip=false test"
        rel = target_file.replace(target_project + "/", "./")
        return f"cd {target_project} && node --test {rel}"
    if layer == "integration":
        if is_maven:
            stem = Path(target_file).stem
            return f"cd {target_project} && mvn -q \"-Dtest={stem}\" -DskipTests=false -Dmaven.test.skip=false test"
        return f"ming-qa manual integration execution for {target_file}"
    if layer == "api":
        if is_maven:
            stem = Path(target_file).stem
            return f"cd {target_project} && mvn -q \"-Dtest={stem}\" -DskipTests=false -Dmaven.test.skip=false test"
        return f"bash {target_file}"
    if layer == "e2e":
        return f"ming-qa run-e2e --repo . --spec {target_file}"
    return ""


def _append_unique(values: list[str], item: str) -> None:
    if item and item not in values:
        values.append(item)


def _make_oracle_item(type_: str, assertion: str, source_risk_id: str = "", extra: dict | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": type_, "assertion": assertion}
    if source_risk_id:
        item["sourceRiskId"] = source_risk_id
    if extra:
        item.update(extra)
    return item


def oracle_for_spec_task(case: dict[str, Any], layer: str, focus_kind: str) -> dict[str, list[dict[str, Any]]]:
    """Return structured oracle objects grouped by type.

    Each item is a dict with at least {"type": str, "assertion": str}.
    DB items may also carry "query" and "tableHint" from case.data.
    Items linked to a risk carry "sourceRiskId".
    """
    text = case_text(case)
    oracle: dict[str, list[dict[str, Any]]] = {
        "ui": [],
        "api": [],
        "db": [],
        "sideEffects": [],
        "negativeAssertions": [],
    }
    # 风险关联只能取自用例显式声明的风险 id。
    # 历史缺陷：这里曾取 traceability[0]，而该字段按 schema 还允许 requirement id /
    # 代码路径 / API 路径（见 references/test-case-schema.md）。使用者照 schema 写
    # API 路径时，oracle 的 sourceRiskId 就被写成 "POST /api/orders/place"，
    # 风险映射门禁因此恒判「P0/P1 风险未被覆盖」——误报，且逼人去补不存在的覆盖。
    case_risk_ids = _case_risk_ids(case)
    source_risk_id = case_risk_ids[0] if case_risk_ids else ""
    data_hint = case.get("data") or {}
    table_hint = str(data_hint.get("tableHint", "")) if isinstance(data_hint, dict) else ""

    # businessAssertions are the canonical source; group them by type heuristic
    for assertion in (case.get("businessAssertions") or []):
        low = assertion.lower()
        if any(t in low for t in ["db", "table", "record", "row", "balance", "余额", "记录", "数据库", "流水", "status", "状态"]):
            extra = {}
            if table_hint:
                extra["tableHint"] = table_hint
            oracle["db"].append(_make_oracle_item("db", assertion, source_risk_id, extra or None))
        elif any(t in low for t in ["response", "code", "http", "api", "returns", "响应", "返回", "接口", "code="]):
            oracle["api"].append(_make_oracle_item("api", assertion, source_risk_id))
        else:
            oracle["api"].append(_make_oracle_item("api", assertion, source_risk_id))

    if layer == "e2e" or focus_kind == "ui":
        oracle["ui"].append(_make_oracle_item("ui", "页面展示、按钮状态、提示文案和跳转结果符合业务预期"))
    if not oracle["api"] and (layer in {"api", "integration", "e2e"} or focus_kind in {"contract", "main"}):
        oracle["api"].append(_make_oracle_item("api", "接口状态码、业务码、关键响应字段和错误信息符合契约", source_risk_id))
    if not oracle["db"] and (layer in {"integration", "e2e"} or focus_kind in {"state", "data", "reward", "review", "async"}):
        db_item = _make_oracle_item("db", "核心状态、金额/计数、关联记录和审计记录保持一致", source_risk_id)
        if table_hint:
            db_item["tableHint"] = table_hint
        oracle["db"].append(db_item)
    if any(token in text for token in ["reward", "settlement", "payment", "notify", "callback", "queue", "奖励", "结算", "支付", "通知", "回调", "异步"]):
        oracle["sideEffects"].append(_make_oracle_item("sideEffect", "奖励/结算/通知/回调等副作用只发生一次且可追踪", source_risk_id))
    if focus_kind in {"validation", "state", "permission", "boundary"} or any(token in text for token in ["permission", "auth", "role", "repeat", "duplicate", "权限", "越权", "重复"]):
        oracle["negativeAssertions"].append(_make_oracle_item("negative", "非法输入、越权、重复提交或错误状态不会写入脏数据", source_risk_id))
    if not any(oracle.values()):
        oracle["api"].append(_make_oracle_item("api", "函数/服务输出与业务规则一致"))
    return oracle


def _build_task_assertions(case: dict[str, Any], focus: dict[str, str]) -> list[str]:
    """Merge business assertions, expected results, and focus assertion without duplicates."""
    seen: set[str] = set()
    result: list[str] = []

    def add(text: str) -> None:
        text = text.strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)

    for assertion in (case.get("businessAssertions") or []):
        add(str(assertion))
    add(focus.get("assertion", ""))
    expected_joined = "; ".join(str(e) for e in (case.get("expected") or [])[:2])
    if expected_joined:
        add(expected_joined)
    if not result:
        result.append("业务结果符合用例预期")
    return result


def method_name_for_task_id(task_id: str) -> str:
    """把 spec task id 转成合法的测试方法名。

    SPEC-TC-P0-001-UNIT-001 -> specTcP0001Unit001

    methodName 是「生成器指定、实现者遵循」的契约：生成阶段把名字定死，执行阶段
    按名字核对。只靠 testName 是不行的——那是中文业务描述（「… - 正常路径」），
    与真实代码里的方法名对不上，「把任务映射到某个文件」就会被冒充成「实现了测试」。
    """
    parts = [p for p in re.split(r"[-_.\s]+", str(task_id or "")) if p]
    if not parts:
        return "specTask"
    name = parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])
    if not name[:1].isalpha():
        name = "spec" + name
    return name


def build_spec_task(case: dict[str, Any], layer: str, focus: dict[str, str], index: int, repo: Path | None = None) -> dict[str, Any]:
    case_id = str(case.get("id", f"CASE-{index:03d}"))
    layer_code = {"unit": "UNIT", "integration": "INT", "api": "API", "e2e": "E2E"}[layer]
    project = _resolve_task_project(case, layer, repo)
    target_project = _project_display_name(project, repo)
    project_kind = getattr(project, "kind", "maven")
    target_file = target_file_for_task(case, layer, target_project, index, project_kind)
    command = command_for_spec_task(layer, target_project, target_file, project_kind)
    title = f"{case.get('title', case_id)} - {focus['title']}"
    task: dict[str, Any] = {
        "id": f"SPEC-{case_id}-{layer_code}-{index:03d}",
        "sourceCaseId": case_id,
        "priority": case.get("priority", "P2"),
        "layer": layer,
        "focus": focus["kind"],
        "targetProject": target_project,
        "targetFile": target_file,
        "testName": title,
        # 可执行的方法名（契约字段）：执行阶段按它核对测试是否真的实现了
        "methodName": method_name_for_task_id(f"SPEC-{case_id}-{layer_code}-{index:03d}"),
        "businessActor": case.get("businessActor", ""),
        "operationPath": case.get("operationPath", " -> ".join(map(str, case.get("steps", [])))),
        "assertions": _build_task_assertions(case, focus),
        # 用例关联的全部风险 id 落在 task 级字段上（qa-test-script-generator/SKILL.md
        # 就是这么定义它的）。oracle 各项的 sourceRiskId 只带主风险一个，多风险用例
        # 靠这里补全——风险映射门禁两处都读。
        "traceability": _case_risk_ids(case),
        "implementationStatus": "pending",
        "executionStatus": "not-run",
        "command": command,
        "oracle": oracle_for_spec_task(case, layer, focus["kind"]),
        "evidence": [],
        "notes": [],
    }
    # generic 项目：脚本不硬造路径/命令，标记要求上游（AI）在实现阶段填入 command/targetFile
    if project_kind == "generic":
        task["needsManualCommand"] = True
    return task


def generate_spec_tasks_data(
    cases_data: dict[str, Any],
    *,
    repo: Path | None = None,
    ratio: dict[str, float] | None = None,
    min_specs_override: str | None = None,
    max_e2e_per_case: int = 2,
    generation_profile: str = "development",
) -> dict[str, Any]:
    errors, normalized = validate_cases_data(cases_data, mutate=True)
    blocking = [error for error in errors if "technical quality gate" not in error]
    if blocking:
        raise QaAgentError("Invalid cases: " + "; ".join(blocking))
    ratio = ratio or dict(DEFAULT_SPEC_TASK_TARGET_RATIO)
    tasks: list[dict[str, Any]] = []
    per_case: dict[str, dict[str, Any]] = {}
    for case in normalized.get("cases", []):
        priority = str(case.get("priority", "P2")).upper()
        minimum = spec_task_minimum(priority, min_specs_override)
        layers = distribute_spec_task_layers(minimum, ratio, max_e2e=max_e2e_per_case)
        # requiresE2E 的用例即使 e2e 被配比挤掉，也强制补一个 e2e task
        requires_e2e = bool(case.get("requiresE2E"))
        if requires_e2e and "e2e" not in layers:
            layers = list(layers) + ["e2e"]
        focuses = spec_task_focuses(case)
        case_tasks: list[dict[str, Any]] = []
        for idx, layer in enumerate(layers, start=1):
            focus = focuses[(idx - 1) % len(focuses)]
            task = build_spec_task(case, layer, focus, idx, repo)
            tasks.append(task)
            case_tasks.append({"id": task["id"], "layer": layer, "focus": task["focus"]})
        per_case[str(case.get("id", ""))] = {
            "priority": priority,
            "minSpecTasks": minimum,
            "taskCount": len(case_tasks),
            "tasks": case_tasks,
        }
    counts = {layer: 0 for layer in sorted(SPEC_TASK_LAYERS)}
    for task in tasks:
        counts[task["layer"]] += 1
    # Build minSpecsByPriority summary
    min_specs_by_priority = {
        p: spec_task_minimum(p, min_specs_override)
        for p in ("P0", "P1", "P2", "P3")
    }
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "source": "test-cases.json",
        "generationProfile": generation_profile,
        "minSpecsByPriority": min_specs_by_priority,
        "targetRatio": ratio,
        "summary": {
            "total": len(tasks),
            "byLayer": {layer: count for layer, count in counts.items() if count},
            "caseCount": len(normalized.get("cases", [])),
        },
        "perCase": per_case,
        "tasks": tasks,
    }


def generate_spec_tasks(args: argparse.Namespace) -> None:
    cases_path = Path(args.cases).resolve()
    repo = Path(args.repo).resolve() if args.repo else None
    ratio = parse_ratio(args.ratio)
    data = generate_spec_tasks_data(
        read_json(cases_path),
        repo=repo,
        ratio=ratio,
        min_specs_override=args.min_specs_by_priority,
        max_e2e_per_case=args.max_e2e_per_case,
    )
    output = Path(args.output).resolve()
    write_json(output, data)
    update_manifest(repo, stage="test-script-generator",
                    artifact={"spec-tasks": _repo_rel(repo, output)},
                    status={"specTasksGenerated": "done"})
    print(f"已写入 spec tasks：{output}（{len(data.get('tasks', []))} 个任务）")


def _task_risk_ids(task: dict[str, Any]) -> set[str]:
    """收集一个 spec 任务关联的风险 ID。

    生成器（generate-spec-tasks）把风险关联挂在 oracle 各项的 sourceRiskId 上，
    并非任务级 traceability——而 traceability 是用例（cases[]）的字段。早期数据
    可能两种都有，所以两处都读，避免门禁因字段口径不一致而恒判失败。
    """
    ids: set[str] = set()
    for rid in task.get("traceability") or []:
        if rid:
            ids.add(str(rid))
    oracle = task.get("oracle")
    if isinstance(oracle, dict):
        for items in oracle.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("sourceRiskId"):
                    ids.add(str(item["sourceRiskId"]))
    return ids


def assert_oracle_mapping_data(
    risk_data: dict[str, Any],
    spec_tasks_data: dict[str, Any],
) -> dict[str, Any]:
    """校验每条 P0/P1 风险是否被 spec 任务的 oracle 断言覆盖。

    判据是「覆盖」，不是「文本等价」。

    风险的 requiredAssertions 是抽象业务类目（「金额计算正确」「重复提交不重复发放」），
    而任务侧 oracle 断言是具体到实现的说法（「余额扣减金额与商品单价一致」
    「余额流水与扣款一一对应且恒等」）。两者语义等价、用词不同，精确子串匹配
    永远为 0——曾经的实现因此对任何真实产物都恒判失败，门禁形同虚设。

    改为：每条 P0/P1 风险至少要有一条携带其 sourceRiskId 的 oracle 断言。
    这能抓住真正可行动的信号——某条风险根本没有任何测试覆盖；而「断言内容是否
    等价」属于人的判断，交给代码审查，门禁不该假装能做。

    文本命中的情况仍记录在 assertionTextMatch 里（informational，不参与判定），
    审查时可参考。
    """
    risks = risk_data.get("risks", [])
    tasks = spec_tasks_data.get("tasks", [])

    findings: list[dict[str, Any]] = []
    checked = 0

    for risk in risks:
        risk_id = risk.get("id", "")
        priority = str(risk.get("priority", "")).upper()
        if priority not in ("P0", "P1"):
            continue

        required = risk.get("requiredAssertions", []) or []
        if not required:
            # 没有断言的风险没有可校验的内容，跳过（保持既有契约，避免噪音）
            continue
        checked += 1

        # 该风险被多少条 oracle 断言显式关联（sourceRiskId 指向它）
        linked_count = 0
        text_hits: list[str] = []
        for task in tasks:
            if risk_id not in _task_risk_ids(task):
                continue
            for items in (task.get("oracle") or {}).values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and str(item.get("sourceRiskId") or "") == risk_id:
                        linked_count += 1
            task_text = json.dumps(task.get("oracle", {}), ensure_ascii=False) + json.dumps(
                task.get("assertions", []), ensure_ascii=False
            )
            for assertion in required:
                if assertion in task_text and assertion not in text_hits:
                    text_hits.append(assertion)

        if linked_count == 0:
            findings.append({
                "riskId": risk_id,
                "priority": priority,
                "category": risk.get("category", ""),
                "requiredAssertions": required,
                "assertionTextMatch": text_hits,
                "message": (
                    f"Risk {risk_id} ({priority}): 没有任何 spec 任务的 oracle 断言关联到该风险"
                    "——该风险在测试中完全没有覆盖"
                ),
            })

    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": "failed" if findings else "passed",
        "summary": {
            "totalRisksChecked": checked,
            "unmappedCount": len(findings),
        },
        "findings": findings,
    }


def assert_oracle_mapping(args: argparse.Namespace) -> None:
    """CLI command: assert-oracle-mapping --risk-analysis <file> --spec-tasks <file> --output <file>"""
    risk_path = Path(args.risk_analysis).resolve()
    spec_tasks_path = Path(args.spec_tasks).resolve()
    out = Path(args.output).resolve() if args.output else None

    risk_data = read_json(risk_path)
    spec_tasks_data = read_json(spec_tasks_path)
    result = assert_oracle_mapping_data(risk_data, spec_tasks_data)

    if out:
        write_json(out, result)
        print(f"Oracle mapping gate: {result['status']} → {out}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))

    if result["status"] == "failed":
        print(f"Oracle mapping gate FAILED: {result['summary']['unmappedCount']} P0/P1 risks not covered")
        raise SystemExit(1)


RISK_CATEGORY_LABELS: dict[str, str] = {
    "permission-boundary": "权限边界",
    "money-reward-settlement": "资金结算",
    "state-transition": "状态流转",
    "async-callback-retry": "异步回调",
    "data-consistency": "数据一致性",
    "negative-path": "异常路径",
    "concurrency": "并发安全",
    "provably-fair": "可验证公平",
    "business-rule": "业务规则",
    "privacy": "隐私脱敏",
}


def _risk_category_label(category: str) -> str:
    """风险类别中文标签。中文随枚举定义在源头，枚举外 fallback 原文（不崩、不露映射缺失）。"""
    zh = RISK_CATEGORY_LABELS.get(category)
    if not zh:
        return html.escape(category)
    return f'{zh} <span style="color:var(--muted);font-size:11px;">({html.escape(category)})</span>'


RISK_RULES: list[dict[str, Any]] = [
    {
        "category": "permission-boundary",
        "priority": "P0",
        "tokens": ["auth", "permission", "role", "admin", "token", "login", "权限", "越权", "角色", "登录"],
        "risk": "权限或身份边界可能导致越权操作、数据泄露或错误审批",
        "requiredAssertions": ["未登录被拒绝", "越权角色被拒绝", "只能操作授权范围内的数据"],
        "suggestedTestLayers": ["unit", "api", "e2e"],
    },
    {
        "category": "money-reward-settlement",
        "priority": "P0",
        "tokens": ["reward", "amount", "payment", "settlement", "cashout", "奖励", "金额", "结算", "提现", "支付"],
        "risk": "金额、奖励或结算规则可能重复发放、漏发或计算错误",
        "requiredAssertions": ["金额计算正确", "重复提交不重复发放", "结算状态与明细一致"],
        "suggestedTestLayers": ["unit", "integration", "api", "e2e"],
    },
    {
        "category": "state-transition",
        "priority": "P1",
        "tokens": ["status", "state", "approve", "reject", "submit", "review", "状态", "审核", "提交", "拒绝", "通过"],
        "risk": "状态流转或幂等边界可能导致错态、重复写入或不可恢复状态",
        "requiredAssertions": ["合法状态可流转", "非法状态被拒绝", "重复操作保持幂等"],
        "suggestedTestLayers": ["unit", "api", "integration"],
    },
    {
        "category": "async-callback-retry",
        "priority": "P1",
        "tokens": ["callback", "queue", "scheduler", "async", "retry", "回调", "异步", "重试", "队列", "定时"],
        "risk": "异步、回调或重试链路可能乱序、重复执行或丢失副作用",
        "requiredAssertions": ["重复回调幂等", "失败可重试", "最终状态一致"],
        "suggestedTestLayers": ["integration", "api"],
    },
    {
        "category": "data-consistency",
        "priority": "P1",
        "tokens": ["repository", "mapper", "entity", "sql", "mysql", "db", "数据库", "一致性", "事务", "记录"],
        "risk": "数据库写入、事务或关联记录可能不一致",
        "requiredAssertions": ["主记录与关联记录一致", "事务失败不产生半写入", "审计记录可追踪"],
        "suggestedTestLayers": ["integration", "api"],
    },
    {
        "category": "negative-path",
        "priority": "P2",
        "tokens": ["validate", "invalid", "error", "exception", "limit", "校验", "非法", "异常", "错误", "边界"],
        "risk": "异常输入和边界路径可能缺少断言",
        "requiredAssertions": ["错误输入返回可理解错误", "边界值按业务规则处理", "失败不产生副作用"],
        "suggestedTestLayers": ["unit", "api"],
    },
]


def _read_optional_json(path_value: str | None) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(path_value).resolve()
    if not path.exists():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


_MODULE_SCAN_EXTENSIONS = {".java", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".sql", ".kt", ".py"}


def _risk_candidate_files(
    repo: Path,
    context: dict[str, Any],
    module: list[str] | None = None,
) -> list[Path]:
    """Return candidate files for risk analysis.

    Priority order (first non-empty set wins):
      1. Explicit ``--module`` patterns (file, directory, or glob).
      2. git diff/status changed files.
      3. ``context`` changedFiles/files/affectedFiles.
      4. Full repository scan.

    ``module`` always wins; if it matches nothing, raises QaAgentError
    instead of silently falling through to a broader set.
    """
    if module:
        result: list[Path] = []
        for pattern in module:
            pattern = pattern.strip()
            if not pattern:
                continue
            resolved = (repo / pattern).resolve() if not Path(pattern).is_absolute() else Path(pattern).resolve()
            if resolved.exists():
                if resolved.is_file():
                    result.append(resolved)
                elif resolved.is_dir():
                    for path in _walk_module_files(resolved):
                        if path.suffix.lower() in _MODULE_SCAN_EXTENSIONS:
                            result.append(path)
            else:
                for path in iter_project_files(repo):
                    if _glob_match(repo, path, pattern) and path.suffix.lower() in _MODULE_SCAN_EXTENSIONS:
                        result.append(path)
        if not result:
            raise QaAgentError(
                f"--module 未匹配到任何文件：{module!r}。请检查路径、目录或 glob 模式是否正确。"
            )
        return result[:120]

    candidates: list[str] = []
    for git_args in (["diff", "--name-only"], ["diff", "--cached", "--name-only"]):
        for line in git_output(repo, git_args).splitlines():
            if line.strip():
                candidates.append(line.strip())
    for line in git_output(repo, ["status", "--short"]).splitlines():
        if len(line) > 3:
            candidates.append(line[3:].strip())
    for key in ["changedFiles", "files", "affectedFiles"]:
        value = context.get(key)
        if isinstance(value, list):
            candidates.extend(str(item) for item in value)
    result = []
    seen: set[str] = set()
    for item in candidates:
        item = item.strip().strip('"')
        if not item:
            continue
        try:
            path = (repo / item).resolve() if not Path(item).is_absolute() else Path(item).resolve()
        except (OSError, ValueError):
            # 非法路径（含 Windows 保留字符等）直接跳过——
            # 上游可能从 git 输出或 context 里带进非路径文本，不该让它中断扫描。
            continue
        if path.exists() and path.is_file() and path.suffix.lower() in MOJIBAKE_TEXT_EXTENSIONS:
            k = str(path).lower()
            if k not in seen:
                result.append(path)
                seen.add(k)
    if result:
        return result[:120]
    interesting_ext = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".java", ".py", ".sql", ".md", ".json"}
    for path in iter_project_files(repo):
        if path.suffix.lower() in interesting_ext:
            result.append(path)
        if len(result) >= 160:
            break
    return result


def _walk_module_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            files.append(Path(dirpath) / filename)
    return files


def _glob_match(repo: Path, path: Path, pattern: str) -> bool:
    import fnmatch
    rel = str(path.relative_to(repo) if path.is_relative_to(repo) else path)
    for pat in pattern.split(","):
        pat = pat.strip().replace("\\", "/")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat):
            return True
    return False


_E2E_CATEGORIES = {"state-transition", "permission-boundary"}
_E2E_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".vue", ".html"}


def _determine_selection_source(module: list[str] | None, candidates_from_git: bool, candidates_from_context: bool) -> str:
    if module:
        return "module"
    if candidates_from_git:
        return "git"
    if candidates_from_context:
        return "context"
    return "repository-fallback"


def analyze_risks_data(repo: Path, context: dict[str, Any] | None = None, existing_index: dict[str, Any] | None = None, module: list[str] | None = None) -> dict[str, Any]:
    context = context or {}
    existing_index = existing_index or {}
    # Detect source before delegating to candidate helper
    git_files: list[str] = []
    for git_args in (["diff", "--name-only"], ["diff", "--cached", "--name-only"]):
        git_files.extend(l.strip() for l in git_output(repo, git_args).splitlines() if l.strip())
    git_files.extend(l[3:].strip() for l in git_output(repo, ["status", "--short"]).splitlines() if len(l) > 3)
    context_files: list[str] = []
    for key in ["changedFiles", "files", "affectedFiles"]:
        if isinstance(context.get(key), list):
            context_files.extend(str(item) for item in context[key])
    selection_source = _determine_selection_source(
        module,
        bool(git_files),
        bool(context_files),
    )
    files = _risk_candidate_files(repo, context, module=module)
    inspected: list[str] = []
    matched: dict[str, dict[str, Any]] = {}
    existing_text = json.dumps(existing_index, ensure_ascii=False).lower() if existing_index else ""
    for path in files:
        rel = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
        inspected.append(rel)
        haystack = (rel + "\n" + read_text(path, limit=20000)).lower()
        for rule in RISK_RULES:
            tokens = [str(token).lower() for token in rule["tokens"]]
            hits = [token for token in tokens if token and token in haystack]
            if not hits:
                continue
            risk = matched.setdefault(
                rule["category"],
                {
                    "id": "",
                    "priority": rule["priority"],
                    "category": rule["category"],
                    "businessPath": "待映射到业务操作路径",
                    "risk": rule["risk"],
                    "impact": "可能影响核心业务验收、数据正确性或用户可见结果",
                    "affectedFiles": [],
                    "signals": [],
                    "requiredAssertions": list(rule["requiredAssertions"]),
                    "suggestedTestLayers": list(rule["suggestedTestLayers"]),
                    "coverageStatus": "covered" if any(token in existing_text for token in tokens) else "missing",
                },
            )
            if rel not in risk["affectedFiles"]:
                risk["affectedFiles"].append(rel)
            for hit in hits[:5]:
                if hit not in risk["signals"]:
                    risk["signals"].append(hit)
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    risks = sorted(matched.values(), key=lambda item: (priority_order.get(str(item["priority"]), 9), item["category"]))
    for index, risk in enumerate(risks, start=1):
        risk["id"] = f"RISK-{risk['priority']}-{index:03d}"
    coverage_gaps = [
        {
            "riskId": risk["id"],
            "priority": risk["priority"],
            "category": risk["category"],
            "gap": "缺少现有用例/测试证据覆盖该风险",
            "requiredAssertions": risk["requiredAssertions"],
        }
        for risk in risks
        if risk.get("coverageStatus") == "missing"
    ]
    required_oracles = {"ui": [], "api": [], "db": [], "sideEffects": [], "negativeAssertions": []}
    for risk in risks:
        category = risk.get("category")
        if category in {"permission-boundary", "negative-path"}:
            _append_unique(required_oracles["negativeAssertions"], "越权、非法输入、重复操作或错误状态不得产生业务副作用")
        if category in {"money-reward-settlement", "data-consistency", "state-transition"}:
            _append_unique(required_oracles["db"], "数据库核心状态、金额/计数、关联记录与审计记录一致")
        if category in {"state-transition", "permission-boundary", "negative-path"}:
            _append_unique(required_oracles["api"], "接口响应码、业务码、错误信息和关键字段符合契约")
        if category in {"async-callback-retry", "money-reward-settlement"}:
            _append_unique(required_oracles["sideEffects"], "异步任务、回调、奖励、结算或通知只产生一次且最终一致")
        if category in {"permission-boundary", "state-transition"}:
            _append_unique(required_oracles["ui"], "用户可见状态、按钮权限、提示文案和跳转符合业务规则")
    # Stamp requiresE2E on each risk based on category and affected file types
    for risk in risks:
        category = risk.get("category", "")
        has_frontend = any(
            Path(f).suffix.lower() in _E2E_EXTENSIONS for f in risk.get("affectedFiles", [])
        )
        risk["requiresE2E"] = category in _E2E_CATEGORIES or has_frontend
    by_priority: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for risk in risks:
        by_priority[str(risk["priority"])] = by_priority.get(str(risk["priority"]), 0) + 1
        by_category[str(risk["category"])] = by_category.get(str(risk["category"]), 0) + 1
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "summary": {"total": len(risks), "byPriority": by_priority, "byCategory": by_category, "inspectedFiles": len(inspected)},
        "scope": {
            "repo": str(repo),
            "selectionSource": selection_source,
            "requestedModules": list(module) if module else [],
            "contextRef": context.get("metadata", {}).get("scope") or context.get("scope") or "",
            "existingIndexSummary": existing_index.get("summary", {}),
        },
        "inspectedFiles": inspected[:120],
        "risks": risks,
        "coverageGaps": coverage_gaps,
        "requiredOracles": required_oracles,
    }


def analyze_risks(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    context = _read_optional_json(args.context)
    existing_index = _read_optional_json(args.existing_index)
    module = _parse_module_list(args.module) if getattr(args, "module", None) else None
    result = analyze_risks_data(repo, context=context, existing_index=existing_index, module=module)
    output = Path(args.output).resolve()
    write_json(output, result)
    update_manifest(repo, stage="risk-analyzer",
                    artifact={"risk-analysis": str(_repo_rel(repo, output))},
                    status={"risksAnalyzed": "done"})
    print(f"已写入风险分析：{output}（{result.get('summary', {}).get('total', 0)} 个风险）")


def _parse_module_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def coverage_balance_data(
    spec_tasks_data: dict[str, Any],
    *,
    target_ratio: dict[str, float] | None = None,
    tolerance: float = 0.15,
    min_specs_override: str | None = None,
) -> dict[str, Any]:
    tasks = spec_tasks_data.get("tasks", [])
    if not isinstance(tasks, list):
        raise QaAgentError("spec tasks root.tasks must be an array")
    target_ratio = target_ratio or spec_tasks_data.get("targetRatio") or dict(DEFAULT_SPEC_TASK_TARGET_RATIO)
    # 解析每个优先级的最小 task 数：显式 override > 产物 minSpecsByPriority > 默认
    effective_minimums = dict(DEFAULT_SPEC_TASK_MIN_BY_PRIORITY)
    if min_specs_override:
        for part in min_specs_override.split(","):
            if not part.strip():
                continue
            key, _, raw = part.partition("=")
            key = key.strip().upper()
            if key in CASE_PRIORITIES and raw.strip():
                effective_minimums[key] = int(raw)
    elif isinstance(spec_tasks_data.get("minSpecsByPriority"), dict):
        for key, value in spec_tasks_data["minSpecsByPriority"].items():
            key_upper = str(key).upper()
            if key_upper in CASE_PRIORITIES:
                effective_minimums[key_upper] = int(value)
    counts = {layer: 0 for layer in sorted(SPEC_TASK_LAYERS)}
    per_case_count: dict[str, int] = {}
    priorities: dict[str, str] = {}
    invalid_tasks: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            invalid_tasks.append("non-object task")
            continue
        layer = task.get("layer")
        if layer not in SPEC_TASK_LAYERS:
            invalid_tasks.append(str(task.get("id", "<missing-id>")))
            continue
        counts[layer] += 1
        case_id = str(task.get("sourceCaseId", ""))
        per_case_count[case_id] = per_case_count.get(case_id, 0) + 1
        priorities[case_id] = str(task.get("priority", "P2")).upper()
    total = sum(counts.values())
    actual = {layer: (counts[layer] / total if total else 0) for layer in sorted(SPEC_TASK_LAYERS)}
    findings: list[dict[str, Any]] = []
    for layer, target in target_ratio.items():
        delta = actual.get(layer, 0) - target
        if abs(delta) > tolerance:
            findings.append(
                {
                    "type": "ratio-out-of-range",
                    "layer": layer,
                    "target": round(target, 4),
                    "actual": round(actual.get(layer, 0), 4),
                    "delta": round(delta, 4),
                    "severity": "warn",
                }
            )
    for case_id, priority in sorted(priorities.items()):
        minimum = effective_minimums.get(priority, 2)
        actual_count = per_case_count.get(case_id, 0)
        if actual_count < minimum:
            findings.append(
                {
                    "type": "case-under-specified",
                    "sourceCaseId": case_id,
                    "priority": priority,
                    "minimum": minimum,
                    "actual": actual_count,
                    "severity": "fail" if priority in {"P0", "P1"} else "warn",
                }
            )
    if counts.get("e2e", 0) > counts.get("api", 0) + counts.get("integration", 0):
        findings.append(
            {
                "type": "e2e-too-heavy",
                "severity": "warn",
                "summary": "E2E tasks should be fewer than API+integration tasks",
            }
        )
    if invalid_tasks:
        findings.append({"type": "invalid-task-layer", "severity": "fail", "tasks": invalid_tasks[:20]})
    status = "passed"
    if any(finding.get("severity") == "fail" for finding in findings):
        status = "failed"
    elif findings:
        status = "warn"
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": status,
        "summary": {
            "total": total,
            "byLayer": {layer: counts[layer] for layer in sorted(SPEC_TASK_LAYERS) if counts[layer]},
            "actualRatio": {layer: round(actual[layer], 4) for layer in sorted(SPEC_TASK_LAYERS)},
            "targetRatio": {layer: round(target_ratio.get(layer, 0), 4) for layer in sorted(SPEC_TASK_LAYERS)},
        },
        "perCaseCount": per_case_count,
        "findings": findings,
    }


def coverage_balance(args: argparse.Namespace) -> None:
    spec_tasks = read_json(Path(args.spec_tasks).resolve())
    result = coverage_balance_data(
        spec_tasks,
        target_ratio=parse_ratio(args.ratio) if args.ratio else None,
        tolerance=args.tolerance,
        min_specs_override=args.min_specs_by_priority,
    )
    if args.output:
        write_json(Path(args.output).resolve(), result)
    print(
        "Coverage balance: {status} ({total} tasks)".format(
            status=result["status"],
            total=result["summary"]["total"],
        )
    )
    for layer, count in result["summary"]["byLayer"].items():
        print(f"- {layer}: {count} ({result['summary']['actualRatio'].get(layer, 0):.2%})")
    for finding in result.get("findings", [])[:20]:
        print(f"警告：{finding.get('type')} {json.dumps(finding, ensure_ascii=False)}")
    if args.strict and result["status"] != "passed":
        raise SystemExit(1)


def normalize_task_status(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float, bool)):
        return True
    if isinstance(value, list):
        return any(meaningful_value(item) for item in value)
    if isinstance(value, dict):
        return any(meaningful_value(item) for item in value.values())
    return bool(str(value).strip())


_MANUAL_VERIFY_HINTS = ("手动", "手工", "manual")
_DATA_BLOCK_HINTS = ("数据", "账号", "资产", "余额", "跨日", "封禁", "缺少", "缺失")
_RECON_HINTS = ("SELECT", "select", "侦察", "recon", "count(*)", "查询")


def has_meaningful_evidence(task: dict[str, Any]) -> bool:
    evidence = task.get("evidence")
    if not meaningful_value(evidence):
        return False
    # 证据溯源：真实执行证据不得是「手动验证/手工核对」（问题 3：不能靠手工核对冒充真实执行）
    text = json.dumps(evidence, ensure_ascii=False)
    return not any(hint in text for hint in _MANUAL_VERIFY_HINTS)


def has_required_task_mapping(task: dict[str, Any]) -> bool:
    return all(
        meaningful_value(task.get(key))
        for key in ["targetFile", "testName", "command", "assertions"]
    )


def has_blocker_evidence(task: dict[str, Any]) -> bool:
    blockers = task.get("blockers") or task.get("blocker")
    next_action = task.get("nextAction") or task.get("next_action")
    owner = task.get("owner")
    has_evidence = meaningful_value(task.get("evidence")) or meaningful_value(task.get("notes"))
    if not (meaningful_value(blockers) and has_evidence and meaningful_value(next_action) and meaningful_value(owner)):
        return False
    # blocked 准入：数据类阻塞（缺数据/账号/资产）必须含 SELECT 侦察证据，不能只是「需 X」这种没验证过的理由
    blocker_text = json.dumps(blockers, ensure_ascii=False)
    if any(hint in blocker_text for hint in _DATA_BLOCK_HINTS):
        return any(hint in blocker_text for hint in _RECON_HINTS)
    return True


def is_placeholder_stub(path: Path) -> bool:
    """检测占位 stub 脚本：echo BLOCKED + exit 0，伪造「执行过」。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(r'echo\s+["\']?BLOCKED', text)) and "exit 0" in text


def count_test_methods(path: Path) -> int:
    """统计测试文件里可发现的测试方法数：Java 数 @Test；脚本非占位即算 1。"""
    if path.suffix.lower() == ".java":
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        return len(re.findall(r"@Test\b", text))
    return 0 if is_placeholder_stub(path) else 1


def discover_test_method_names(path: Path) -> set[str] | None:
    """提取测试文件里真实存在的测试方法名。

    Java 返回 @Test 标注的方法名；其它类型（Playwright spec 等）的「方法名」概念
    不同，返回 None 表示该文件只能按数量校验。

    只做数量校验是抓不住「映射≠实现」的：54 个 task 映射到一个只有 7 个 @Test 的
    文件，只要补到 8 个方法就能蒙混过关，而那 8 个是不是对应的断言没人知道。
    """
    if path.suffix.lower() != ".java":
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()

    names: set[str] = set()
    for match in re.finditer(r"@Test\b", text):
        # @Test 与方法签名之间可能隔着 @DisplayName 等注解，向后扫一段
        tail = text[match.end(): match.end() + 600]
        signature = re.search(r"\bvoid\s+([A-Za-z_$][\w$]*)\s*\(", tail)
        if signature:
            names.add(signature.group(1))
    return names


def assert_script_implementation_data(spec_tasks_data: dict[str, Any], repo: Path) -> dict[str, Any]:
    """脚本实现真实性门禁：映射到文件 ≠ 实现了测试。

    对每个 targetFile 校验：文件存在、非占位 stub、可发现的测试方法数 ≥ 映射到它的 task 数。
    否则「54 个 task 映射到只有 7 个 @Test 的文件」会被判为虚标实现。
    """
    tasks = spec_tasks_data.get("tasks", []) if isinstance(spec_tasks_data, dict) else []
    if not isinstance(tasks, list):
        return {"version": "1.0", "status": "failed", "summary": {"files": 0, "findings": 1},
                "findings": [{"type": "invalid-tasks", "severity": "fail", "message": "tasks 必须是数组"}]}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tf = str(t.get("targetFile", "") or "").strip()
        if tf:
            by_file.setdefault(tf, []).append(t)
    findings: list[dict[str, Any]] = []
    for tf, file_tasks in sorted(by_file.items()):
        path = Path(tf).resolve() if Path(tf).is_absolute() else (Path(repo) / tf).resolve()
        if not path.exists():
            findings.append({"type": "target-file-missing", "severity": "fail", "targetFile": tf,
                             "taskCount": len(file_tasks), "message": "targetFile 不存在，测试未实现"})
            continue
        if is_placeholder_stub(path):
            findings.append({"type": "placeholder-stub", "severity": "fail", "targetFile": tf,
                             "message": "占位 stub 脚本（echo BLOCKED + exit 0），测试未真正实现"})
            continue
        method_count = count_test_methods(path)

        # 优先按方法名核对：task 声明了 methodName 且目标文件能解析出方法名时，
        # 逐个核对它是否真的存在。数量校验只能保证「够数」，保证不了「对得上」。
        expected_names = [str(t.get("methodName") or "") for t in file_tasks]
        expected_names = [n for n in expected_names if n]
        actual_names = discover_test_method_names(path)
        if actual_names is not None and expected_names:
            missing = [n for n in expected_names if n not in actual_names]
            if missing:
                findings.append({
                    "type": "method-not-implemented", "severity": "fail", "targetFile": tf,
                    "taskCount": len(file_tasks), "testMethodCount": method_count,
                    "missingMethods": missing[:20], "missingMethodCount": len(missing),
                    "message": (
                        f"映射 {len(file_tasks)} 个 task，其中 {len(missing)} 个声明的方法"
                        f"在 {path.name} 中不存在（如 {', '.join(missing[:3])}）"
                    ),
                })
            continue

        if method_count < len(file_tasks):
            findings.append({"type": "mapping-not-implemented", "severity": "fail", "targetFile": tf,
                             "taskCount": len(file_tasks), "testMethodCount": method_count,
                             "message": f"映射 {len(file_tasks)} 个 task 但只有 {method_count} 个测试方法"})
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": "failed" if findings else "passed",
        "summary": {"files": len(by_file), "findings": len(findings)},
        "findings": findings,
    }


def assert_script_implementation(args: argparse.Namespace) -> None:
    spec_tasks = read_json(Path(args.spec_tasks).resolve())
    repo = Path(args.repo).resolve()
    result = assert_script_implementation_data(spec_tasks, repo)
    if args.output:
        write_json(Path(args.output).resolve(), result)
    print(f"脚本实现门禁：{result['status']}（{result['summary']['files']} 个 targetFile，{result['summary']['findings']} 个问题）")
    for finding in result.get("findings", [])[:20]:
        print(f"  - {finding.get('type')}: {finding.get('targetFile', finding.get('message', ''))}")
    if result["status"] != "passed":
        raise SystemExit(1)


def assert_completion_data(
    cases_data: dict[str, Any],
    spec_tasks_data: dict[str, Any],
    *,
    priorities: set[str] | None = None,
    allow_blocked: bool = False,
    allow_deferred: bool = False,
    allow_skipped: bool = False,
    fail_on_failed: bool = False,
    min_specs_override: str | None = None,
) -> dict[str, Any]:
    priorities = priorities or {"P0", "P1"}
    errors, normalized_cases = validate_cases_data(cases_data, mutate=True)
    blocking_case_errors = [error for error in errors if "technical quality gate" not in error]
    tasks = spec_tasks_data.get("tasks", [])
    if not isinstance(tasks, list):
        raise QaAgentError("spec tasks root.tasks must be an array")

    cases_by_id = {
        str(case.get("id", "")): case
        for case in normalized_cases.get("cases", [])
        if str(case.get("priority", "P2")).upper() in priorities
    }
    tasks_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in cases_by_id}
    # 诊断计数器：跟踪被过滤掉的任务及原因
    filter_diag: dict[str, int] = {"total_tasks": len(tasks), "not_dict": 0,
                                     "priority_mismatch": 0, "missing_fields": 0, "matched": 0}
    for task in tasks:
        if not isinstance(task, dict):
            filter_diag["not_dict"] += 1
            continue
        priority = str(task.get("priority", "P2")).upper()
        if priority not in priorities:
            filter_diag["priority_mismatch"] += 1
            continue
        task_id_key = str(task.get("id", ""))
        case_id = str(task.get("sourceCaseId", ""))
        if not case_id or not task_id_key:
            filter_diag["missing_fields"] += 1
            continue
        if case_id in tasks_by_case:
            tasks_by_case[case_id].append(task)
            filter_diag["matched"] += 1

    # 输出诊断摘要（仅当有任务被过滤时）
    total_filtered = filter_diag["priority_mismatch"] + filter_diag["missing_fields"]
    if total_filtered > 0:
        reasons: list[str] = []
        if filter_diag["priority_mismatch"]:
            reasons.append(f"{filter_diag['priority_mismatch']} 个因 priority 不在 {priorities} 中被过滤")
        if filter_diag["missing_fields"]:
            reasons.append(f"{filter_diag['missing_fields']} 个因缺少 priority/sourceCaseId/id 字段被过滤")
        detail = "; ".join(reasons)
        print(f"[assert-completion] {total_filtered}/{filter_diag['total_tasks']} 个 task 被过滤（{detail}）")

    findings: list[dict[str, Any]] = []
    for error in blocking_case_errors:
        findings.append({"type": "invalid-case-file", "severity": "fail", "message": error})

    confirmed_statuses = {"confirmed", "implemented", "passed", "failed", "blocked", "skipped"}
    for case_id, case in sorted(cases_by_id.items()):
        case_tasks = tasks_by_case.get(case_id, [])
        priority = str(case.get("priority", "P2")).upper()
        case_status = normalize_task_status(case.get("status"))
        if case_status not in confirmed_statuses:
            findings.append(
                {
                    "type": "case-not-confirmed",
                    "severity": "fail",
                    "sourceCaseId": case_id,
                    "priority": priority,
                    "status": case_status or "<missing>",
                    "title": case.get("title"),
                }
            )
        if not case_tasks:
            diag = ""
            if total_filtered > 0 and filter_diag["matched"] == 0:
                diag = f"（{total_filtered} 个 task 存在但被过滤，检查每个 task 的 priority 字段是否与 enforcing priorities 匹配）"
            findings.append(
                {
                    "type": "case-without-spec-tasks",
                    "severity": "fail",
                    "sourceCaseId": case_id,
                    "priority": priority,
                    "title": case.get("title"),
                    "diagnostic": diag or None,
                }
            )
            continue
        minimum = spec_task_minimum(priority, min_specs_override)
        if len(case_tasks) < minimum:
            findings.append(
                {
                    "type": "case-under-min-spec-tasks",
                    "severity": "fail",
                    "sourceCaseId": case_id,
                    "priority": priority,
                    "actualTaskCount": len(case_tasks),
                    "minimumTaskCount": minimum,
                    "title": case.get("title"),
                }
            )
        # 用例层 verified 闸门：至少一个 task 真实执行通过（passed + 有执行证据），否则该用例业务未被验证。
        # 全部 blocked / 未实现 / 未执行 = 未验证，门禁必须 fail，不允许 complete_with_allowed_gaps。
        verified = any(
            normalize_task_status(t.get("executionStatus")) == "passed" and has_meaningful_evidence(t)
            for t in case_tasks
        )
        if not verified:
            findings.append(
                {
                    "type": "case-not-verified",
                    "severity": "fail",
                    "sourceCaseId": case_id,
                    "priority": priority,
                    "title": case.get("title"),
                    "message": "该用例没有任何 task 真实执行通过（全部 blocked/未实现/未执行），业务未被验证",
                }
            )

    counters = {
        "casesChecked": len(cases_by_id),
        "tasksChecked": 0,
        "implemented": 0,
        "executed": 0,
        "blocked": 0,
        "deferred": 0,
        "skipped": 0,
        "failed": 0,
        "unimplemented": 0,
        "unexecuted": 0,
    }
    for task in tasks:
        if not isinstance(task, dict):
            findings.append({"type": "invalid-task", "severity": "fail", "task": str(task)[:200]})
            continue
        priority = str(task.get("priority", "P2")).upper()
        if priority not in priorities:
            continue
        counters["tasksChecked"] += 1
        task_id = str(task.get("id", "<missing-id>"))
        case_id = str(task.get("sourceCaseId", ""))
        implementation_status = normalize_task_status(task.get("implementationStatus"))
        execution_status = normalize_task_status(task.get("executionStatus"))
        status_pair = {
            "taskId": task_id,
            "sourceCaseId": case_id,
            "priority": priority,
            "implementationStatus": implementation_status,
            "executionStatus": execution_status,
            "targetFile": task.get("targetFile"),
            "testName": task.get("testName"),
        }
        if case_id not in cases_by_id:
            findings.append({"type": "task-without-confirmed-case", "severity": "fail", **status_pair})

        implemented = not (
            implementation_status in SPEC_TASK_UNIMPLEMENTED_STATUS
            or implementation_status not in SPEC_TASK_TERMINAL_IMPLEMENTATION_STATUS
        )
        if not implemented:
            counters["unimplemented"] += 1
            # 「计划没建完」不是「业务没验证」。这套 task 是按测试金字塔自动展开的，
            # 建不建属于计划完成度——降为 warn，只报告不阻断（阻断由下文用例层的
            # case-not-verified 负责）。曾经这里是 fail，导致业务用例全部执行通过、
            # case-not-verified 已清零时，门禁仍判 failed，使用者被迫补大量仅为凑数的
            # 测试来迁就粒度——那是让门禁在生产工作量，而不是在生产置信度。
            findings.append({"type": "task-not-implemented", "severity": "warn", **status_pair})
        else:
            counters["implemented"] += 1

        if execution_status in SPEC_TASK_UNEXECUTED_STATUS or execution_status not in SPEC_TASK_TERMINAL_EXECUTION_STATUS:
            counters["unexecuted"] += 1
            # 没实现的 task 谈不上「没执行」，不重复记一条账
            if implemented:
                findings.append({"type": "task-not-executed", "severity": "fail", **status_pair})
            continue

        if execution_status == "passed":
            counters["executed"] += 1
            if not has_required_task_mapping(task):
                findings.append({"type": "passed-task-missing-mapping", "severity": "fail", **status_pair})
            if not has_meaningful_evidence(task):
                findings.append({"type": "passed-task-missing-evidence", "severity": "fail", **status_pair})
            # generic 项目的 task 由上游（AI）填 command。声称通过却没有可执行命令＝假通过：
            # 执行层不可能跑一条空命令还得到 passed。这里给出明确指引，而不是让空命令
            # 在执行阶段变成含糊失败。
            if not str(task.get("command", "")).strip():
                findings.append({
                    "type": "generic-command-missing",
                    "severity": "fail",
                    "message": "task 声称已通过但 command 为空；generic 项目必须由实现阶段填入可执行的测试命令",
                    **status_pair,
                })
        elif execution_status == "failed":
            counters["failed"] += 1
            if not has_required_task_mapping(task):
                findings.append({"type": "failed-task-missing-mapping", "severity": "fail", **status_pair})
            if not has_meaningful_evidence(task):
                findings.append({"type": "failed-task-missing-evidence", "severity": "fail", **status_pair})
            if fail_on_failed:
                findings.append({"type": "task-failed", "severity": "fail", **status_pair})
            else:
                findings.append({"type": "task-failed", "severity": "warn", **status_pair})
        elif execution_status == "blocked":
            counters["blocked"] += 1
            if not allow_blocked:
                findings.append({"type": "task-blocked", "severity": "fail", **status_pair})
            elif not has_blocker_evidence(task):
                findings.append({"type": "blocked-task-missing-evidence", "severity": "fail", **status_pair})
        elif execution_status in {"deferred", "explicitly-deferred", "explicitly_deferred"}:
            counters["deferred"] += 1
            if not allow_deferred:
                findings.append({"type": "task-deferred", "severity": "fail", **status_pair})
            elif not has_blocker_evidence(task):
                findings.append({"type": "deferred-task-missing-evidence", "severity": "fail", **status_pair})
        elif execution_status == "skipped":
            counters["skipped"] += 1
            if not allow_skipped:
                findings.append({"type": "task-skipped", "severity": "fail", **status_pair})
            elif not has_blocker_evidence(task):
                findings.append({"type": "skipped-task-missing-evidence", "severity": "fail", **status_pair})
        else:
            counters["executed"] += 1

    status = "passed" if not any(finding.get("severity") == "fail" for finding in findings) else "failed"
    decision = "complete" if status == "passed" else "incomplete"
    if status == "passed" and (counters["blocked"] or counters["deferred"] or counters["skipped"]):
        decision = "complete_with_allowed_gaps"
    if status == "passed" and counters["failed"]:
        decision = "complete_not_ready"
    next_task_ids = [
        str(finding.get("taskId") or finding.get("sourceCaseId"))
        for finding in findings
        if finding.get("severity") == "fail" and (finding.get("taskId") or finding.get("sourceCaseId"))
    ][:50]
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": status,
        "decision": decision,
        "scope": {"priorities": sorted(priorities)},
        "summary": counters,
        "nextTaskIds": next_task_ids,
        "findings": findings,
    }


def assert_completion(args: argparse.Namespace) -> None:
    priorities = {p.strip().upper() for p in str(args.priorities).split(",") if p.strip()}
    result = assert_completion_data(
        read_json(Path(args.cases).resolve()),
        read_json(Path(args.spec_tasks).resolve()),
        priorities=priorities,
        allow_blocked=args.allow_blocked,
        allow_deferred=args.allow_deferred,
        allow_skipped=args.allow_skipped,
        fail_on_failed=args.fail_on_failed,
        min_specs_override=args.min_specs_by_priority,
    )
    if args.output:
        write_json(Path(args.output).resolve(), result)
    summary = result["summary"]
    print(
        "Completion check: {status} ({decision}); cases={casesChecked}, tasks={tasksChecked}, "
        "unimplemented={unimplemented}, unexecuted={unexecuted}, blocked={blocked}, deferred={deferred}, "
        "skipped={skipped}, failed={failed}".format(status=result["status"], decision=result["decision"], **summary)
    )
    for finding in result.get("findings", [])[:50]:
        print(f"{finding.get('severity', 'warn').upper()}: {finding.get('type')} {json.dumps(finding, ensure_ascii=False)}")
    if result["status"] != "passed":
        raise SystemExit(1)


BLOCKING_REVIEW_SEVERITIES = {"p0", "p1", "blocking", "critical", "high", "fail", "failed"}
REVIEW_RESOLVED_STATUSES = {"resolved", "accepted", "waived", "fixed", "closed", "done", "non-blocking"}
REVIEW_FAILED_STATUSES = {"failed", "fail", "blocking", "not-ready", "not_ready", "rejected"}


def _review_findings(review_data: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for key in ["findings", "issues", "readinessConstraints", "constraints"]:
        value = review_data.get(key)
        if isinstance(value, list):
            findings.extend(item for item in value if isinstance(item, dict))
    nested = review_data.get("review")
    if isinstance(nested, dict):
        findings.extend(_review_findings(nested))
    return findings


def _finding_is_resolved(finding: dict[str, Any]) -> bool:
    status = normalize_task_status(finding.get("status") or finding.get("resolution") or finding.get("state"))
    if status in REVIEW_RESOLVED_STATUSES:
        return True
    return bool(finding.get("resolved") or finding.get("accepted") or finding.get("waived"))


def _resolve_deferred(deferred_list: list[dict[str, Any]]) -> dict[str, str]:
    """Return {findingId: deferredSince} for deferred findings that are still within their grace period."""
    active: dict[str, str] = {}
    now_str = utc_now()
    for d in deferred_list:
        fid = d.get("id")
        if not fid:
            continue
        expires = d.get("expiresAt")
        if expires and expires < now_str:
            continue  # expired — back to blocking
        since = d.get("deferredSince", "unknown")
        active[fid] = since
    return active


def assert_code_review_data(review_data: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    status = normalize_task_status(review_data.get("status"))
    if not status:
        findings.append({"type": "code-review-status-missing", "severity": "fail", "message": "code-review.json 缺少 status"})
    elif status in REVIEW_FAILED_STATUSES:
        findings.append({"type": "code-review-status-failed", "severity": "fail", "status": status})
    review_findings = _review_findings(review_data)
    # Resolve deferred findings: P0/P1 items listed in deferredFindings are non-blocking until expired
    deferred = _resolve_deferred(review_data.get("deferredFindings", []))
    blocking = []
    for finding in review_findings:
        severity = normalize_task_status(finding.get("severity") or finding.get("priority") or finding.get("level"))
        verdict = normalize_task_status(finding.get("verdict"))
        # verdict 参与判定：REJECTED（已排除）不 blocking；PLAUSIBLE（待确认）和 CONFIRMED 都 blocking（未排除 = 风险未清）
        is_blocking = severity in BLOCKING_REVIEW_SEVERITIES and not _finding_is_resolved(finding) and verdict != "rejected"
        if is_blocking:
            finding_id = finding.get("id") or finding.get("findingId")
            # 可追溯性：P0/P1 blocking finding 必须有 file + evidence，泛泛「需检查」不算 finding
            if not meaningful_value(finding.get("file") or finding.get("path")):
                findings.append({"type": "finding-missing-file", "severity": "fail",
                                 "findingSeverity": severity, "findingId": finding_id,
                                 "message": "blocking finding 缺 file，无法追溯到具体代码"})
            if not meaningful_value(finding.get("evidence")):
                findings.append({"type": "finding-missing-evidence", "severity": "fail",
                                 "findingSeverity": severity, "findingId": finding_id,
                                 "message": "blocking finding 缺 evidence，无法证明是读代码得出的独立结论"})
            # If this finding is deferred and not expired, skip blocking
            if finding_id and finding_id in deferred:
                findings.append({
                    "type": "blocking-code-review-finding",
                    "severity": "warn",
                    "findingSeverity": severity,
                    "message": finding.get("message") or finding.get("title") or finding.get("summary") or "blocking review finding",
                    "file": finding.get("file") or finding.get("path"),
                    "line": finding.get("line"),
                    "deferred": True,
                    "deferredSince": deferred[finding_id],
                })
                continue
            item = {
                "type": "blocking-code-review-finding",
                "severity": "fail",
                "findingSeverity": severity,
                "message": finding.get("message") or finding.get("title") or finding.get("summary") or "blocking review finding",
                "file": finding.get("file") or finding.get("path"),
                "line": finding.get("line"),
            }
            findings.append(item)
            blocking.append(item)
    result_status = "passed" if not any(item.get("severity") == "fail" for item in findings) else "failed"
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": result_status,
        "summary": {"findings": len(review_findings), "blockingFindings": len(blocking)},
        "findings": findings,
    }


def assert_code_review(args: argparse.Namespace) -> None:
    review_path = Path(args.code_review).resolve()
    if review_path.exists():
        result = assert_code_review_data(read_json(review_path))
    else:
        result = {
            "version": "1.0",
            "generatedAt": utc_now(),
            "status": "failed",
            "summary": {"findings": 0, "blockingFindings": 1},
            "findings": [{"type": "code-review-missing", "severity": "fail", "path": str(review_path)}],
        }
    if args.output:
        write_json(Path(args.output).resolve(), result)
    print(f"代码审查门禁：{result['status']}；阻塞项={result.get('summary', {}).get('blockingFindings', 0)}")
    for finding in result.get("findings", [])[:50]:
        print(f"{finding.get('severity', 'warn').upper()}: {finding.get('type')} {json.dumps(finding, ensure_ascii=False)}")
    repo = Path(args.repo).resolve()
    update_manifest(
        repo,
        stage="code-reviewer",
        artifact={"code-review": _repo_rel(repo, review_path)},
        status={"codeReviewed": result["status"]},
    )
    if result["status"] != "passed":
        raise SystemExit(1)


def check_evidence_integrity(
    cases_data: dict[str, Any] | None,
    spec_tasks_data: dict[str, Any] | None,
    latest_run_data: dict[str, Any] | None,
    code_review_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """证据完整性门禁：报告渲染前校验执行证据链完整，杜绝「summary 全 0 仍渲染报告」。

    校验：
      1. latest-run.json 聚合结果非空（totalCases > 0）。
      2. unmatchedLogs 为空（无未匹配日志）。
      3. 每个 spec-task 的 sourceCaseId 都有对应执行记录。
      4. code-review.json 的 scope 必须是合法对象格式。
    """
    cases_data = cases_data or {}
    spec_tasks_data = spec_tasks_data or {}
    latest_run_data = latest_run_data or {}
    code_review_data = code_review_data or {}

    cases = cases_data.get("cases", []) if isinstance(cases_data.get("cases"), list) else []
    tasks = spec_tasks_data.get("tasks", []) if isinstance(spec_tasks_data.get("tasks"), list) else []
    run_summary = latest_run_data.get("summary", {}) if isinstance(latest_run_data.get("summary"), dict) else {}
    run_cases = latest_run_data.get("cases", []) if isinstance(latest_run_data.get("cases"), list) else []
    unmatched = latest_run_data.get("unmatchedLogs", []) if isinstance(latest_run_data.get("unmatchedLogs"), list) else []

    run_total = int(run_summary.get("totalCases", 0) or 0)
    run_case_ids = {str(c.get("caseId", "") or "").upper() for c in run_cases}

    findings: list[dict[str, Any]] = []

    if run_total <= 0:
        findings.append({
            "type": "run-summary-empty",
            "severity": "fail",
            "message": "latest-run.json summary.totalCases=0，执行证据缺失，不得渲染为可信报告",
        })
    if unmatched:
        findings.append({
            "type": "unmatched-run-logs",
            "severity": "fail",
            "unmatchedLogs": unmatched[:20],
            "message": f"{len(unmatched)} 个 run log 未能匹配，证据链断裂",
        })
    missing_runs: list[str] = []
    for task in tasks:
        case_id = str(task.get("sourceCaseId", "") or "").upper()
        if case_id and case_id not in run_case_ids:
            missing_runs.append(case_id)
    if missing_runs:
        findings.append({
            "type": "task-without-run-evidence",
            "severity": "fail",
            "caseIds": list(dict.fromkeys(missing_runs))[:20],
            "message": f"{len(set(missing_runs))} 个 spec-task 缺少执行记录",
        })
    scope = code_review_data.get("scope") if code_review_data else None
    if scope is not None and not isinstance(scope, dict):
        findings.append({
            "type": "invalid-code-review-scope",
            "severity": "fail",
            "message": "code-review.json 的 scope 必须是对象 {files: []}，当前为非法类型",
        })

    status = "failed" if any(f.get("severity") == "fail" for f in findings) else "passed"
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": status,
        "summary": {
            "testCasesTotal": len(cases),
            "specTasksTotal": len(tasks),
            "runCasesTotal": run_total,
            "unmatchedLogs": len(unmatched),
            "findings": len(findings),
        },
        "findings": findings,
    }


def assert_evidence_integrity(args: argparse.Namespace) -> None:
    cases_data = read_json(Path(args.cases).resolve()) if getattr(args, "cases", None) and Path(args.cases).exists() else {}
    spec_tasks_data = read_json(Path(args.spec_tasks).resolve()) if getattr(args, "spec_tasks", None) and Path(args.spec_tasks).exists() else {}
    latest_run_data = read_json(Path(args.run).resolve()) if getattr(args, "run", None) and Path(args.run).exists() else {}
    code_review_data = read_json(Path(args.code_review).resolve()) if getattr(args, "code_review", None) and Path(args.code_review).exists() else {}
    result = check_evidence_integrity(cases_data, spec_tasks_data, latest_run_data, code_review_data)
    if args.output:
        write_json(Path(args.output).resolve(), result)
    print(f"证据完整性门禁：{result['status']}（用例 {result['summary']['testCasesTotal']} / task {result['summary']['specTasksTotal']} / run {result['summary']['runCasesTotal']} / 未匹配 {result['summary']['unmatchedLogs']}）")
    for finding in result.get("findings", [])[:20]:
        print(f"  - {finding.get('type')}: {finding.get('message', '')}")
    if result["status"] != "passed":
        raise SystemExit(1)


# 「流程没走完」（产物缺失、证据链断裂）与「走完但不达标」是两回事，最终档位由顶层
# decision 一个字段给出。findings 只描述事实——原先每条 finding 各盖一个 decision 章，
# 读起来 Incomplete 与 Not Ready 混排，要确定最终落哪一档还得回头查文档。
_INCOMPLETE_FINDING_TYPES = frozenset({
    "run-summary-empty",
    "unmatched-run-logs",
    "task-without-run-evidence",
    "invalid-code-review-scope",
    "invalid-case-file",
    "invalid-task",
    "invalid-tasks",
    "invalid-task-layer",
})


def _is_incomplete_finding(finding: dict[str, Any]) -> bool:
    """这条 finding 属于「缺东西」还是「东西在但不达标」。"""
    ftype = str(finding.get("type", ""))
    return ftype.endswith("missing") or ftype in _INCOMPLETE_FINDING_TYPES


def assert_readiness_data(
    completion_data: dict[str, Any] | None,
    code_review_data: dict[str, Any] | None,
    *,
    report_path: Path | None = None,
    report_freshness_data: dict[str, Any] | None = None,
    evidence_integrity_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    completion_data = completion_data or {}
    code_review_data = code_review_data or {}
    report_freshness_data = report_freshness_data or {}
    completion_status = normalize_task_status(completion_data.get("status"))
    completion_decision = normalize_task_status(completion_data.get("decision"))
    if not completion_data:
        findings.append({"type": "completion-check-missing", "severity": "fail"})
    elif completion_status != "passed":
        findings.append({"type": "completion-check-not-passed", "severity": "fail", "status": completion_status, "completionDecision": completion_decision})
    elif completion_decision in {"complete-not-ready", "complete_not_ready"}:
        findings.append({"type": "completion-has-failed-business-task", "severity": "fail"})
    review_check = assert_code_review_data(code_review_data) if code_review_data else {
        "status": "failed",
        "summary": {"findings": 0, "blockingFindings": 1},
        "findings": [{"type": "code-review-missing", "severity": "fail"}],
    }
    if review_check.get("status") != "passed":
        for finding in review_check.get("findings", []):
            findings.append(dict(finding))
    freshness_status = normalize_task_status(report_freshness_data.get("status"))
    freshness_decision = normalize_task_status(report_freshness_data.get("decision"))
    if not report_freshness_data:
        findings.append({"type": "report-freshness-missing", "severity": "fail"})
    elif freshness_status != "passed":
        findings.append(
            {
                "type": "report-freshness-not-passed",
                "severity": "fail",
                "status": freshness_status,
                "decision": freshness_decision or "Not Ready",
            }
        )
    if report_path and not report_path.exists():
        findings.append({"type": "html-report-missing", "severity": "fail", "path": str(report_path)})
    # 证据完整性：执行证据链断裂 → Incomplete，不允许仅因报告新鲜就判可信
    evidence_integrity_data = evidence_integrity_data or {}
    evidence_status = normalize_task_status(evidence_integrity_data.get("status"))
    if not evidence_integrity_data:
        findings.append({"type": "evidence-integrity-missing", "severity": "fail"})
    elif evidence_status != "passed":
        for finding in evidence_integrity_data.get("findings", []):
            findings.append(dict(finding))
    blocking_code = any(item.get("type") == "blocking-code-review-finding" for item in findings)
    incomplete = any(_is_incomplete_finding(item) for item in findings)
    failed = any(item.get("severity") == "fail" for item in findings)
    if not failed:
        decision = "Conditionally Ready" if completion_decision in {"complete-with-allowed-gaps", "complete_with_allowed_gaps"} else "Ready"
        status = "warn" if decision == "Conditionally Ready" else "passed"
    else:
        decision = "Not Ready" if blocking_code or completion_decision in {"complete-not-ready", "complete_not_ready"} else "Incomplete" if incomplete else "Not Ready"
        status = "failed"
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": status,
        "decision": decision,
        "summary": {
            "completionStatus": completion_status or "missing",
            "completionDecision": completion_decision or "missing",
            "codeReviewStatus": review_check.get("status", "missing"),
            "reportFreshnessStatus": freshness_status or "missing",
            "evidenceIntegrityStatus": evidence_status or "missing",
            "blockingCodeReviewFindings": review_check.get("summary", {}).get("blockingFindings", 0),
            "reportExists": bool(report_path.exists()) if report_path else None,
        },
        "findings": findings,
    }


def assert_readiness(args: argparse.Namespace) -> None:
    completion_path = Path(args.completion_check).resolve()
    code_review_path = Path(args.code_review).resolve()
    report_path = Path(args.report).resolve() if args.report else None
    freshness_path = Path(args.report_freshness_check).resolve() if getattr(args, "report_freshness_check", None) else None
    evidence_path = Path(args.evidence_integrity_check).resolve() if getattr(args, "evidence_integrity_check", None) else None
    completion_data = read_json(completion_path) if completion_path.exists() else {}
    code_review_data = read_json(code_review_path) if code_review_path.exists() else {}
    freshness_data = read_json(freshness_path) if freshness_path and freshness_path.exists() else {}
    evidence_data = read_json(evidence_path) if evidence_path and evidence_path.exists() else {}
    result = assert_readiness_data(
        completion_data,
        code_review_data,
        report_path=report_path,
        report_freshness_data=freshness_data,
        evidence_integrity_data=evidence_data,
    )
    if args.output:
        write_json(Path(args.output).resolve(), result)
    print(f"就绪门禁：{result['status']}（{zh_readiness_label(result['decision'])}）")
    for finding in result.get("findings", [])[:50]:
        print(f"{finding.get('severity', 'warn').upper()}: {finding.get('type')} {json.dumps(finding, ensure_ascii=False)}")
    if result["status"] == "failed":
        raise SystemExit(1)


def assert_report_freshness(args: argparse.Namespace) -> None:
    report_path = Path(args.report).resolve()
    source_paths = collect_report_source_paths(args)
    findings: list[dict[str, Any]] = []
    if not report_path.exists():
        findings.append({"type": "report-missing", "severity": "fail", "path": str(report_path)})
        result = {
            "status": "failed",
            "decision": "Incomplete",
            "reportPath": str(report_path),
            "generatedAt": None,
            "reportMtime": None,
            "sourceFiles": [],
            "staleSources": [],
            "findings": findings,
        }
    else:
        report_mtime = report_path.stat().st_mtime
        source_files: list[dict[str, Any]] = []
        stale_sources: list[dict[str, Any]] = []
        for label, path in source_paths:
            entry = {
                "label": label,
                "path": str(path),
                "exists": path.exists(),
                "mtime": format_utc_timestamp(path.stat().st_mtime if path.exists() else None),
            }
            source_files.append(entry)
            if not path.exists():
                if label in OPTIONAL_REPORT_SOURCES:
                    # 按需产出的源缺失 = 本轮走的是复用路径，不是报告陈旧
                    entry["optional"] = True
                    continue
                findings.append({"type": "source-missing", "severity": "fail", "source": label, "path": str(path)})
                continue
            if path.stat().st_mtime > report_mtime + 1e-9:
                stale_entry = {"label": label, "path": str(path), "mtime": entry["mtime"]}
                stale_sources.append(stale_entry)
                findings.append({"type": "stale-source", "severity": "fail", **stale_entry})
        result = {
            "status": "passed" if not findings else "failed",
            "decision": "Ready" if not findings else "Not Ready",
            "reportPath": str(report_path),
            "generatedAt": format_utc_timestamp(report_mtime),
            "reportMtime": format_utc_timestamp(report_mtime),
            "sourceFiles": source_files,
            "staleSources": stale_sources,
            "findings": findings,
        }
    if args.output:
        write_json(Path(args.output).resolve(), result)
    print(f"报告时效检查：{result['status']}；报告={result['reportPath']}")
    for finding in result.get("findings", [])[:50]:
        print(f"{finding.get('severity', 'warn').upper()}: {finding.get('type')} {json.dumps(finding, ensure_ascii=False)}")
    if result["status"] != "passed":
        raise SystemExit(1)


def separate_technical_checks(args: argparse.Namespace) -> None:
    path = Path(args.cases).resolve()
    data = read_json(path)
    errors, normalized = validate_cases_data(data, mutate=True)
    technical_errors = [
        error for error in errors if "looks like a technical quality gate" in error
    ]
    blocking_errors = [error for error in errors if error not in technical_errors]
    if blocking_errors:
        raise QaAgentError("Invalid cases: " + "; ".join(blocking_errors))

    remaining_cases = []
    moved_quality = []
    moved_environment = []
    for case in normalized.get("cases", []):
        if looks_like_quality_gate_case(case):
            if technical_case_kind(case) == "environment":
                moved_environment.append(technical_case_to_environment_check(case))
            else:
                moved_quality.append(technical_case_to_quality_gate(case))
        else:
            remaining_cases.append(case)

    normalized["cases"] = remaining_cases
    normalized.setdefault("qualityGates", []).extend(moved_quality)
    normalized.setdefault("environmentChecks", []).extend(moved_environment)
    output = Path(args.output).resolve() if args.output else path
    write_json(output, normalized)
    print(
        f"Separated {len(moved_quality)} quality gates and {len(moved_environment)} environment checks; "
        f"{len(remaining_cases)} business cases remain: {output}"
    )


def chat_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return base_url + "/chat/completions"
    return base_url + "/v1/chat/completions"


def parse_model_list(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_MODELS
    return [item.strip() for item in raw.split(",") if item.strip()]


DEFAULT_LLM_CONFIG_PATH = ".qa-agent/config/qa-agent.config.yaml"
DEFAULT_LLM_TIMEOUT_SECONDS = 300


def resolve_llm_settings(args: argparse.Namespace) -> dict[str, Any]:
    """解析多模型审查的 LLM 设置：CLI 参数 > 配置文件 llm 段 > 内置默认值。

    配置文件的 llm 段此前只存在于模板里，代码从没读过——用户把 llm.models 改成
    自己的模型名，实际仍走三个内置默认值，静默无效。这里把它接上，并新增 stream。

    stream 默认 True：流式是超集（SSE 聚合已实现），且多数网关默认或只支持流式；
    端点不支持时 call_model 会自动降级一次非流式。

    对应的 CLI 参数默认值因此必须是 None 而不是具体值——否则「没传参」和
    「传了默认值」无法区分，配置段永远没机会生效。
    """
    explicit = getattr(args, "config", None)
    config_path = Path(explicit) if explicit else Path(DEFAULT_LLM_CONFIG_PATH)
    cfg: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw_llm = load_config(config_path).get("llm")
            # 只有映射才认。写成列表/字符串/标量都当没配——配置坏了应回退默认值，
            # 而不是让多模型审查直接崩。
            cfg = raw_llm if isinstance(raw_llm, dict) else {}
        except Exception:  # noqa: BLE001 - 解析失败同样回退
            cfg = {}

    def pick(cli_value: Any, cfg_key: str, default: Any) -> Any:
        if cli_value not in (None, "", []):
            return cli_value
        value = cfg.get(cfg_key)
        return default if value in (None, "") else value

    models_raw = pick(getattr(args, "models", None), "models", None)
    if isinstance(models_raw, list):
        models = [str(m).strip() for m in models_raw if str(m).strip()] or list(DEFAULT_MODELS)
    else:
        models = parse_model_list(models_raw)

    return {
        "models": models,
        "baseUrlEnv": str(pick(getattr(args, "base_url_env", None), "baseUrlEnv", DEFAULT_BASE_URL_ENV)),
        "apiKeyEnv": str(pick(getattr(args, "api_key_env", None), "apiKeyEnv", DEFAULT_API_KEY_ENV)),
        "baseUrl": str(pick(getattr(args, "base_url", None), "defaultBaseUrl", DEFAULT_BASE_URL)),
        "timeout": int(pick(getattr(args, "timeout", None), "timeoutSeconds", DEFAULT_LLM_TIMEOUT_SECONDS)),
        "stream": bool(pick(getattr(args, "stream", None), "stream", True)),
    }


def model_review_prompt(cases: dict[str, Any], context: dict[str, Any] | None) -> str:
    # 只传用例核心字段，减少 token 消耗
    slim_cases = [
        {k: c[k] for k in ["id", "priority", "title", "layer", "steps", "expected",
                             "businessAssertions", "source", "risk", "status"]
         if k in c}
        for c in cases.get("cases", [])
    ]
    payload = {
        "task": "Review these QA business test cases. Return JSON only. Find missing high-risk business operation paths, wrong assertions, duplicates, priority errors, unclear or unverifiable expected results, and any tooling/environment checks incorrectly written as test cases.",
        "businessCaseRule": "A valid case must describe business behavior: actor, operation path, business state, action, and observable business assertion. Maven/Vitest/node:test/Playwright install/compile/build/test-list checks are quality gates or environment checks, not test cases.",
        "languageRule": "Use Simplified Chinese for all human-readable review text and test-case narrative suggestions. Keep product/domain terms, code identifiers, API paths, enum values, commands, URLs, file paths, model names, and raw source evidence unchanged.",
        "requiredOutput": {
            "model": "model-name",
            "summary": "short assessment",
            "findings": [
                {
                    "id": "MR-001",
                    "severity": "P0|P1|P2|P3",
                    "caseIds": ["TC-P0-001"],
                    "problem": "what is wrong or missing",
                    "impact": "why it matters",
                    "recommendation": "specific change",
                }
            ],
            "missingCases": [],
            "duplicateCases": [],
            "priorityCorrections": [],
            "confidence": 0.0,
        },
        "openQuestions": cases.get("openQuestions", []),
        "testCases": slim_cases,
    }
    return json.dumps(payload, ensure_ascii=False)


_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


def parse_sse_content(raw: str) -> tuple[str, str | None]:
    """从 SSE 流文本聚合 delta.content，返回 (拼接内容, finish_reason)。"""
    parts: list[str] = []
    finish_reason: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        piece = (choice.get("delta") or {}).get("content")
        if piece:
            parts.append(piece)
        if choice.get("finish_reason"):
            finish_reason = choice.get("finish_reason")
    return "".join(parts), finish_reason


def _looks_like_stream_unsupported(detail: str) -> bool:
    """HTTP 错误体是否在说「这个端点不支持流式」。

    用于流式请求失败后的一次性降级。只在错误体同时提到 stream 和
    否定词时才判定，避免把无关的 400 也当成降级信号。
    """
    low = (detail or "").lower()
    if "stream" not in low:
        return False
    return any(hint in low for hint in ("not supported", "unsupported", "must be false",
                                        "must be set to false", "not allowed", "invalid"))


def call_model(
    model: str,
    prompt: str,
    base_url: str,
    api_key: str,
    timeout: int,
    *,
    stream: bool = True,
    max_retries: int = 2,
    backoff_seconds: float = 2.0,
) -> dict[str, Any]:
    """调用 OpenAI 兼容的 /chat/completions。

    stream 默认 True。流式是这里的「超集」：SSE 聚合（parse_sse_content）本就
    实现了，非流式反而是需要额外分支的那条路；而当下多数网关默认或只支持流式。
    此前是靠一个硬编码的模型集合（STREAM_REQUIRED_MODELS）去猜谁需要流式，
    换一个同样强制流式的网关就会撞 400，且用户无法自己打开。

    碰到确实不支持流式的端点时自动降级重试一次非流式，用户不必预先知道该配什么。
    """
    messages = [
        {
            "role": "system",
            "content": "You are a senior QA reviewer. Return strict JSON only. Use Simplified Chinese for human-readable review text except product/domain terms and technical identifiers.",
        },
        {"role": "user", "content": prompt},
    ]

    def _body(use_stream: bool) -> bytes:
        return json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 16000,
            "stream": use_stream,
        }).encode("utf-8")

    use_stream = stream
    downgraded = False
    attempts = 0
    last_error: dict[str, Any] | None = None
    while attempts <= max_retries:
        attempts += 1
        request = urllib.request.Request(
            chat_url(base_url),
            data=_body(use_stream),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-4000:]
            # 一次性降级：端点不支持流式时改用非流式重试。降级不消耗重试次数——
            # 它是换了一条请求形态，不是同一请求的重复。
            if use_stream and not downgraded and _looks_like_stream_unsupported(detail):
                use_stream = False
                downgraded = True
                attempts -= 1
                continue
            last_error = {"model": model, "ok": False, "error": f"HTTP {exc.code}: {detail}", "attempts": attempts}
            if exc.code in _RETRYABLE_HTTP_STATUS and attempts <= max_retries:
                time.sleep(backoff_seconds * (2 ** (attempts - 1)))
                continue
            return last_error
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # noqa: BLE001 - network-level failures, retryable.
            last_error = {"model": model, "ok": False, "error": str(exc), "attempts": attempts}
            if attempts <= max_retries:
                time.sleep(backoff_seconds * (2 ** (attempts - 1)))
                continue
            return last_error
        except Exception as exc:  # noqa: BLE001 - CLI should preserve external error.
            return {"model": model, "ok": False, "error": str(exc), "attempts": attempts}
        try:
            if use_stream:
                content, finish_reason = parse_sse_content(raw)
            else:
                envelope = json.loads(raw)
                choice = envelope["choices"][0]
                content = choice["message"].get("content")
                finish_reason = choice.get("finish_reason")
        except Exception as exc:  # noqa: BLE001 - malformed envelope, not a transport issue; do not retry.
            return {"model": model, "ok": False, "error": f"Invalid response envelope: {exc}", "raw": raw[-4000:], "attempts": attempts}
        if not content:
            if finish_reason == "length":
                return {"model": model, "ok": False, "error": f"输出被截断（finish_reason=length），需增大 max_tokens 或精简 prompt", "raw": raw[-4000:], "attempts": attempts}
            return {"model": model, "ok": False, "error": "Model returned empty content", "raw": raw[-4000:], "attempts": attempts}
        parsed = parse_json_from_text(content)
        if parsed is None:
            return {"model": model, "ok": False, "error": "Model did not return JSON", "raw": content[-4000:], "attempts": attempts}
        parsed.setdefault("model", model)
        return {"model": model, "ok": True, "review": parsed, "attempts": attempts}
    return last_error or {"model": model, "ok": False, "error": "unknown failure", "attempts": attempts}


def parse_json_from_text(text: str | None) -> Any | None:
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def synthesize_reviews(results: list[dict[str, Any]]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    failed = []
    for result in results:
        if not result.get("ok"):
            failed.append({"model": result.get("model"), "error": result.get("error")})
            continue
        review = result.get("review", {})
        for finding in review.get("findings", []) or []:
            item = dict(finding)
            item["model"] = result.get("model")
            findings.append(item)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        key = re.sub(r"\s+", " ", (finding.get("problem") or finding.get("recommendation") or "")[:120].lower())
        grouped.setdefault(key, []).append(finding)
    accepted_candidates = []
    for key, items in grouped.items():
        severities = [item.get("severity", "P2") for item in items]
        accepted_candidates.append(
            {
                "problemKey": key,
                "models": sorted({item.get("model", "") for item in items}),
                "count": len(items),
                "maxSeverity": min(severities, key=lambda sev: ["P0", "P1", "P2", "P3"].index(sev) if sev in CASE_PRIORITIES else 2),
                "items": items,
            }
        )
    accepted_candidates.sort(key=lambda item: (["P0", "P1", "P2", "P3"].index(item["maxSeverity"]), -item["count"]))
    models_succeeded = [result.get("model") for result in results if result.get("ok")]
    review: dict[str, Any] = {
        "generatedAt": utc_now(),
        # 全部模型失败时必须标 skipped。否则 findings: [] 有两种读法——「审过了，没问题」
        # 和「根本没审」——而两者的产物长得一模一样。使用者实测时三个模型全失败，
        # 产物照常写出，他差点按「已审查且干净」理解。
        "status": "reviewed" if models_succeeded else "skipped",
        "modelsRequested": [result.get("model") for result in results],
        "modelsSucceeded": models_succeeded,
        "modelsFailed": failed,
        "findings": findings,
        "synthesis": accepted_candidates,
    }
    if not models_succeeded:
        review["note"] = (
            "没有任何模型成功执行，本轮未做交叉审查——findings 为空不代表审查通过。"
            "常见原因：未配置 QA_AGENT_LLM_API_KEY（该阶段本就是可选的增强，跳过不影响其余流程）。"
        )
    return review


def review_cases(args: argparse.Namespace) -> None:
    cases = read_json(Path(args.cases).resolve())
    context = read_json(Path(args.context).resolve()) if args.context else None
    prompt = model_review_prompt(cases, context)
    llm = resolve_llm_settings(args)
    models = llm["models"]
    if args.dry_run:
        results = [
            {
                "model": model,
                "ok": True,
                "review": {
                    "model": model,
                    "summary": "dry-run 评审占位：未真实调用模型",
                    "findings": [],
                    "missingCases": [],
                    "duplicateCases": [],
                    "priorityCorrections": [],
                    "confidence": 0.0,
                },
            }
            for model in models
        ]
    else:
        api_key = os.environ.get(llm["apiKeyEnv"])
        base_url = os.environ.get(llm["baseUrlEnv"]) or llm["baseUrl"]
        if not api_key:
            # 缺 key 不中断流程：写一个「全部模型失败」的评审结果，让后续阶段继续。
            results = [
                {"model": model, "ok": False, "error": f"Missing API key env var: {llm['apiKeyEnv']}"}
                for model in models
            ]
        elif not base_url:
            # 同样不中断：本工具不内置任何默认网关，地址必须由用户显式配置。
            results = [
                {"model": model, "ok": False, "error": f"Missing base URL env var: {llm['baseUrlEnv']}"}
                for model in models
            ]
        else:
            # 整体超时 = 单模型 socket 超时 + 30s 余量（并发调度/结果收集）
            overall_timeout = llm["timeout"] + 30
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(models))
            try:
                futures = [
                    executor.submit(
                        call_model,
                        model,
                        prompt,
                        base_url,
                        api_key,
                        llm["timeout"],
                        stream=llm["stream"],
                        max_retries=getattr(args, "max_retries", 2),
                        backoff_seconds=getattr(args, "retry_backoff_seconds", 2.0),
                    )
                    for model in models
                ]
                done, _not_done = concurrent.futures.wait(futures, timeout=overall_timeout)
                results = []
                for model, fut in zip(models, futures):
                    if fut not in done:
                        fut.cancel()
                        results.append({"model": model, "ok": False, "error": f"整体超时（>{overall_timeout}s）未完成"})
                        continue
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # noqa: BLE001 - 单个模型异常不应中断评审
                        results.append({"model": model, "ok": False, "error": f"评审异常: {exc}"})
            finally:
                # wait=False：不等待未完成线程，避免整体超时被 with 块的隐式 shutdown(wait=True) 反噬
                executor.shutdown(wait=False, cancel_futures=True)
    output = synthesize_reviews(results)
    output["rawResults"] = results
    write_json(Path(args.output).resolve(), output)
    print(f"已写入多模型评审：{Path(args.output).resolve()}")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise QaAgentError(f"Config not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data or {}
    except Exception:
        return parse_simple_yaml(text)


def _is_quoted(value: str) -> bool:
    """YAML 标量是否被成对引号包裹（单引号、双引号都算）。"""
    return len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}


def parse_scalar(value: str) -> Any:
    """解析一个 YAML 标量。

    历史缺陷：只剥双引号、不剥单引号。而 `yaml_quote()` 生成配置时用 json.dumps
    产出双引号——生成端和解析端不对称，使用者手写 `'cd x && mvn test'` 时引号被
    原样留下，传给 cmd.exe 就成了 `''cd' 不是内部或外部命令`。
    单引号还会连带影响列表项判定（见 parse_simple_yaml 里对 `:` 的处理）。
    """
    value = value.strip()
    if value in {"[]", ""}:
        return []
    if value in {"true", "false"}:
        return value == "true"
    if _is_quoted(value):
        if value[0] == '"':
            # yaml_quote() 用 json.dumps 生成，转义规则即 JSON 的
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value[1:-1]
        # YAML 单引号：内部写 '' 表示一个字面单引号
        return value[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        is_list = index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- ")
        container: Any = [] if is_list else {}
        while index < len(lines):
            line_indent, line = lines[index]
            if line_indent < indent:
                break
            if line_indent > indent:
                index += 1
                continue
            if is_list:
                if not line.startswith("- "):
                    break
                item = line[2:].strip()
                if not item:
                    child, index = parse_block(index + 1, next_indent(index + 1, indent))
                    container.append(child)
                elif ":" in item and not _is_quoted(item):
                    key, value = item.split(":", 1)
                    obj: dict[str, Any] = {key.strip(): parse_scalar(value)}
                    index += 1
                    while index < len(lines) and lines[index][0] > line_indent:
                        child_indent, child_line = lines[index]
                        if child_indent <= line_indent or ":" not in child_line:
                            break
                        child_key, child_value = child_line.split(":", 1)
                        child_key = child_key.strip()
                        child_value = child_value.strip()
                        if child_value:
                            obj[child_key] = parse_scalar(child_value)
                            index += 1
                        else:
                            child, index = parse_block(index + 1, next_indent(index + 1, child_indent))
                            obj[child_key] = child
                    container.append(obj)
                else:
                    container.append(parse_scalar(item))
                    index += 1
            else:
                if line.startswith("- ") or ":" not in line:
                    break
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if value:
                    container[key] = parse_scalar(value)
                    index += 1
                else:
                    child_indent = next_indent(index + 1, indent)
                    if child_indent <= indent:
                        container[key] = {}
                        index += 1
                    else:
                        child, index = parse_block(index + 1, child_indent)
                        container[key] = child
        return container, index

    def next_indent(index: int, fallback: int) -> int:
        if index >= len(lines):
            return fallback
        return lines[index][0]

    parsed, _ = parse_block(0, lines[0][0] if lines else 0)
    return parsed if isinstance(parsed, dict) else {}


QA_LAYOUT_TRACKED_DIRS = ["config", "cases", "profiles", "risk-rules", "fixtures", "knowledge", "reports"]
QA_LAYOUT_RUNTIME_DIRS = ["current", "runs", "tmp", "local"]


def init_layout(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    qa_root = repo / ".qa-agent"
    for dirname in [*QA_LAYOUT_TRACKED_DIRS, *QA_LAYOUT_RUNTIME_DIRS]:
        directory = qa_root / dirname
        directory.mkdir(parents=True, exist_ok=True)
        if dirname in QA_LAYOUT_TRACKED_DIRS:
            keep = directory / ".gitkeep"
            if not keep.exists():
                keep.write_text("", encoding="utf-8")
    gitignore = qa_root / ".gitignore"
    if getattr(args, "write_gitignore", False) or not gitignore.exists():
        gitignore.write_text(
            "\n".join(
                [
                    "# QA Agent runtime artifacts",
                    "/current/",
                    "/runs/",
                    "/tmp/",
                    "/local/",
                    "/*.json",
                    "/*.html",
                    "",
                    "# Long-lived QA Agent knowledge (team-shared, git-tracked)",
                    "!/config/",
                    "!/cases/",
                    "!/profiles/",
                    "!/risk-rules/",
                    "!/fixtures/",
                    "!/knowledge/",
                    "!/reports/",
                    "!/.gitignore",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    root_gitignore = repo / ".gitignore"
    snippet = [
        "# QA Agent tracked knowledge, ignored runtime",
        ".qa-agent/*",
        "!.qa-agent/.gitignore",
        "!.qa-agent/config/",
        "!.qa-agent/config/**",
        "!.qa-agent/cases/",
        "!.qa-agent/cases/**",
        "!.qa-agent/profiles/",
        "!.qa-agent/profiles/**",
        "!.qa-agent/risk-rules/",
        "!.qa-agent/risk-rules/**",
        "!.qa-agent/fixtures/",
        "!.qa-agent/fixtures/**/*.example.*",
        "!.qa-agent/reports/",
        "!.qa-agent/reports/**",
    ]
    if getattr(args, "root_gitignore", False):
        existing = root_gitignore.read_text(encoding="utf-8", errors="replace") if root_gitignore.exists() else ""
        if ".qa-agent/*" not in existing:
            root_gitignore.write_text(existing.rstrip() + "\n\n" + "\n".join(snippet) + "\n", encoding="utf-8")
    result = {
        "version": "1.0",
        "generatedAt": utc_now(),
        "qaRoot": str(qa_root),
        "trackedDirs": QA_LAYOUT_TRACKED_DIRS,
        "runtimeDirs": QA_LAYOUT_RUNTIME_DIRS,
        "gitignore": str(gitignore),
        "rootGitignoreUpdated": bool(getattr(args, "root_gitignore", False)),
    }
    if getattr(args, "json", None):
        write_json(Path(args.json).resolve(), result)
    print(f"已初始化 QA Agent 目录：{qa_root}")


def seed_playwright_templates(repo: Path, *, force: bool = False) -> dict[str, str]:
    written: dict[str, str] = {}
    for rel_path, source in PLAYWRIGHT_TEMPLATE_ASSET_MAP.items():
        target = repo / rel_path
        if not source.exists():
            continue
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written[rel_path] = str(target)
    return written


def seed_e2e_lib(repo: Path, *, force: bool = False) -> str:
    """将 E2E fixture 模板部署到项目的 tests/e2e/lib/e2e-fixture.js。仅首次写入。"""
    source = ASSETS / "e2e-lib-template.js"
    target = repo / "tests" / "e2e" / "lib" / "e2e-fixture.js"
    if target.exists() and not force:
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return str(target)


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_local_env(repo: Path, *, override: bool = False) -> dict[str, str]:
    """三层加载：config/env.shared（团队共享，git 跟踪）→ local/.env（密钥/覆盖）→ os.environ"""
    values: dict[str, str] = {}
    # 第一层：团队共享值（不含密钥）
    shared = repo / ".qa-agent" / "config" / "env.shared"
    if shared.exists():
        values.update(parse_dotenv(shared))
    # 第二层：本地密钥和覆盖值
    env_file = repo / ".qa-agent" / "local" / ".env"
    if env_file.exists():
        values.update(parse_dotenv(env_file))
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def qa_env_value(repo: Path, key: str) -> str:
    if os.environ.get(key):
        return str(os.environ.get(key))
    # local/.env 优先覆盖 config/env.shared；空值视为未覆盖，回退到 env.shared
    local = parse_dotenv(repo / ".qa-agent" / "local" / ".env")
    if local.get(key):
        return local[key]
    shared = parse_dotenv(repo / ".qa-agent" / "config" / "env.shared")
    return shared.get(key, "")


def write_text_if_needed(path: Path, content: str, *, force: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def write_json_if_needed(path: Path, data: Any, *, force: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return False
    write_json(path, data)
    return True


def infer_service_examples(repo: Path) -> list[dict[str, Any]]:
    """自动推导项目中的服务配置，包括启动命令和就绪信号。

    在仓库子目录中搜索 pom.xml / package.json，按框架类型识别前后端服务，
    填充 dir / startCmd / readySignal / healthUrl 字段。未找到时回退到纯模板（无 startCmd）。
    """
    services: list[dict[str, Any]] = []
    project_dirs = _find_project_dirs(repo)

    # 后端: 找包含 pom.xml 且有 spring-boot-maven-plugin 的目录
    for proj in project_dirs:
        pom = proj / "pom.xml"
        if not pom.exists():
            continue
        full_text = ""
        try:
            full_text = pom.read_text(encoding="utf-8")
        except Exception:
            pass
        if "spring-boot-maven-plugin" not in full_text:
            continue
        rel = str(proj.relative_to(repo)).replace("\\", "/")
        service_id = _derive_service_id(proj, default="api")
        # 从 application.yml 推导端口
        port = _find_app_port(proj, default=8080)
        health_path = _find_health_path(proj)
        services.append({
            "id": service_id,
            "baseUrlEnv": "QA_API_BASE_URL",
            "required": False,
            "scope": ["api", "integration"],
            "dir": rel,
            "startCmd": "mvn spring-boot:run",
            "readySignal": "Started",
            "defaultUrl": f"http://127.0.0.1:{port}",
        })

    # 前端: 找包含 package.json 且有 next dev / vite dev 等脚本的目录
    for proj in project_dirs:
        pkg = proj / "package.json"
        if not pkg.exists():
            continue
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:
            continue
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        scripts = data.get("scripts", {})
        is_next = "next" in deps
        is_react = "react" in deps or "vite" in deps
        if not (is_next or is_react):
            continue
        rel = str(proj.relative_to(repo)).replace("\\", "/")
        # 确定服务 ID
        lower_rel = rel.lower()
        if "h5" in lower_rel or "web" in lower_rel or "front" in lower_rel:
            service_id = "web"
        elif "admin" in lower_rel:
            service_id = "admin"
        else:
            service_id = "web"
        # 确定启动命令
        start_cmd = ""
        ready_signal = ""
        if "dev" in scripts:
            start_cmd = "npm run dev"
        elif "start" in scripts:
            start_cmd = "npm run start"
        if is_next:
            ready_signal = "Ready in"
        elif is_react:
            ready_signal = "Local:"
        port = _find_frontend_port(proj, scripts, data, default=3000)
        services.append({
            "id": service_id,
            "baseUrlEnv": "QA_WEB_BASE_URL",
            "required": True,
            "scope": ["e2e"],
            "dir": rel,
            "startCmd": start_cmd,
            "readySignal": ready_signal,
            "defaultUrl": f"http://127.0.0.1:{port}",
        })

    if services:
        return services
    # 回退: 空白模板
    return [
        {"id": "web", "baseUrlEnv": "QA_WEB_BASE_URL",
         "required": True, "scope": ["e2e"], "dir": "", "startCmd": "", "readySignal": "", "defaultUrl": "http://127.0.0.1:3000"},
        {"id": "api", "baseUrlEnv": "QA_API_BASE_URL",
         "required": False, "scope": ["api", "integration"], "dir": "", "startCmd": "", "readySignal": "", "defaultUrl": "http://127.0.0.1:8080"},
    ]


def _find_project_dirs(repo: Path) -> list[Path]:
    """找到仓库中所有可能是独立项目的子目录（含 pom.xml 或 package.json）。"""
    dirs: list[Path] = []
    for entry in sorted(repo.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == "node_modules":
            continue
        if (entry / "pom.xml").exists() or (entry / "package.json").exists():
            dirs.append(entry)
    return dirs


def _find_app_port(proj: Path, default: int = 8080) -> int:
    """从 Spring Boot application*.yml 中读取 server.port。仅匹配显式的 server.port 配置行。"""
    resources = proj / "src" / "main" / "resources"
    for yml in sorted(resources.glob("application*.yml")):
        try:
            for line in yml.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if re.match(r"^server\.port\s*:\s*\d+", stripped):
                    return int(stripped.split(":")[-1].strip())
        except Exception:
            continue
    return default


def _find_health_path(proj: Path) -> str:
    """从 Spring Boot application*.yml 推断 health 检查 URL 后缀。
    优先匹配 Tomcat 日志中的 context path，其次读取配置文件。
    默认返回 /api/doc.html（Knife4j 文档页）。"""
    resources = proj / "src" / "main" / "resources"
    for yml in sorted(resources.glob("application*.yml")):
        try:
            for line in yml.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if re.match(r"server\.servlet\.context-path\s*:", stripped):
                    cp = stripped.split(":")[-1].strip().strip("'\"")
                    if not cp.startswith("/"):
                        cp = "/" + cp
                    return (cp + "/doc.html") if cp != "/" else "/doc.html"
        except Exception:
            continue
    return "/api/doc.html"


def _derive_service_id(proj: Path, default: str = "api") -> str:
    """从目录名推断服务 ID。匹配常见命名约定。"""
    name = proj.name.lower()
    if any(kw in name for kw in ["backend", "api", "server"]):
        return "api"
    if "admin" in name:
        return "admin"
    if any(kw in name for kw in ["h5", "frontend", "web", "www", "client"]):
        return "web"
    return default


def _find_frontend_port(proj: Path, scripts: dict[str, str], pkg_data: dict[str, Any], default: int = 3000) -> int:
    """从 package.json scripts 或 .env.development 中读取前端开发服务器端口。"""
    for script_val in scripts.values():
        import re as _re
        m = _re.search(r"(?:-p|--port)\s+(\d+)", script_val)
        if m:
            return int(m.group(1))
    env_dev = proj / ".env.development"
    if env_dev.exists():
        try:
            for line in env_dev.read_text(encoding="utf-8").splitlines():
                if "PORT" in line and "=" in line:
                    port_str = line.split("=")[-1].strip().strip("'\"")
                    return int(port_str)
        except Exception:
            pass
    return default


def load_accounts_config(repo: Path) -> dict[str, Any]:
    """账号合并进 env + 约定：默认一个 user-default 账号，凭证读 QA_USER_*。"""
    return {
        "version": "1.0",
        "accounts": [
            {
                "id": "user-default",
                "role": "user",
                "scopes": ["web", "e2e"],
                "usernameEnv": "QA_USER_USERNAME",
                "passwordEnv": "QA_USER_PASSWORD",
            }
        ],
    }


def self_invocation() -> str:
    """本 CLI 可被直接复制的调用形式。

    用于工具输出里的「下一步」提示——提示里给出的命令必须是能跑通的。
    之前有四处硬编码成 `qa_agent.py init-project --repo .`：缺 python 前缀、
    缺路径，用户照抄得到的是 command not found。
    """
    explicit = os.environ.get("QA_AGENT_CLI")
    if explicit:
        return explicit
    return f'python "{Path(__file__).resolve()}"'


def load_services_config(repo: Path) -> dict[str, Any]:
    """服务配置：local/services.local.json（私有）优先 → config/services.json（公有）兜底。"""
    return _load_layered_config(repo, "services")


# 启动命令的首个 token → 它真正依赖的运行时检查名
_RUNNER_RUNTIME = {
    "mvn": "maven", "mvn.cmd": "maven", "mvnw": "maven", "mvnw.cmd": "maven",
    "npm": "node", "npm.cmd": "node", "npx": "node", "npx.cmd": "node",
    "node": "node", "node.exe": "node",
    "yarn": "node", "yarn.cmd": "node", "pnpm": "node", "pnpm.cmd": "node",
}


def service_runtime_requirements(repo: Path) -> set[str]:
    """从服务启动命令推导真正需要的运行时（"maven" / "node"）。

    doctor 曾把 maven / node 一律标成可选（required=False），但 ``services.json`` 的
    startCmd 就写着 `mvn spring-boot:run` / `npm run dev`——被标「可选」的 maven 缺失
    会直接让后端服务起不来，doctor 却放行。必需性应当从实际依赖推导，不是静态清单。
    """
    needed: set[str] = set()
    try:
        services = load_services_config(repo).get("services", [])
    except Exception:  # noqa: BLE001 - 配置读不出来不该让 doctor 自己崩掉
        return needed
    for service in services if isinstance(services, list) else []:
        if not isinstance(service, dict):
            continue
        start_cmd = str(service.get("startCmd", "") or "").strip()
        if not start_cmd:
            continue
        head = Path(start_cmd.split()[0]).name.lower()
        runtime = _RUNNER_RUNTIME.get(head)
        if runtime:
            needed.add(runtime)
    return needed


def _load_layered_config(repo: Path, kind: str) -> dict[str, Any]:
    """加载分层配置：local/{kind}.local.json（私有）与 config/{kind}.json（公有）**按 id 合并**。

    私有文件里的同 id 条目覆盖公有，其余条目原样保留；私有里新增的条目追加进来。

    这里刻意是合并而不是二选一。文件名叫 local override，用户只写「想改的那几项」
    是完全符合直觉的用法——二选一的话，他没写到的服务会静默消失，
    而报错只会说「某服务不可达」，根本看不出是配置文件被整体替换了。
    """
    qa_root = repo / ".qa-agent"
    local_path = qa_root / "local" / f"{kind}.local.json"
    config_path = qa_root / "config" / f"{kind}.json"

    public = read_json(config_path) if config_path.exists() else {}
    private = read_json(local_path) if local_path.exists() else {}
    public = public if isinstance(public, dict) else {}
    private = private if isinstance(private, dict) else {}

    merged: dict[str, Any] = {**public, **private}
    public_items = public.get(kind)
    private_items = private.get(kind)
    if isinstance(public_items, list) and isinstance(private_items, list):
        merged[kind] = _merge_by_id(public_items, private_items)
    elif isinstance(private_items, list):
        merged[kind] = private_items

    merged.setdefault("version", "1.0")
    merged.setdefault(kind, [])
    return merged


def _merge_by_id(public_items: list[Any], private_items: list[Any]) -> list[Any]:
    """按 id 合并两组条目：同 id 用私有那条，新的追加到末尾，保持公有顺序。"""
    def key_of(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        return str(item.get("id") or item.get("name") or "")

    merged: list[Any] = []
    index: dict[str, int] = {}
    for item in public_items:
        index[key_of(item)] = len(merged)
        merged.append(item)
    for item in private_items:
        key = key_of(item)
        if key and key in index:
            merged[index[key]] = item      # 同 id：私有覆盖公有
        else:
            merged.append(item)
    return merged


def env_example_text(accounts: list[dict[str, Any]], services: list[dict[str, Any]]) -> str:
    """生成 local/.env 模板：密钥、账号凭证和可覆盖的服务地址，按组划分。"""
    sections: list[tuple[str, list[tuple[str, str, str]]]] = [
        ("Agent", [
            ("QA_AGENT", AGENT_CLAUDE, "使用的 agent：claude-code / codex / both"),
        ]),
        ("LLM", [
            ("QA_AGENT_LLM_BASE_URL", "", "LLM 网关地址（覆盖 env.shared 默认值）"),
            ("QA_AGENT_LLM_API_KEY", "", "LLM API Key（可选；不填则跳过多模型交叉审查，其余流程不受影响）"),
        ]),
    ]
    lines = [
        "# QA Agent 本地密钥和覆盖值——不要提交到 git。",
        "# 团队共享的非敏感值在 config/env.shared 中，每次加载时生效。",
        "# 本文件中的同名变量会覆盖 config/env.shared 的值。",
    ]
    seen: set[str] = set()
    for section, kv_pairs in sections:
        lines.append("")
        lines.append(f"# ── {section} ──")
        for key, value, comment in kv_pairs:
            if not key or key in seen:
                continue
            seen.add(key)
            if comment:
                lines.append(f"# {comment}")
            lines.append(f"{key}={value}")
    lines.append("")
    return "\n".join(lines)


def _set_env_key(env_path: Path, key: str, value: str) -> None:
    """在 .env 文件中覆盖或追加 key=value。"""
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def env_shared_text(accounts: list[dict[str, Any]], services: list[dict[str, Any]]) -> str:
    """生成 config/env.shared 内容（团队共享的非敏感环境值），按组划分。"""
    service_keys: list[tuple[str, str, str]] = []
    for service in services:
        sid = str(service.get("id", ""))
        env_key = str(service.get("baseUrlEnv", ""))
        default_url = str(service.get("defaultUrl", ""))
        service_keys.append((env_key, default_url, f"{sid} 服务地址"))
    sections: list[tuple[str, list[tuple[str, str, str]]]] = [
        ("LLM", [
            ("QA_AGENT_LLM_BASE_URL", "", "LLM 网关地址（无内置默认，需自行配置）"),
        ]),
        ("服务地址", service_keys),
        ("账号凭证", [
            ("QA_USER_USERNAME", "", "测试账号用户名"),
            ("QA_USER_PASSWORD", "", "测试账号密码"),
        ]),
        ("MySQL", [
            ("QA_MYSQL_HOST", "", "MySQL 主机"),
            ("QA_MYSQL_PORT", "3306", "MySQL 端口"),
            ("QA_MYSQL_DATABASE", "", "MySQL 数据库名"),
            ("QA_MYSQL_USER", "", "MySQL 用户名"),
            ("QA_MYSQL_PASS", "", "MySQL 密码"),
        ]),
    ]
    lines = [
        "# QA Agent 团队共享环境值——可提交到 git。",
        "# 含测试环境凭证（账号/MySQL），仅 LLM API Key 等真实密钥放在 local/.env。",
    ]
    seen: set[str] = set()
    for section, kv_pairs in sections:
        lines.append("")
        lines.append(f"# ── {section} ──")
        for key, value, comment in kv_pairs:
            if not key or key in seen:
                continue
            seen.add(key)
            if comment:
                lines.append(f"# {comment}")
            lines.append(f"{key}={value}")
    lines.append("")
    return "\n".join(lines)


def init_project(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    if not repo.exists():
        raise QaAgentError(f"Repo not found: {repo}")
    force = bool(getattr(args, "force", False))
    init_layout(
        argparse.Namespace(
            repo=str(repo),
            write_gitignore=True,
            root_gitignore=getattr(args, "root_gitignore", False),
            json=None,
        )
    )
    config_path = repo / ".qa-agent" / "config" / "qa-agent.config.yaml"
    init_config(
        argparse.Namespace(
            output=str(config_path),
            repo=str(repo),
            agent=normalize_agent_name(getattr(args, "agent", AGENT_CLAUDE)),
        )
    )
    profile = detect_project_test_profile(repo)
    profile_path = repo / ".qa-agent" / "profiles" / "project-test-profile.json"
    write_json_if_needed(profile_path, {"version": "1.0", "generatedAt": utc_now(), **profile}, force=force)
    services = infer_service_examples(repo)
    services_data = {
        "version": "1.0",
        "_comment": "服务结构定义：URL 值配置在 env（config/env.shared 公有默认 / local/.env 私有覆盖），通过 baseUrlEnv 环境变量读取；local/services.local.json 用于按 id 私有覆盖服务结构（如 dir/startCmd/readySignal），未列出的服务保持本文件的定义。",
        "services": [{k: v for k, v in s.items() if k != "defaultUrl"} for s in services],
    }
    written = {
        "profile": str(profile_path),
        "envShared": str(repo / ".qa-agent" / "config" / "env.shared"),
        "servicesConfig": str(repo / ".qa-agent" / "config" / "services.json"),
        "localEnv": str(repo / ".qa-agent" / "local" / ".env"),
    }
    write_text_if_needed(Path(written["envShared"]), env_shared_text([], services), force=False)
    write_json_if_needed(Path(written["servicesConfig"]), services_data, force=force)
    write_text_if_needed(Path(written["localEnv"]), env_example_text([], services), force=False)
    agent = normalize_agent_name(getattr(args, "agent", AGENT_CLAUDE))
    _set_env_key(Path(written["localEnv"]), "QA_AGENT", agent)
    seed_playwright_templates(repo, force=force)
    seed_e2e_lib(repo, force=force)
    # 默认装 Playwright 运行时（@playwright/test + config + 浏览器），已就绪则跳过；失败（如无 npm）不阻塞初始化
    if not getattr(args, "skip_playwright_runtime", False):
        try:
            install = install_playwright_runtime_in_repo(
                repo,
                timeout=getattr(args, "timeout", 600),
                skip_if_present=True,
                dry_run=getattr(args, "dry_run", False),
                install_browsers=True,
            )
            if install["status"] == "skipped":
                print(f"Playwright 运行时已就绪：{summarize_playwright(install['before'])}")
            elif getattr(args, "dry_run", False):
                print(f"[dry-run] 将安装 Playwright 运行时")
            else:
                print(f"Playwright 运行时安装完成：{summarize_playwright(install['after'])}")
        except QaAgentError as exc:
            print(f"跳过 Playwright 运行时安装：{exc}")
    verify_mysql = bool(getattr(args, "verify_mysql_mcp", False))
    mysql = detect_mysql_mcp(repo, verify=verify_mysql, timeout=getattr(args, "mysql_mcp_timeout", 12))
    summary = {
        "version": "1.0",
        "generatedAt": utc_now(),
        "repo": str(repo),
        "generated": written,
        "testSuites": [suite.get("name") for suite in profile.get("suites", [])],
        "mysqlMcp": {
            "projectConfigured": bool(mysql.get("projectConfigured")),
            "codexConfigured": bool(mysql.get("codexConfigured")),
            "serverName": mysql.get("serverName"),
            **({"verify": mysql["verify"]} if mysql.get("verify") else {}),
        },
        "nextActions": [
            # 受众是「正在执行这条命令的人」——通常是 agent。不要去写「回到 agent 说句话」，
            # 那对 agent 自身是循环的，对人也没必要。
            "下一步：运行 doctor --strict --check-services 体检环境。",
            "缺什么按提示引导用户补齐：必填通常是测试账号（config/env.shared 的 "
            "QA_USER_USERNAME/QA_USER_PASSWORD）与目标服务地址；LLM API Key 在 local/.env，"
            "是可选的交叉审查增强，不填则该阶段跳过。",
        ],
    }
    if getattr(args, "json", None):
        write_json(Path(args.json).resolve(), summary)
    print("\nQA Agent 初始化完成")
    print("\n已生成/确认可提交文件：")
    for key in ["profile", "envShared", "servicesConfig"]:
        print(f"- {written[key]}")
    print("\n已生成/确认本地文件（不要提交）：")
    for key in ["localEnv"]:
        print(f"- {written[key]}")
    if verify_mysql:
        verify_result = mysql.get("verify") or {}
        if verify_result.get("ok"):
            print("\nMySQL MCP 连接验证通过")
        elif "verify" in mysql:
            stderr_tail = (verify_result.get("stderr") or "").strip().splitlines()
            hint = stderr_tail[-1] if stderr_tail else f"exitCode={verify_result.get('exitCode')}"
            print(f"\nMySQL MCP 连接验证失败：{hint}")
        else:
            print(f"\nMySQL MCP 连接验证未执行：{mysql.get('reason', 'unknown')}")
    # 打印推导出的服务启动配置
    if services:
        print("\n推导出的服务启动配置：")
        for svc in services:
            sid = svc.get("id", "?")
            d = svc.get("dir", "")
            cmd = svc.get("startCmd", "")
            sig = svc.get("readySignal", "")
            status = "已配置" if (cmd and d) else "缺少 startCmd/dir，需手动补全"
            print(f"  {sid}: cd {d} && {cmd}  (ready: {sig})  [{status}]")
            if not cmd or not d:
                print(
                    "    → 补全启动方式：把这些字段写进 "
                    ".qa-agent/local/services.local.json（按 id 覆盖，未列出的服务保持原样）。"
                    "该文件私有、不进 git；想与团队共享就改 config/services.json"
                )
    print("\n下一步：")
    for action in summary["nextActions"]:
        print(f"- {action}")


def init_config(args: argparse.Namespace) -> None:
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    repo = Path(getattr(args, "repo", output.parent.parent)).resolve()
    if getattr(args, "skip_detect_commands", False) or not repo.exists():
        shutil.copyfile(ASSETS / "ming-qa.config.example.yaml", output)
    else:
        profile = detect_project_test_profile(repo)
        output.write_text(render_config_yaml(detect_base_branch(repo), profile["commands"]), encoding="utf-8")
    print(f"已写入配置：{output}")
    if not repo.exists():
        return

    agent = normalize_agent_name(getattr(args, "agent", AGENT_CLAUDE))
    loops = [AGENT_CLAUDE, AGENT_CODEX] if agent == AGENT_BOTH else [agent]
    # 从 .env 自动生成 MySQL 凭证（共用，只跑一次）
    cred = upsert_mysql_mcp_entry(repo)
    if cred["generated"]:
        print(f"已生成 MySQL MCP 配置：{cred['mcpPath']}")
    # Codex：生成项目级 .codex/config.toml（agent=claude 时跳过）
    if agent != AGENT_CLAUDE:
        codex = generate_codex_mcp_from_project(repo)
        if codex["generated"]:
            print(f"已生成 Codex MCP 配置：{codex['codexConfigPath']}")

    # Playwright Test Agents（每个 loop 独立安装，已存在则跳过）
    for loop in loops:
        playwright = detect_playwright_assets(repo)
        if has_playwright_test_agents(playwright, loop):
            print(f"已检测到 Playwright Test Agents ({loop})：{summarize_playwright(playwright)}")
            continue
        install = install_playwright_agents_in_repo(
            repo,
            loop=loop,
            timeout=300,
            skip_if_present=True,
            dry_run=False,
            no_yes=False,
        )
        if install["status"] == "failed":
            result = install["result"]
            if result.get("stdout"):
                print(result["stdout"])
            if result.get("stderr"):
                print(result["stderr"], file=sys.stderr)
            raise SystemExit(result["exitCode"])
        if install["status"] == "dry-run":
            print(f"将安装 Playwright Test Agents ({loop})：{install['command']}")
        else:
            print(f"Playwright Test Agents ({loop}) 安装完成：{summarize_playwright(install['after'])}")


def run_commands(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    config = load_config(Path(args.config).resolve())
    commands = config.get("commands", {}).get(args.gate, [])
    if not commands:
        raise QaAgentError(f"No commands configured for gate: {args.gate}")
    run = {
        "version": "1.0",
        "startedAt": utc_now(),
        "repo": str(repo),
        "gate": args.gate,
        "commands": [],
    }
    env = gate_env(repo)
    for command in commands:
        result = run_cmd(command, repo, timeout=args.timeout, shell=True, env=env)
        run["commands"].append(result)
        if result["exitCode"] != 0 and not args.continue_on_failure:
            break
    run["finishedAt"] = utc_now()
    run["status"] = "passed" if all(cmd["exitCode"] == 0 for cmd in run["commands"]) else "failed"
    write_json(Path(args.output).resolve(), run)
    print(f"已写入运行结果：{Path(args.output).resolve()}（{run['status']}）")
    if run["status"] != "passed":
        raise SystemExit(1)


def configured_commands(config: dict[str, Any], gate: str) -> list[str]:
    commands = config.get("commands", {}).get(gate, [])
    return commands if isinstance(commands, list) else []


def run_gate(repo: Path, gate: str, commands: list[str], timeout: int, continue_on_failure: bool) -> dict[str, Any]:
    gate_run = {"gate": gate, "status": "skipped", "commands": []}
    if not commands:
        gate_run["reason"] = "no commands configured"
        return gate_run
    gate_run["status"] = "passed"
    env = gate_env(repo)
    for command in commands:
        result = run_cmd(command, repo, timeout=timeout, shell=True, env=env)
        gate_run["commands"].append(result)
        if result["exitCode"] != 0:
            gate_run["status"] = "failed"
            if not continue_on_failure:
                break
    return gate_run


def run_loop(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    config = load_config(Path(args.config).resolve())
    max_loops = int(config.get("qualityGates", {}).get("maxRepairLoops", 5))
    gates = args.gates.split(",") if args.gates else ["unit", "api", "integration", "e2e", "review"]
    harness = {
        "version": "1.0",
        "startedAt": utc_now(),
        "repo": str(repo),
        "maxRepairLoops": max_loops,
        "iterations": [],
    }
    final_status = "passed"
    for index in range(1, max_loops + 1):
        iteration = {"index": index, "startedAt": utc_now(), "gates": []}
        failed_gate = None
        for gate in gates:
            gate = gate.strip()
            if not gate:
                continue
            gate_result = run_gate(
                repo,
                gate,
                configured_commands(config, gate),
                timeout=args.timeout,
                continue_on_failure=args.continue_on_failure,
            )
            iteration["gates"].append(gate_result)
            if gate_result["status"] == "failed":
                failed_gate = gate
                break
        iteration["finishedAt"] = utc_now()
        iteration["status"] = "failed" if failed_gate else "passed"
        if failed_gate:
            iteration["failedGate"] = failed_gate
            iteration["repairRequired"] = True
            final_status = "failed"
        else:
            final_status = "passed"
        harness["iterations"].append(iteration)
        # The Codex agent performs code repair between harness invocations; the
        # deterministic harness stops at the first failed iteration with evidence.
        if failed_gate or final_status == "passed":
            break
    harness["finishedAt"] = utc_now()
    harness["status"] = final_status
    if final_status == "failed" and len(harness["iterations"]) >= max_loops:
        harness["status"] = "max_loops_exhausted"
    output_path = Path(args.output).resolve()
    write_json(output_path, harness)
    print(f"已写入循环执行结果：{output_path}（{harness['status']}）")
    update_manifest(
        repo,
        stage="test-runner",
        artifact={"run-loop": _repo_rel(repo, output_path)},
        status={
            "loopIteration": len(harness["iterations"]),
            "loopStatus": harness["status"],
            "maxRepairLoops": max_loops,
        },
    )
    if harness["status"] != "passed":
        raise SystemExit(1)


def update_results(args: argparse.Namespace) -> None:
    cases_path = Path(args.cases).resolve()
    cases_data = read_json(cases_path)
    errors, cases_data = validate_cases_data(cases_data, mutate=True)
    if errors:
        raise QaAgentError("Invalid cases: " + "; ".join(errors))
    run_data = read_json(Path(args.run).resolve())
    EVIDENCE_MISSING = "__evidence_missing__"
    explicit_case_status: dict[str, dict[str, Any]] = {}
    for result in run_data.get("caseResults", []):
        if isinstance(result, dict) and result.get("caseId"):
            explicit_case_status[str(result["caseId"])] = result
    # aggregate-runs 的产物键是 cases[]（每条带 finalOutcome），不是 caseResults[]——
    # 而 caseResults[] 全仓库没有任何产出方。只认 caseResults 的后果是：每条用例的
    # 真实执行证据永远落不到用例状态上，状态只能退化成「该层有没有配置门禁命令」，
    # 于是 run-e2e 真跑过并通过的用例，会因为 e2e 层未配置命令而被判成 skipped。
    # 显式 caseResults 优先，这里只做补充。
    for result in run_data.get("cases", []):
        if not isinstance(result, dict):
            continue
        case_id = result.get("caseId")
        outcome = str(result.get("finalOutcome") or "").upper()
        mapped = {"PASS": "passed", "FAIL": "failed"}.get(outcome)
        if case_id and mapped and str(case_id) not in explicit_case_status:
            explicit_case_status[str(case_id)] = {
                "caseId": case_id,
                "status": mapped,
                "outcome": outcome,
                "summary": f"来自 aggregate-runs 的逐用例执行证据（finalOutcome={outcome}）",
            }
    gate_status: dict[str, str] = {}
    if "gate" in run_data:
        gate_status[run_data["gate"]] = run_data.get("status", EVIDENCE_MISSING)
    for iteration in run_data.get("iterations", []):
        for gate in iteration.get("gates", []):
            gate_status[gate.get("gate", "")] = gate.get("status", EVIDENCE_MISSING)
    layer_to_gate = {
        "frontend-unit": "unit",
        "backend-unit": "unit",
        "api": "api",
        "integration": "integration",
        "e2e": "e2e",
    }
    for case in cases_data.get("cases", []):
        case_id = str(case.get("id", ""))
        if case_id in explicit_case_status:
            explicit = explicit_case_status[case_id]
            explicit_status = explicit.get("status")
            if explicit_status:
                case["status"] = explicit_status
                case["result"] = {
                    "updatedAt": utc_now(),
                    "runRef": str(Path(args.run).resolve()),
                    **{k: v for k, v in explicit.items() if k != "caseId"},
                }
            else:
                # 存在显式结果记录但没有有效状态：不猜测状态，保留原值并留痕待核实。
                case["result"] = {
                    "updatedAt": utc_now(),
                    "runRef": str(Path(args.run).resolve()),
                    "classification": "execution-evidence-missing",
                    "summary": "存在显式结果记录，但未提供有效状态，用例状态保持不变，需人工核实证据链。",
                    **{k: v for k, v in explicit.items() if k not in {"caseId", "status"}},
                }
            continue
        gate = layer_to_gate.get(case.get("layer"))
        if not gate or gate not in gate_status:
            continue
        status = gate_status[gate]
        if status == "passed" and not getattr(args, "legacy_gate_mapping", False):
            case["result"] = {
                "updatedAt": utc_now(),
                "gate": gate,
                "gateStatus": status,
                "runRef": str(Path(args.run).resolve()),
                "classification": "quality-gate-evidence-only",
                "summary": "Gate passed, but business case status was not changed because no explicit caseResult/spec-task evidence mapped this run to the case.",
            }
            continue
        if status == EVIDENCE_MISSING:
            # 质量门禁产物中没有该用例对应的有效状态：不默认判定为 blocked，只留痕待核实。
            case["result"] = {
                "updatedAt": utc_now(),
                "gate": gate,
                "runRef": str(Path(args.run).resolve()),
                "classification": "execution-evidence-missing",
                "summary": "质量门禁产物中未找到该用例对应的有效执行结果状态，用例状态保持不变，需人工核实证据链。",
            }
            continue
        if status == "passed":
            case["status"] = "passed"
        elif status == "failed":
            case["status"] = "failed"
        elif status == "skipped":
            case["status"] = "skipped"
        else:
            case["status"] = status
        case["result"] = {
            "updatedAt": utc_now(),
            "gate": gate,
            "gateStatus": status,
            "runRef": str(Path(args.run).resolve()),
        }
    output = Path(args.output).resolve() if args.output else cases_path
    write_json(output, cases_data)
    print(f"已写入更新后的测试用例：{output}")
    repo = Path(args.repo).resolve()
    update_manifest(
        repo,
        artifact={"cases": _repo_rel(repo, output)},
        status={"resultsUpdated": "done"},
    )


def default_codex_skills_dir() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "skills"
    return resolve_home_dir() / ".codex" / "skills"


def cmd_manifest(args: argparse.Namespace) -> None:
    """查看当前 QA 流程 manifest。

    `--brief` 给人类读的汇总：当前阶段、每个产物**是否真的落盘**、各状态键。
    原始 JSON 只列路径，要判断「这个阶段到底完没完成」得自己逐个去 stat——
    而 .qa-agent/current/ 下有二十来个 json，缺的正是这个总览。
    """
    repo = Path(args.repo).resolve()
    manifest_file = repo / MANIFEST_PATH
    if not manifest_file.exists():
        print(f"manifest 不存在（{manifest_file}），先执行 QA 阶段命令生成")
        return
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not getattr(args, "brief", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"当前阶段：{data.get('currentStage', '-')}")
    artifacts = data.get("artifacts", {}) if isinstance(data.get("artifacts"), dict) else {}
    if artifacts:
        print("\n产物：")
        for name, path in sorted(artifacts.items()):
            mark = "✓" if (repo / str(path)).exists() else "✗"
            print(f"  {mark} {name} — {path}")
    status = data.get("status", {}) if isinstance(data.get("status"), dict) else {}
    if status:
        print("\n状态：")
        for key, value in sorted(status.items()):
            print(f"  {key}: {value}")


# ---------------------------------------------------------------------------
# run-with-env — 统一测试脚本执行环境入口
# ---------------------------------------------------------------------------

_ENV_REQUIRED_PREFIX = "QA_USER_PASSWORD"
_ENV_OPTIONAL_PREFIXES = ("QA_USER_USERNAME", "QA_API_BASE_URL", "QA_WEB_BASE_URL",
                           "QA_MYSQL_HOST", "QA_MYSQL_PORT", "QA_MYSQL_DATABASE", "QA_MYSQL_USER")


def _load_env(repo: Path) -> dict[str, str]:
    """分层加载 config/env.shared → local/.env，返回环境变量字典。"""
    env_vars: dict[str, str] = {}
    for env_path in [repo / ".qa-agent" / "config" / "env.shared",
                     repo / ".qa-agent" / "local" / ".env"]:
        if not env_path.exists():
            continue
        raw = env_path.read_text(encoding="utf-8")
        for line in raw.splitlines():
            line = line.strip().replace("\r", "")
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\"'")
            env_vars[key] = val
    return env_vars


def _derive_case_id(stem: str) -> str:
    """从脚本/spec 文件名 stem 推导 case_id（tc-pX-NNN 或 task-N-tc-pX-NNN）。"""
    parts = stem.split("-")
    if parts and parts[0] == "task" and len(parts) >= 5:
        return f"{parts[2]}-{parts[3]}-{parts[4]}"
    if len(parts) >= 3 and parts[0].lower() == "tc":
        return f"{parts[0]}-{parts[1]}-{parts[2]}"
    return stem


def _record_run_sidecar(repo: Path, case_id: str, task_id: str, script_name: str,
                        return_code: int, stdout: str, stderr: str,
                        case_ids: list[str] | None = None) -> None:
    """写 runs/run-*.log + run-*.meta.json（聚合唯一事实源）。"""
    runs_dir = repo / ".qa-agent" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    slug = spec_slug(case_id) or "default"
    epoch = int(time.time())
    log_path = runs_dir / f"run-{case_id}-{slug}-{epoch}.log"
    log_path.write_text(
        f"# run {script_name}\n"
        f"# executed: {dt.datetime.now().isoformat()}\n"
        f"# exit_code: {return_code}\n\n"
        f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n",
        encoding="utf-8"
    )
    covered_ids = [case_id.upper()] if case_id else []
    for extra in case_ids or []:
        value = str(extra or "").strip().upper()
        if value and value not in covered_ids:
            covered_ids.append(value)
    sidecar = {
        "version": "1.0",
        "runId": str(epoch),
        "caseId": case_id.upper(),
        # 一次执行可以覆盖多条用例（一个套件跑完十几条）。只写单值的话，其余用例会被
        # evidence-integrity 门禁判成「缺少执行记录」，使用者只能把套件拆开逐条跑——
        # 为了迎合门禁改变测试组织方式，方向是反的。
        "caseIds": covered_ids,
        "taskId": task_id,
        "script": script_name,
        "logFile": log_path.name,
        "exitCode": return_code,
        "executedAt": dt.datetime.now(_CN_TZ).isoformat(timespec="seconds"),
        "outcome": "PASS" if return_code == 0 else "FAIL",
    }
    sidecar_path = runs_dir / f"run-{case_id}-{slug}-{epoch}.meta.json"
    write_json(sidecar_path, sidecar)
    print(f"[ming-qa] 日志: {log_path}")


def interpreter_argv_for_script(script_path: Path) -> list[str]:
    """按扩展名选解释器。

    历史缺陷：这里一律用 bash 执行，不看扩展名。`.py` 脚本因此被 bash 当 shell 解析，
    报「import: command not found」「syntax error near unexpected token '('」——使用者
    只能自己再写一层 `.sh` 包装来 exec python。

    `.py` 用跑本 CLI 的那个解释器（`sys.executable`），而不是 PATH 里的 `python`：
    后者在 Windows 上可能是 Microsoft Store 的占位程序（静默无输出）。
    未知扩展名仍按 shell 脚本处理，保持既有行为。
    """
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        return [sys.executable or "python", str(script_path)]
    if suffix in {".js", ".mjs", ".cjs"}:
        node = shutil.which("node")
        if not node:
            raise QaAgentError(f"{suffix} 脚本需要 Node.js，但 PATH 里找不到 node")
        return [node, str(script_path)]
    return [shutil.which("bash") or "bash", str(script_path)]


def run_with_env(args: argparse.Namespace) -> None:
    """分层加载 config/env.shared → local/.env → CRLF→LF → 执行脚本 → 记录日志。

    按扩展名选解释器，并以 argv 直接执行（不经 shell）。
    """
    repo = Path(args.repo).resolve()

    env_vars = _load_env(repo)

    # --extra 传入的额外变量（同名覆盖 .env）
    extra_vars: dict[str, str] = {}
    for item in getattr(args, "extra", None) or []:
        key, sep, val = str(item).partition("=")
        if sep:
            extra_vars[key] = val

    script_path = Path(args.script)
    if not script_path.is_absolute():
        script_path = (repo / script_path).resolve()
    if not script_path.exists():
        raise QaAgentError(f"脚本不存在: {script_path}")

    argv = interpreter_argv_for_script(script_path)
    total_vars = len(env_vars) + len(extra_vars)
    print(f"[run-with-env] {script_path.name}")
    print(f"[run-with-env] 命令: {Path(argv[0]).name} {script_path.name}（{total_vars} vars）")
    if getattr(args, "dry_run", False):
        print("[run-with-env] DRY-RUN, 不执行")
        return

    # 执行并捕获输出（显式指定 UTF-8 避免 Windows GBK 默认编码导致的解码错误）。
    # 变量放进 env 而不是拼成 "env K=V …" 前缀再 shell=True：值里含空格或引号时
    # 拼字符串会被 shell 拆错，配置派生的内容也会被 shell 解释。
    import subprocess as sp
    env_os = os.environ.copy()
    env_os.update(env_vars)
    env_os.update(extra_vars)
    env_os["PYTHONIOENCODING"] = "utf-8"
    result = sp.run(argv, cwd=str(repo), env=env_os,
                    capture_output=True, encoding="utf-8", errors="replace",
                    timeout=getattr(args, "timeout", 120))
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    # case_id：显式 --case-id 优先，其次 --case-ids 的第一项，最后从脚本名推断
    explicit_case_id = (getattr(args, "case_id", None) or "").strip()
    declared_ids = [
        part.strip().upper()
        for part in str(getattr(args, "case_ids", "") or "").split(",")
        if part.strip()
    ]
    case_id = explicit_case_id or (declared_ids[0] if declared_ids else "") or _derive_case_id(script_path.stem)
    _record_run_sidecar(
        repo, case_id, getattr(args, "task_id", None) or "",
        script_path.name, result.returncode, result.stdout, result.stderr,
        case_ids=declared_ids,
    )

    if result.returncode != 0:
        raise SystemExit(result.returncode)


def run_e2e(args: argparse.Namespace) -> None:
    """在 playwrightDir 执行 npx playwright test <spec>，并写 run 证据（log + sidecar）。

    与 run-with-env 对称：run-with-env 执行 bash 脚本，run-e2e 执行 Playwright spec。
    两者都写 runs/run-*.log + .meta.json，供 aggregate-runs 统一聚合。
    """
    repo = Path(args.repo).resolve()
    spec = Path(args.spec)
    if not spec.is_absolute():
        spec = (repo / spec).resolve()

    env_vars = _load_env(repo)
    env_os = os.environ.copy()
    env_os["PYTHONIOENCODING"] = "utf-8"
    for key, val in env_vars.items():
        env_os[key] = val
    for item in getattr(args, "extra", None) or []:
        if "=" in item:
            key, _, val = item.partition("=")
            env_os[key] = val

    pw_dir = get_playwright_dir(repo)
    try:
        rel_spec = str(spec.relative_to(pw_dir)).replace("\\", "/")
    except ValueError:
        rel_spec = str(spec).replace("\\", "/")

    npx = find_npx()
    if not npx:
        raise QaAgentError("npx was not found in PATH; install Node.js/npm first")

    cmd = [npx, "playwright", "test", str(rel_spec)]
    if getattr(args, "project", None):
        cmd += ["--project", args.project]

    import subprocess as sp
    print(f"[run-e2e] {' '.join(cmd)} (cwd={pw_dir})")
    result = sp.run(cmd, cwd=str(pw_dir), env=env_os,
                    capture_output=True, encoding="utf-8", errors="replace",
                    timeout=getattr(args, "timeout", 180))
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    explicit_case_id = getattr(args, "case_id", None)
    stem = spec.name
    for suffix in (".spec.ts", ".spec.js", ".spec.tsx", ".spec.mjs"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    case_id = explicit_case_id.strip() if explicit_case_id else _derive_case_id(stem)
    _record_run_sidecar(
        repo, case_id, getattr(args, "task_id", None) or "",
        spec.name, result.returncode, result.stdout, result.stderr,
    )

    if result.returncode != 0:
        raise SystemExit(result.returncode)


def default_skills_dir(target: str) -> Path:
    """按 target 返回用户级 skills 目录。"""
    if normalize_agent_name(target) == AGENT_CLAUDE:
        return resolve_home_dir() / ".claude" / "skills"
    return default_codex_skills_dir()


def _remove_install_target(path: Path) -> None:
    """删除安装目标，兼容三种形态：普通目录、符号链接、Windows junction。

    Windows 上 npx skills 建的是 **junction**（reparse tag MOUNT_POINT）。
    Python 的 Path.is_symlink() 与 os.path.islink() 都不认它（lstat 看到的是目录），
    但 shutil.rmtree() 会直接抛 "Cannot call rmtree on a symbolic link"——
    于是「先判断是不是链接」这种分支写法必然漏掉 junction。

    所以这里对 rmtree 的失败兜底：junction 用 os.rmdir 删除**链接本身**，绝不动真身
    （真身在 .agents/skills 下，是另一份真实安装）。
    """
    if path.is_symlink() or os.path.islink(path):
        path.unlink()
        return
    try:
        shutil.rmtree(path)
    except OSError:
        # junction 走这里：rmdir 只摘掉链接，不会递归删除目标目录
        os.rmdir(path)


def _install_skills_from(source_root: Path, destination_root: Path, *, force: bool) -> list[str]:
    """把 source_root 下的 8 个 skill 目录覆盖安装到 destination_root，返回已安装目录名。"""
    destination_root.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".qa-agent", ".git")
    installed: list[str] = []
    for skill_dir in SKILL_DIRS:
        src = source_root / skill_dir
        dst = destination_root / skill_dir
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            if not force:
                raise QaAgentError(f"Destination exists, rerun with --force: {dst}")
            _remove_install_target(dst)
        shutil.copytree(src, dst, ignore=ignore)
        installed.append(skill_dir)
    return installed


def _validate_skill(destination_root: Path) -> None:
    """若目标环境存在 skill-creator 校验器，则校验主 skill。"""
    validator = destination_root / ".system" / "skill-creator" / "scripts" / "quick_validate.py"
    skill_dir = destination_root / "quality-assurance-agent"
    if validator.exists():
        result = run_cmd([sys.executable, str(validator), str(skill_dir)], destination_root, timeout=60)
        print(result["stdout"] or result["stderr"])
        if result["exitCode"] != 0:
            raise SystemExit(result["exitCode"])
    else:
        print("未找到校验器；已跳过 quick_validate.py")


def install_skill(args: argparse.Namespace) -> None:
    if normalize_agent_name(args.target) not in (AGENT_CLAUDE, AGENT_CODEX):
        raise QaAgentError(f"Unsupported target: {args.target}")
    destination_root = Path(args.path).resolve() if args.path else default_skills_dir(args.target)
    source_root = ROOT.parent  # 开发仓库 .claude/skills/，含 8 个 skill 目录
    installed = _install_skills_from(source_root, destination_root, force=args.force)
    print(f"已安装 {len(installed)} 个 skill 到：{destination_root}")
    _validate_skill(destination_root)


def cmd_version(args: argparse.Namespace) -> None:
    print(SKILL_VERSION)


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def command_version(command: str, args: list[str] | None = None) -> str:
    executable = shutil.which(command)
    if not executable:
        return "not found"
    result = run_cmd([executable, *(args or ["--version"])], Path.cwd(), timeout=20)
    text = (result.get("stdout") or result.get("stderr") or "").strip().splitlines()
    return text[0][:200] if text else executable


def encoding_health() -> dict[str, Any]:
    preferred = (sys.getfilesystemencoding() or "").lower()
    stdout_encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    python_io = os.environ.get("PYTHONIOENCODING", "")
    ok = "utf" in preferred and ("utf" in stdout_encoding or os.name != "nt")
    detail = f"fs={preferred or 'unknown'}, stdout={stdout_encoding or 'unknown'}, PYTHONIOENCODING={python_io or 'unset'}"
    if os.name == "nt" and "utf" not in stdout_encoding:
        detail += "; set $OutputEncoding, [Console]::OutputEncoding, and PYTHONIOENCODING=utf-8 before rendering Chinese artifacts"
    return {"ok": ok, "detail": detail}


def parse_surefire_reports(reports_dir: Path) -> dict[str, Any]:
    summary = {
        "reportsDir": str(reports_dir),
        "files": 0,
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "failedSuites": [],
    }
    if not reports_dir.exists():
        summary["status"] = "missing"
        return summary
    for xml_path in sorted(reports_dir.glob("TEST-*.xml")):
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError as exc:
            summary["failedSuites"].append({"file": str(xml_path), "error": f"parse error: {exc}"})
            continue
        tests = int(float(root.attrib.get("tests", 0)))
        failures = int(float(root.attrib.get("failures", 0)))
        errors = int(float(root.attrib.get("errors", 0)))
        skipped = int(float(root.attrib.get("skipped", 0)))
        summary["files"] += 1
        summary["tests"] += tests
        summary["failures"] += failures
        summary["errors"] += errors
        summary["skipped"] += skipped
        if failures or errors:
            summary["failedSuites"].append(
                {
                    "file": str(xml_path),
                    "name": root.attrib.get("name", xml_path.stem),
                    "tests": tests,
                    "failures": failures,
                    "errors": errors,
                    "skipped": skipped,
                }
            )
    if not summary["files"]:
        summary["status"] = "no-reports"
    elif summary["tests"] == 0:
        # skipTests/maven.test.skip 导致 0 测试执行，属于假通过，不得判 passed
        summary["status"] = "no-tests"
    else:
        summary["status"] = "passed" if not summary["failures"] and not summary["errors"] else "failed"
    return summary


def surefire_to_quality_gate(summary: dict[str, Any], command: str | None = None) -> dict[str, Any]:
    return {
        "id": "QG-BACKEND-SUREFIRE",
        "name": "Backend Maven Surefire reports",
        "category": "integration",
        "command": command or "mvn -q test -DskipITs",
        "status": summary.get("status", "blocked"),
        "summary": "{tests} tests, {failures} failures, {errors} errors, {skipped} skipped, {files} files".format(
            **summary
        ),
        "artifacts": [summary.get("reportsDir", "")],
        "details": {
            "tests": summary.get("tests", 0),
            "failures": summary.get("failures", 0),
            "errors": summary.get("errors", 0),
            "skipped": summary.get("skipped", 0),
            "files": summary.get("files", 0),
            "failedSuites": summary.get("failedSuites", []),
        },
    }


def summarize_surefire(args: argparse.Namespace) -> None:
    summary = parse_surefire_reports(Path(args.reports).resolve())
    if args.output:
        write_json(Path(args.output).resolve(), summary)
    if getattr(args, "quality_gate_output", None):
        write_json(
            Path(args.quality_gate_output).resolve(),
            surefire_to_quality_gate(summary, getattr(args, "command", None)),
        )
    print(
        "Surefire: {status}; {tests} tests, {failures} failures, {errors} errors, {skipped} skipped, {files} files".format(
            **summary
        )
    )
    if summary.get("failedSuites"):
        print(json.dumps(summary["failedSuites"][:20], ensure_ascii=False, indent=2))
    if summary["status"] == "failed":
        raise SystemExit(1)


def checks_to_environment_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": "ENV-" + re.sub(r"[^A-Za-z0-9]+", "-", str(check.get("name", "check")).upper()).strip("-"),
            "name": check.get("name", ""),
            "status": "passed" if check.get("ok") else "blocked",
            "detail": check.get("detail", ""),
            "requiredFor": ["quality-assurance-agent"],
        }
        for check in checks
    ]


def doctor(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    checks = []

    def add(name: str, ok: bool, detail: str, *, category: str = "environment", next_action: str = "", required: bool = True) -> None:
        item = {"name": name, "ok": ok, "detail": detail, "category": category, "required": required}
        if next_action:
            item["nextAction"] = next_action
        checks.append(item)

    add("python", True, sys.version.split()[0])
    enc = encoding_health()
    add("utf8_output", bool(enc["ok"]), enc["detail"], required=False)
    add("git", command_exists("git"), shutil.which("git") or "not found")
    # ripgrep 是 collect-context 的硬依赖，缺了会直接失败——之前 doctor 根本不检查它
    add(
        "ripgrep",
        bool(shutil.which("rg")),
        shutil.which("rg") or (
            "未找到；collect-context --scope module 需要它。安装："
            "winget install BurntSushi.ripgrep.MSVC（Windows）/ brew install ripgrep（macOS）"
            "/ apt install ripgrep（Debian/Ubuntu）"
        ),
    )
    # 服务启动命令真正用到哪个运行时，它就应当是必须项而不是可选项
    runtime_needs = service_runtime_requirements(repo) if repo.exists() else set()
    add("java", command_exists("java"), command_version("java", ["-version"]), required=False)
    add(
        "maven",
        command_exists("mvn"),
        command_version("mvn", ["-version"]) if command_exists("mvn")
        else ("未找到；config/services.json 的启动命令依赖它" if "maven" in runtime_needs else "not found"),
        required="maven" in runtime_needs,
        next_action=(
            "安装 Maven 或改掉 config/services.json 里的启动命令" if "maven" in runtime_needs else ""
        ),
    )
    add(
        "node",
        command_exists("node"),
        command_version("node", ["--version"]) if command_exists("node")
        else ("未找到；config/services.json 的启动命令依赖它" if "node" in runtime_needs else "not found"),
        required="node" in runtime_needs,
    )
    add("npm", command_exists("npm"), command_version("npm", ["--version"]), required=False)
    add("npx", bool(find_npx()), find_npx() or "未找到；自动安装 Playwright Test Agents 需要 Node.js/npm", required=False)
    add("skill_dir", (ROOT / "SKILL.md").exists(), str(ROOT))
    skill_text_health = scan_mojibake_paths([ROOT / "SKILL.md", ROOT / "README.md", ASSETS / "report-template.html"])
    add(
        "skill_text_mojibake",
        skill_text_health["status"] == "passed",
        "{issueFileCount}/{scannedFileCount} files with mojibake".format(**skill_text_health),
    )
    if repo.exists():
        load_local_env(repo)
        add("repo_exists", True, str(repo))
        add("git_repo", (repo / ".git").exists(), ".git found" if (repo / ".git").exists() else ".git missing", required=False)
        if (repo / ".git").exists():
            add("current_branch", True, git_output(repo, ["branch", "--show-current"]).strip() or "detached/unknown")
        config_path = repo / ".qa-agent" / "config" / "qa-agent.config.yaml"
        legacy_config_path = repo / ".qa-agent" / "qa-agent.config.yaml"
        effective_config_path = config_path if config_path.exists() else legacy_config_path
        qa_root = repo / ".qa-agent"
        missing_layout = [name for name in [*QA_LAYOUT_TRACKED_DIRS, *QA_LAYOUT_RUNTIME_DIRS] if not (qa_root / name).exists()]
        add(
            "qa_layout",
            not missing_layout,
            "ok" if not missing_layout else "missing: " + ", ".join(missing_layout),
            category="setup",
            next_action=f"{self_invocation()} init-project --repo ." if missing_layout else "",
        )
        add(
            "qa_config",
            effective_config_path.exists(),
            str(effective_config_path) if effective_config_path.exists() else "missing",
            category="setup",
            next_action=f"{self_invocation()} init-project --repo ." if not effective_config_path.exists() else "",
        )
        local_env_path = repo / ".qa-agent" / "local" / ".env"
        add(
            "local_env_file",
            local_env_path.exists(),
            str(local_env_path) if local_env_path.exists() else "missing",
            category="secrets",
            next_action="填写 .qa-agent/local/.env" if not local_env_path.exists() else "",
        )
        stack = detect_stack(repo)
        add("stack_detected", bool(stack.get("stacks")), ", ".join(stack.get("stacks", [])) or "none", required=False)
        test_profile = detect_project_test_profile(repo)
        suite_names = [suite.get("name", "") for suite in test_profile.get("suites", [])]
        add("test_profile", bool(suite_names), ", ".join(suite_names) or "no test suites detected", required=False)
        require_existing_tests = bool(getattr(args, "require_existing_tests", False))
        for suite in test_profile.get("suites", []):
            files = suite.get("testFiles")
            if isinstance(files, list):
                missing_tests_action = (
                    "创建或恢复该套件的测试文件，或关闭 --require-existing-tests"
                    if require_existing_tests
                    else "无需阻断初始化；在测试脚本生成阶段补齐或映射到 spec tasks"
                )
                add(
                    f"test_files:{suite.get('name')}",
                    bool(files),
                    f"{len(files)} 个测试文件" if files else "未发现测试文件；后续应生成或映射测试脚本",
                    next_action=missing_tests_action if not files else "",
                    required=require_existing_tests,
                )
            if suite.get("testDir"):
                add(
                    f"test_dir:{suite.get('name')}",
                    (repo / suite["root"] / suite["testDir"]).exists(),
                    f"{suite['root']}/{suite['testDir']}",
                    next_action="确认测试目录配置，或在测试脚本生成阶段创建目录" if not (repo / suite["root"] / suite["testDir"]).exists() else "",
                    required=require_existing_tests,
                )
        from qa_core.project_manifest import discover_projects

        for project in discover_projects(repo):
            if project.kind != "maven":
                continue
            surefire_path = project.root / "target" / "surefire-reports"
            if not surefire_path.exists():
                continue
            surefire = parse_surefire_reports(surefire_path)
            add(
                f"surefire_reports:{project.name}",
                surefire.get("status") == "passed",
                "{tests} tests, {failures} failures, {errors} errors, {skipped} skipped, {files} files".format(**surefire),
            )
        if os.name == "nt":
            add(
                "powershell_maven_selector",
                True,
                'quote multi-class selectors, for example: mvn -q "-Dtest=A,B" test',
            )
        agent = normalize_agent_name(getattr(args, "agent", AGENT_CLAUDE))
        loops = [AGENT_CLAUDE, AGENT_CODEX] if agent == AGENT_BOTH else [agent]

        # Playwright Test Agents（每个 loop 独立安装）
        for loop in loops:
            playwright = detect_playwright_assets(repo)
            if not has_playwright_test_agents(playwright, loop):
                install = install_playwright_agents_in_repo(
                    repo, loop=loop, timeout=300, skip_if_present=True, dry_run=False, no_yes=False,
                )
                if install["status"] == "passed":
                    playwright = install["after"]
                    add(f"playwright_agents_{loop}", True, summarize_playwright(playwright), required=False)
        add("playwright_assets", playwright["available"], summarize_playwright(playwright), required=False)
        # 运行时（L2 @playwright/test + config）与浏览器（L3）分离检测，避免把「agents 定义存在」误判为「可跑 spec」
        runtime = playwright.get("runtime") or {}
        if runtime:
            runtime_ok = bool(runtime.get("declared") and runtime.get("installed") and playwright.get("configs"))
            missing = []
            if not runtime.get("declared"):
                missing.append("@playwright/test not declared")
            if not runtime.get("installed"):
                missing.append("@playwright/test not installed")
            if not playwright.get("configs"):
                missing.append("playwright.config missing")
            add(
                "playwright_runtime",
                runtime_ok,
                "runtime ready (pkg+installed+config)" if runtime_ok else "missing: " + "; ".join(missing),
                required=False,
                next_action="" if runtime_ok else "缺 Playwright 运行时；让 agent 执行 install-playwright-runtime（它会按 scope 判定是否需要）",
            )
            add(
                "playwright_browsers",
                bool(runtime.get("browsersInstalled")),
                f"{len(runtime.get('browsers', []))} browser binaries cached" if runtime.get("browsersInstalled") else "no browser binary cached",
                required=False,
                next_action="" if runtime.get("browsersInstalled") else "npx playwright install chromium",
            )
            # config 的 baseURL 与 services 的 web baseURL 一致性：config 用 process.env 占位则天然一致，硬编码则须与 env 相等
            config_hints = (playwright.get("configs") or [{}])[0].get("hints") or {}
            env_web_url = (qa_env_value(repo, "QA_WEB_BASE_URL") or "").strip()
            if env_web_url:
                env_refs = config_hints.get("env") or []
                hardcoded = config_hints.get("baseURL") or ""
                if "QA_WEB_BASE_URL" in env_refs:
                    add("playwright_config_baseurl", True, "baseURL 由 process.env.QA_WEB_BASE_URL 占位（与 services 一致）", required=False)
                elif hardcoded and hardcoded == env_web_url:
                    add("playwright_config_baseurl", True, f"baseURL={hardcoded} 与 env 一致", required=False)
                elif hardcoded:
                    add(
                        "playwright_config_baseurl",
                        False,
                        f"config baseURL={hardcoded} 与 env QA_WEB_BASE_URL={env_web_url} 不一致",
                        required=False,
                        next_action="修正 playwright.config 的 baseURL，或改用 process.env.QA_WEB_BASE_URL 占位",
                    )
                else:
                    add(
                        "playwright_config_baseurl",
                        False,
                        "config 未声明 baseURL 也未引用 process.env.QA_WEB_BASE_URL",
                        required=False,
                        next_action="在 playwright.config 里配置 baseURL: process.env.QA_WEB_BASE_URL",
                    )
            else:
                add(
                    "playwright_config_baseurl",
                    False,
                    "QA_WEB_BASE_URL 未配置，无法校验 config baseURL 一致性",
                    required=False,
                    next_action="在 config/env.shared 配置 QA_WEB_BASE_URL",
                )

        # 移动端（Android 真机）：仅在项目确有 android-real task 时进入检测。
        # 一个纯 Web 项目不该因为「没插手机」而失败——这与 services 探活
        # 「被标记为 required 才阻断」是同一个模式。
        require_mobile = bool(getattr(args, "require_mobile_e2e", False))
        if should_check_mobile(repo, require_mobile):
            mobile_tasks = _mobile_platform_tasks(repo)
            adb_info = _detect_adb_devices(repo)
            add(
                "mobile_adb",
                adb_info["available"],
                adb_info["adbPath"] or "未找到 adb；Android 真机测试需要它",
                required=require_mobile,
                next_action="" if adb_info["available"] else
                "安装 Android Platform Tools（含 adb）并加入 PATH："
                "https://developer.android.com/tools/releases/platform-tools",
            )
            if adb_info["available"]:
                if adb_info["authorized"]:
                    ok = True
                    detail = f"{len(adb_info['authorized'])} 台已授权设备：{', '.join(adb_info['authorized'])}"
                    action = ""
                elif adb_info["unauthorized"]:
                    ok = False
                    detail = f"{len(adb_info['unauthorized'])} 台设备未授权：{', '.join(adb_info['unauthorized'])}"
                    action = ("在手机屏幕上点「允许 USB 调试」；若弹窗未出现，拔插 USB 后执行 "
                              "adb kill-server && adb devices 重新触发授权")
                else:
                    ok = False
                    detail = "adb 可用但未发现设备"
                    action = "用 USB 连接 Android 设备，并在开发者选项中开启 USB 调试"
                add(
                    "mobile_devices",
                    ok,
                    detail,
                    required=bool(mobile_tasks) or require_mobile,
                    next_action=action,
                )
            mcp_registered = _mobile_mcp_registered(repo)
            add(
                "mobile_mcp",
                mcp_registered,
                "mobile-mcp 已注册" if mcp_registered else "mobile-mcp 未注册",
                required=require_mobile,
                next_action="" if mcp_registered else
                "claude mcp add mobile-mcp -- npx -y @mobilenext/mobile-mcp@latest",
            )

        # MySQL MCP 配置：仅 --config-mysql-mcp 时执行（doctor 默认只检查，不改配置），复用 configure_mysql_mcp 配两边
        if getattr(args, "config_mysql_mcp", False):
            configured = configure_mysql_mcp(repo, "both")
            cred = configured.get(AGENT_CLAUDE)
            if cred is not None and cred["generated"]:
                add("mysql_mcp_entry", True, cred["mcpPath"], required=False)
            elif cred is not None and "缺少" in cred.get("reason", ""):
                add("mysql_db_config", False, cred.get("reason", "未配置数据库"), required=False,
                    next_action="如验收涉及数据库，请在 config/env.shared 配置 QA_MYSQL_HOST/USER 后重跑")
            codex = configured.get(AGENT_CODEX)
            if codex is not None and codex["generated"]:
                add("mysql_codex_config", True, codex["codexConfigPath"], required=False)
            elif codex is not None and codex.get("reason"):
                add("mysql_codex_config", False, codex["reason"], required=False,
                    next_action="如验收涉及数据库，请在 config/env.shared 配置 QA_MYSQL_HOST/USER 后重跑")
        mysql = detect_mysql_mcp(
            repo,
            verify=bool(getattr(args, "verify_mysql_mcp", False)),
            timeout=getattr(args, "mysql_mcp_timeout", 12),
        )
        add("mysql_mcp_claude", bool(mysql.get("projectConfigured")), mysql.get("projectMcpPath", "missing"), required=False)
        add("mysql_mcp_codex", bool(mysql.get("codexConfigured")), mysql.get("codexConfigPath", "missing"), required=False)
        if mysql.get("verify"):
            verify_result = mysql["verify"]
            add(
                "mysql_mcp_verify",
                bool(verify_result.get("ok")),
                "patterns=" + (",".join(verify_result.get("matchedPatterns", [])) or "none"),
                category="database",
                next_action="MySQL 连接验证失败：请检查 config/env.shared 的 QA_MYSQL_HOST/PORT/DATABASE/USER/PASS 是否正确、数据库网络是否可达；配置正确后重跑 --verify-mysql-mcp",
            )
        accounts_data = load_accounts_config(repo)
        accounts = accounts_data.get("accounts", []) if isinstance(accounts_data.get("accounts"), list) else []
        add(
            "accounts_config",
            bool(accounts),
            f"{len(accounts)} accounts" if accounts else "账号凭证未配置（QA_USER_USERNAME/PASSWORD）",
            category="secrets",
            next_action=f"运行 {self_invocation()} init-project --repo ." if not accounts else "",
        )
        for account in accounts:
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("id") or account.get("role") or "account")
            for field, label in [("usernameEnv", "username"), ("passwordEnv", "password")]:
                env_key = str(account.get(field) or "").strip()
                add(
                    f"account:{account_id}:{label}",
                    bool(env_key and qa_env_value(repo, env_key)),
                    f"{env_key}: {'present' if env_key and qa_env_value(repo, env_key) else 'missing'}",
                    category="secrets",
                    next_action=f"在 .qa-agent/config/env.shared 中配置 {env_key}（也可写到 local/.env 覆盖）" if env_key and not qa_env_value(repo, env_key) else "",
                )
        services_data = load_services_config(repo)
        services = services_data.get("services", []) if isinstance(services_data.get("services"), list) else []
        add(
            "services_config",
            bool(services),
            f"{len(services)} services" if services else "missing config/services.json",
            category="services",
            next_action=f"运行 {self_invocation()} init-project --repo ." if not services else "",
        )
        for service in services:
            if not isinstance(service, dict):
                continue
            service_id = str(service.get("id") or service.get("name") or "service")
            env_key = str(service.get("baseUrlEnv") or "").strip()
            url = (qa_env_value(repo, env_key) if env_key else "") or str(service.get("baseUrl") or "")
            add(
                f"service:{service_id}:baseUrl",
                bool(url),
                f"{env_key or 'baseUrl'}: {'configured' if url else 'missing'}",
                category="services",
                next_action=(
                    f"在 config/env.shared 或 local/.env 中配置 {env_key}"
                    if env_key and not url
                    else ""
                ),
            )
        if getattr(args, "check_services", False):
            targets = default_local_stack_targets(repo, args)
            # 先探测
            for target in targets:
                probe_url = target.get("healthUrl") or target["url"]
                probe = probe_http_url_with_retry(probe_url, timeout=getattr(args, "service_timeout", 5))
                target["ok"] = bool(probe.get("ok"))
                target["detail"] = probe.get("detail")
                target["status"] = probe.get("status")
                target["server"] = probe.get("server") or ""
                target["probeUrl"] = probe_url
                # 声明了 healthUrl 就按健康检查的标准判：健康端点返回 4xx/5xx 说明端口上
                # 不是这个服务（或服务没起来）。baseUrl 根路径天然 404，不能当健康依据。
                if target.get("healthUrl") and isinstance(target["status"], int) and target["status"] >= 400:
                    target["ok"] = False
                    target["detail"] = f"{target['detail']}（healthUrl 返回 {target['status']}，非健康响应）"
            # 自动启动不可达的服务
            if getattr(args, "auto_start", False):
                targets = _auto_start_services(repo, args, targets)
            for target in targets:
                detail = f"{target.get('probeUrl', target['url'])} -> {target.get('detail', 'unknown')}"
                # 端口可达 ≠ 服务正确。带上对端身份，被无关进程占用端口时一眼就能看出。
                if target.get("server"):
                    detail += f" [Server: {target['server']}]"
                add(
                    f"service:{target['name']}:reachable",
                    bool(target.get("ok")) or not target.get("required", False),
                    detail,
                    category="services",
                    next_action=(
                        # 只有真的尝试过自动启动才说「已尝试」——之前无论有没有传
                        # --auto-start 都这么写，用户会以为自己漏看了什么。
                        (
                            f"已尝试自动启动 {target['name']} 服务但失败，请手动启动；"
                            if getattr(args, "auto_start", False)
                            else f"{target['name']} 服务不可达，请先启动它（或加 --auto-start 让 doctor 代为尝试）；"
                        )
                        + f"仍不可达时查看日志 .qa-agent/current/{target['name']}.out.log"
                          f"（前端常见：缺依赖，先 cd 到服务目录执行 npm install）。"
                        + "若探活『成功』但下游请求失败，核对上面的 Server 标识是否是预期进程——端口可能被无关进程占用；"
                          "在 config/services.json 为该服务声明 healthUrl 可获得真实的健康判定"
                        if target.get("required") and not target.get("ok")
                        else ""
                    ),
                )
            # 服务「可达」不等于它依赖的中间件版本支持所需能力。不阻断，但要让人看见——
            # 否则依赖该能力的断言（埋点、消息、队列）会整体不可信而无人察觉。
            for name, lines in scan_service_logs_for_capability_errors(
                repo, [str(t.get("name", "")) for t in targets]
            ).items():
                add(
                    f"service:{name}:capability",
                    False,
                    f"日志中有 {len(lines)} 条中间件能力/版本错误，最近一条：{lines[-1][:140]}",
                    category="services",
                    required=False,
                    next_action=(
                        "服务能启动不代表中间件版本支持它用到的命令（例如 Redis Streams 需要 "
                        "Redis ≥ 5.0）。依赖这类能力的断言整体不可信，请在报告里标注，"
                        f"不要当通过。完整日志：.qa-agent/current/{name}.out.log"
                    ),
                )
    else:
        add("repo_exists", False, str(repo))
    api_key_present = bool(qa_env_value(repo, DEFAULT_API_KEY_ENV)) if repo.exists() else bool(os.environ.get(DEFAULT_API_KEY_ENV))
    add(
        DEFAULT_API_KEY_ENV,
        api_key_present,
        "present" if api_key_present else "missing",
        category="secrets",
        # required=False：README 写明了「多模型交叉审查是可选增强，未配置时该阶段跳过」。
        # 之前按必须项拦截，是在拿选做当必做卡人。
        required=False,
        next_action="在 .qa-agent/local/.env 中配置 QA_AGENT_LLM_API_KEY（可选，不配则跳过交叉审查阶段）" if not api_key_present else "",
    )
    base_url = qa_env_value(repo, DEFAULT_BASE_URL_ENV) if repo.exists() else os.environ.get(DEFAULT_BASE_URL_ENV)
    add(DEFAULT_BASE_URL_ENV, True, "configured" if base_url else "not set（No built-in default; set it to enable multi-model review）", category="secrets")

    failed = [check for check in checks if not check["ok"]]
    for check in checks:
        mark = "OK" if check["ok"] else ("FAIL" if check.get("required", True) else "WARN")
        suffix = f"；处理建议={check['nextAction']}" if check.get("nextAction") else ""
        print(f"[{mark}] {check['name']}: {check['detail']}{suffix}")
    # 显式豁免：--ignore <check 名>（可重复）。被豁免的项仍然执行、仍然出现在输出里，
    # 只是不计入阻塞——用于「我知道它是这个状态，仍要继续」的场景。
    # 豁免必须可见可追溯，不能变成静默跳过。
    ignored = {str(x).strip() for x in (getattr(args, "ignore", None) or []) if str(x).strip()}
    blocking_failed = [
        check for check in failed
        if check.get("required", True) and check["name"] not in ignored
    ]
    waived = [check for check in failed if check.get("required", True) and check["name"] in ignored]
    if blocking_failed:
        print(f"\n必须修复项（{len(blocking_failed)}）：")
        for check in blocking_failed[:30]:
            print(f"- {check['name']}: {check.get('nextAction') or check['detail']}")
    if waived:
        print(f"\n已豁免项（{len(waived)}）—— 问题依旧存在，按 --ignore 要求不计入阻塞：")
        for check in waived[:10]:
            print(f"- {check['name']}: {check.get('nextAction') or check['detail']}")
    optional_failed = [check for check in failed if not check.get("required", True)]
    if optional_failed:
        print(f"\n可选检查项（{len(optional_failed)}）：")
        for check in optional_failed[:20]:
            print(f"- {check['name']}: {check.get('nextAction') or check['detail']}")

    # ── 结论和下一步 ──
    print("\n" + "=" * 50)
    if blocking_failed:
        print("结论：环境未就绪，请先修复以上必须项再重新运行 doctor。")
        print(f"\n下一步：")
        # 去重：多个检查项常常指向同一个动作（例如 qa_layout 与 qa_config 都指向
        # init-project），逐条打印会给出重复的 1. 2. 3.，看着像清单错了。
        seen_actions: set[str] = set()
        idx = 0
        for check in blocking_failed[:5]:
            action = check.get("nextAction") or check["detail"]
            if action in seen_actions:
                continue
            seen_actions.add(action)
            idx += 1
            print(f"  {idx}. {action}")
        if args.strict:
            # 出口指向 --ignore（显式、逐项、有记录），而不是「去掉 --strict」——
            # 后者是一刀切绕过，且 agent 侧固定带 --strict，那个建议根本用不上。
            print(
                "\n提示：以上必须项如果本次确实用不到（例如项目没有前端，web 服务本就起不来），"
                "可以逐项豁免，不必为了它把整个检查关掉：\n"
                f"  {self_invocation()} doctor --repo . --strict --check-services --ignore <检查名>\n"
                f"本次未通过的检查名：{', '.join(check['name'] for check in blocking_failed[:5])}\n"
                "被豁免的项仍会出现在输出里，只是不再阻塞。"
            )
    else:
        has_optional = optional_failed and len(optional_failed) > 0
        suffix = "（可选检查项不影响验收流程，建议后续处理）" if has_optional else ""
        print(f"结论：环境就绪，可以开始验收。{suffix}")
        print(f"\n下一步：")
        # 账号凭证分层读取：config/env.shared（团队共享）→ local/.env（覆盖），不再只读 local/.env
        missing = []
        if not qa_env_value(repo, "QA_USER_USERNAME"):
            missing.append("测试账号（QA_USER_USERNAME）")
        if not qa_env_value(repo, "QA_USER_PASSWORD"):
            missing.append("测试密码（QA_USER_PASSWORD）")
        if missing:
            print(f"  ? 检测到尚未配置：{', '.join(missing)}，请编辑 .qa-agent/config/env.shared 补全。")
        else:
            print("  对 Agent 说：使用 quality-assurance-agent，验收 <你的需求>")
    print("=" * 50)

    if args.json:
        write_json(
            Path(args.json).resolve(),
            {
                "generatedAt": utc_now(),
                "checks": checks,
                "environmentChecks": checks_to_environment_checks(checks),
                "mysqlMcp": mysql if repo.exists() else None,
            },
        )
    if blocking_failed and args.strict:
        raise SystemExit(1)


def summarize_playwright(playwright: dict[str, Any]) -> str:
    agents = len(playwright.get("agents", []))
    configs = len(playwright.get("configs", []))
    specs = playwright.get("specs", {}).get("count", 0)
    reports = len(playwright.get("reports", []))
    runtime = playwright.get("runtime") or {}
    if not playwright.get("available") and not runtime.get("declared"):
        return "not detected"
    parts = [f"agents={agents}", f"configs={configs}", f"specs={specs}", f"reports={reports}"]
    if runtime:
        parts.append(f"pkg={'yes' if runtime.get('declared') else 'no'}")
        parts.append(f"installed={'yes' if runtime.get('installed') else 'no'}")
        parts.append(f"browsers={'yes' if runtime.get('browsersInstalled') else 'no'}")
    return ", ".join(parts)


def ensure_branch(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    current = git_output(repo, ["branch", "--show-current"]).strip()
    name = args.branch or f"feature/ming-qa-{slugify(args.slug or 'work')}"
    if current == name:
        print(f"已在分支：{name}")
        return
    existing = git_output(repo, ["branch", "--list", name]).strip()
    command = ["git", "switch", name] if existing else ["git", "switch", "-c", name]
    result = run_cmd(command, repo)
    print(result["stdout"] or result["stderr"])
    if result["exitCode"] != 0:
        raise SystemExit(result["exitCode"])


def _path_matches_any(rel_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def enforce_repair_scope(repo: Path, paths: list[str], config_path: Path | None) -> None:
    """校验 repair 动作要提交的路径是否在配置允许的白名单/黑名单范围内。

    未配置 repair.allowedPaths / repair.deniedPaths 时不做任何限制（向后兼容）。
    命中黑名单或未命中非空白名单时 fail-closed：不执行任何 git add。
    """
    repair_scope: dict[str, Any] = {}
    if config_path and config_path.exists():
        repair_scope = load_config(config_path).get("repair", {}) or {}
    allowed_paths = repair_scope.get("allowedPaths") or []
    denied_paths = repair_scope.get("deniedPaths") or []
    if not allowed_paths and not denied_paths:
        return
    violations = []
    for raw_path in paths:
        candidate = Path(raw_path)
        abs_path = candidate if candidate.is_absolute() else (repo / candidate)
        rel_path = _repo_rel(repo, abs_path.resolve())
        if denied_paths and _path_matches_any(rel_path, denied_paths):
            violations.append(f"{rel_path}（命中 deniedPaths）")
            continue
        if allowed_paths and not _path_matches_any(rel_path, allowed_paths):
            violations.append(f"{rel_path}（未命中 allowedPaths）")
    if violations:
        raise QaAgentError(
            "atomic-commit 被 repair 路径白名单/黑名单拒绝，未执行任何 git add：\n  "
            + "\n  ".join(violations)
            + f"\n当前 allowedPaths={allowed_paths} deniedPaths={denied_paths}"
        )


def _record_qa_fix(repo: Path, message: str, body: str | None, paths: list[str]) -> None:
    """将一次 QA 修复（atomic-commit）记录到 .qa-agent/current/qa-fixes.json。"""
    fixes_path = repo / ".qa-agent" / "current" / "qa-fixes.json"
    data = read_json(fixes_path) if fixes_path.exists() else {"version": "1.0", "fixes": []}
    data.setdefault("version", "1.0")
    data.setdefault("fixes", [])
    data["fixes"].append({
        "message": message,
        "body": body or "",
        "paths": [str(p) for p in paths],
        "committedAt": utc_now(),
    })
    fixes_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(fixes_path, data)


def atomic_commit(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    if not args.paths:
        raise QaAgentError("atomic-commit requires at least one path")
    config_path = Path(args.config).resolve() if getattr(args, "config", None) else None
    enforce_repair_scope(repo, args.paths, config_path)
    add = run_cmd(["git", "add", "--", *args.paths], repo)
    if add["exitCode"] != 0:
        print(add["stderr"], file=sys.stderr)
        raise SystemExit(add["exitCode"])
    commit_args = ["git", "commit", "-m", args.message]
    if args.body:
        commit_args.extend(["-m", args.body])
    commit = run_cmd(commit_args, repo, timeout=120)
    print(commit["stdout"] or commit["stderr"])
    if commit["exitCode"] != 0:
        raise SystemExit(commit["exitCode"])
    _record_qa_fix(repo, args.message, getattr(args, "body", None), args.paths)


def badge(value: str) -> str:
    safe = html.escape(str(value or "N/A"))
    css = re.sub(r"[^a-zA-Z0-9_-]", "", str(value or "").replace(" ", "-"))
    return f'<span class="badge {css}">{safe}</span>'


def table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        cells = []
        for cell in row:
            if isinstance(cell, str) and cell.startswith("<span"):
                cells.append(f"<td>{cell}</td>")
            else:
                cells.append(f"<td>{html.escape(str(cell))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def format_utc_timestamp(value: float | None) -> str:
    if value is None:
        return "N/A"
    return dt.datetime.fromtimestamp(value, tz=_CN_TZ).isoformat()


def collect_spec_task_entries(spec_tasks_data: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any]] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("sourceCaseId") and node.get("targetFile"):
                key = (node.get("id"), node.get("sourceCaseId"), node.get("targetFile"), node.get("testName"))
                if key not in seen:
                    seen.add(key)
                    entries.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(spec_tasks_data)
    return entries


# 按需产出的报告源：只在「本轮重新生成用例」那条路径上存在。
#
# test-cases.generated.json 由用例设计阶段产出；而复用已 promote 的用例集
# （.qa-agent/cases/*.json → current/test-cases.json，promote-cases 的设计意图，
# 回归运行正是这个场景）时本来就没有这个文件。把它的缺失当作「报告陈旧」会让
# 任何复用路径的运行永远判 Not Ready。
#
# 注意只放宽「缺失」：若它存在却比报告新，仍按陈旧拦截——那才是真正的陈旧。
OPTIONAL_REPORT_SOURCES = {"generated-cases"}


def collect_report_source_paths(args: argparse.Namespace) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    for label, value in [
        ("cases", getattr(args, "cases", None)),
        ("generated-cases", str(Path(args.cases).resolve().parent / "test-cases.generated.json") if getattr(args, "cases", None) else None),
        ("run", getattr(args, "run", None)),
        ("review", getattr(args, "review", None)),
        ("code-review", getattr(args, "code_review", None)),
        ("risk-analysis", getattr(args, "risk_analysis", None)),
        ("completion-check", getattr(args, "completion_check", None)),
        ("readiness-check", getattr(args, "readiness_check", None)),
        ("spec-tasks", getattr(args, "spec_tasks", None)),
    ]:
        if value:
            paths.append((label, Path(value).resolve()))
    return paths


def build_scope_change_ledger_data(
    generated_cases_data: dict[str, Any],
    current_cases_data: dict[str, Any],
) -> dict[str, Any]:
    generated_cases = list(generated_cases_data.get("cases", []))
    current_case_map = {
        str(case.get("id", "")): case
        for case in current_cases_data.get("cases", [])
        if str(case.get("id", ""))
    }
    entries: list[dict[str, Any]] = []
    retained_ids: list[str] = []
    excluded_ids: list[str] = []
    for case in generated_cases:
        case_id = str(case.get("id", ""))
        current_case = current_case_map.get(case_id)
        retained = current_case is not None
        if retained:
            retained_ids.append(case_id)
        else:
            excluded_ids.append(case_id)
        entries.append(
            {
                "caseId": case_id,
                "title": case.get("title", ""),
                "priority": case.get("priority", ""),
                "module": case.get("module", ""),
                "layer": case.get("layer", ""),
                "generatedStatus": case.get("status", ""),
                "currentStatus": current_case.get("status", "") if current_case else "",
                "scopeDecision": "retained" if retained else "excluded",
                "reason": "已进入本轮最终确认并完成执行" if retained else "draft case 未纳入本轮最终确认范围",
                "traceability": case.get("traceability", []),
            }
        )
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "summary": {
            "generatedCases": len(generated_cases),
            "retainedCases": len(retained_ids),
            "excludedCases": len(excluded_ids),
            "retainedCaseIds": retained_ids,
            "excludedCaseIds": excluded_ids,
        },
        "source": {
            "generatedCasesGeneratedAt": generated_cases_data.get("metadata", {}).get("generatedAt"),
            "currentCasesGeneratedAt": current_cases_data.get("metadata", {}).get("generatedAt"),
        },
        "entries": entries,
    }


def build_handoff_checkpoint_data(
    *,
    completion_data: dict[str, Any],
    readiness_data: dict[str, Any],
    code_review_data: dict[str, Any],
    report_freshness_data: dict[str, Any],
    scope_change_ledger: dict[str, Any],
    report_path: Path,
    manifest_path: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    completion_summary = completion_data.get("summary", {}) or {}
    readiness_summary = readiness_data.get("summary", {}) or {}
    scope_summary = scope_change_ledger.get("summary", {}) or {}
    completed_items: list[str] = []
    if completion_data.get("status") == "passed":
        completed_items.append(
            "完成门通过：{cases} 个 case / {tasks} 个 task".format(
                cases=completion_summary.get("casesChecked", 0),
                tasks=completion_summary.get("tasksChecked", 0),
            )
        )
    if readiness_data.get("status") == "passed":
        completed_items.append(
            "就绪门通过：completion={completion}，code-review={review}，reportFreshness={freshness}".format(
                completion=readiness_summary.get("completionStatus", "missing"),
                review=readiness_summary.get("codeReviewStatus", "missing"),
                freshness=readiness_summary.get("reportFreshnessStatus", "missing"),
            )
        )
    if scope_summary.get("excludedCases", 0):
        completed_items.append(
            "范围收口：已分离 {count} 个 draft case".format(count=scope_summary.get("excludedCases", 0))
        )
    blockers = [
        finding
        for finding in readiness_data.get("findings", [])
        if str(finding.get("severity", "")).lower() == "fail"
    ]
    if not blockers:
        blockers = [
            finding
            for finding in code_review_data.get("findings", [])
            if str(finding.get("severity", "")).lower() in {"critical", "high", "fail"}
        ]
    next_step = (
        "如需继续，优先回收 scope-change ledger 中的 excluded draft case 或补充新的 confirmed case。"
        if scope_summary.get("excludedCases", 0)
        else "如需继续，按已确认范围继续新增 case、补充回归或进入下一轮变更。"
    )
    return {
        "version": "1.0",
        "generatedAt": utc_now(),
        "stage": "finalization",
        "currentStep": "报告重渲染与门禁复跑",
        "completedItems": completed_items,
        "nextStep": next_step,
        "blockers": blockers,
        "artifacts": {
            "report": str(report_path),
            "reportManifest": str(manifest_path),
            "scopeChangeLedger": str(ledger_path),
        },
        "gateSummary": {
            "completion": completion_summary,
            "readiness": readiness_summary,
            "reportFreshness": report_freshness_data,
            "codeReviewStatus": code_review_data.get("status", "missing"),
        },
        "scopeSummary": scope_summary,
    }


def collect_run_commands(run_data: dict[str, Any]) -> list[dict[str, Any]]:
    commands = list(run_data.get("commands", []))
    for iteration in run_data.get("iterations", []):
        for gate in iteration.get("gates", []):
            for cmd in gate.get("commands", []):
                enriched = dict(cmd)
                enriched.setdefault("gate", gate.get("gate", ""))
                commands.append(enriched)
    return commands


def _server_signature(headers: Any) -> str:
    """取对端的 Server 头（连 Content-Type 一起），用于识别「端口上到底是谁」。"""
    if headers is None:
        return ""
    try:
        server = (headers.get("Server") or "").strip()
        content_type = (headers.get("Content-Type") or "").strip()
    except Exception:  # noqa: BLE001 - 头字段不可读不该影响探活结论
        return ""
    parts = [p for p in (server, content_type) if p]
    return " / ".join(parts)


def probe_http_url_with_retry(url: str, timeout: int = 5, attempts: int = 3,
                              budget_seconds: float = 20.0) -> dict[str, Any]:
    """带预热与重试的探活。

    首次请求可能触发服务端按需编译（Next.js dev server 等），单次探测会把「正在编译」
    误判成不可达。使用者实测到的就是：「doctor 报 timed out，同一个 URL 直接 curl 返回
    200」，而 --strict 因此被阻断，得手动再跑一次才过。

    第一次请求同时充当预热，失败后按递增间隔重试；整体受 budget_seconds 约束，
    服务真的没起时也不会把 doctor 拖太久。
    """
    last: dict[str, Any] = {"url": url, "ok": False, "status": None, "detail": "not probed"}
    deadline = time.time() + budget_seconds
    tried = 0
    for attempt in range(1, max(1, attempts) + 1):
        tried = attempt
        last = probe_http_url(url, timeout=timeout)
        if last.get("ok"):
            break
        if attempt >= attempts or time.time() >= deadline:
            break
        time.sleep(min(1.0 * attempt, 2.0))
    last["attempts"] = tried
    if tried > 1 and not last.get("ok"):
        last["detail"] = f"{last.get('detail')}（已重试 {tried} 次）"
    return last


# 中间件「版本能力不匹配」的典型日志签名：应用能起来，但持续刷这类错误。
# 使用者实测踩到的正是这个——test 环境 Redis 4.0.8，而项目用 Redis Streams 做埋点
# （XADD/XREADGROUP 需 Redis ≥ 5.0）。服务照常启动，doctor 只检查「可达」，一路绿灯，
# 直到有人翻服务日志才发现埋点链路整体不可信。
_MIDDLEWARE_CAPABILITY_PATTERNS = (
    "unknown command",
    "err unknown",
    "unknown subcommand",
    "is not supported in this version",
    "wrong number of arguments for",
)


def scan_service_logs_for_capability_errors(repo: Path, services: list[str]) -> dict[str, list[str]]:
    """扫服务日志找中间件能力不匹配的证据，返回 {服务名: [命中行]}。

    零配置：服务日志本来就落在 .qa-agent/current/<name>.out.log。不去直连中间件探测版本，
    因为那需要新增一套中间件配置面（主机/端口/口令），而项目用哪种中间件、跑在哪个版本，
    只有应用自己知道。
    """
    hits: dict[str, list[str]] = {}
    for name in services:
        if not name:
            continue
        log_file = repo / ".qa-agent" / "current" / f"{name}.out.log"
        if not log_file.exists():
            continue
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matched = [
            line.strip()[:200]
            for line in text.splitlines()
            if any(pattern in line.lower() for pattern in _MIDDLEWARE_CAPABILITY_PATTERNS)
        ]
        if matched:
            hits[name] = matched[-5:]
    return hits


def probe_http_url(url: str, timeout: int = 5) -> dict[str, Any]:
    if not url:
        return {"url": url, "ok": False, "status": None, "detail": "missing url"}
    started = time.time()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ming-qa-local-stack/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return {
                "url": url,
                "ok": 200 <= int(status) < 500,
                "status": status,
                "durationSeconds": round(time.time() - started, 3),
                "detail": "reachable",
                # 端口可达 ≠ 服务正确。带上对端身份，误占端口的进程才看得出来。
                "server": _server_signature(getattr(response, "headers", None)),
            }
    except urllib.error.HTTPError as exc:
        return {
            "url": url,
            "ok": 200 <= int(exc.code) < 500,
            "status": exc.code,
            "durationSeconds": round(time.time() - started, 3),
            "detail": f"http {exc.code}",
            "server": _server_signature(getattr(exc, "headers", None)),
        }
    except Exception as exc:  # noqa: BLE001 - CLI should report probe failures without stack traces.
        return {
            "url": url,
            "ok": False,
            "status": None,
            "durationSeconds": round(time.time() - started, 3),
            "detail": str(exc),
        }


def _discover_project_path_hint(repo: Path, hint: str) -> Path:
    """Best-effort project root for a check-local-stack service hint (h5/admin/backend).

    Prefers a discovered project (pom.xml/package.json) whose name or root
    directory matches the hint; falls back to `repo / hint` so callers relying
    on `path.exists()` keep their previous behavior when no project matches.
    """
    from qa_core.project_manifest import discover_projects

    for project in discover_projects(repo):
        haystack = f"{project.name} {project.root.name}".lower()
        if hint in haystack:
            return project.root
    return repo / hint


def default_local_stack_targets(repo: Path, args: argparse.Namespace | None = None) -> list[dict[str, Any]]:
    args = args or argparse.Namespace()
    load_local_env(repo)
    targets: list[dict[str, Any]] = []
    services = load_services_config(repo).get("services", [])
    if isinstance(services, list) and services:
        required = set(filter(None, str(getattr(args, "required", "") or "").split(",")))
        for service in services:
            if not isinstance(service, dict):
                continue
            name = str(service.get("id") or service.get("name") or "").strip()
            if not name:
                continue
            env_key = str(service.get("baseUrlEnv") or "").strip()
            explicit_url = getattr(args, f"{name}_url", None)
            url = explicit_url or (qa_env_value(repo, env_key) if env_key else "") or str(service.get("baseUrl") or "")
            if not url:
                continue
            health_url = str(service.get("healthUrl") or "").strip()
            targets.append(
                {
                    "name": name,
                    "url": url.rstrip("/"),
                    "projectPath": str(repo),
                    "envKey": env_key,
                    "required": (name in required) if required else bool(service.get("required", False)),
                    # 探活要用的地址：声明了 healthUrl 就用它——baseUrl 根路径对很多服务
                    # 本来就返回 404，「有响应」证明不了端口上是正确的那个服务。
                    "healthUrl": health_url.rstrip("/"),
                    "readySignal": str(service.get("readySignal") or "").strip(),
                }
            )
        if targets:
            return targets
    definitions = [
        (
            "h5",
            "QA_AGENT_H5_URL",
            getattr(args, "h5_url", None) or os.environ.get("QA_AGENT_H5_URL") or "http://127.0.0.1:3002",
            _discover_project_path_hint(repo, "h5"),
        ),
        (
            "admin",
            "QA_AGENT_ADMIN_URL",
            getattr(args, "admin_url", None) or os.environ.get("QA_AGENT_ADMIN_URL") or "http://127.0.0.1:4010",
            _discover_project_path_hint(repo, "admin"),
        ),
        (
            "backend",
            "QA_AGENT_BACKEND_URL",
            getattr(args, "backend_url", None) or os.environ.get("QA_AGENT_BACKEND_URL") or "http://127.0.0.1:8080/api",
            _discover_project_path_hint(repo, "backend"),
        ),
    ]
    required = set(filter(None, str(getattr(args, "required", "h5,admin,backend")).split(",")))
    for name, env_key, url, path in definitions:
        if path.exists() or getattr(args, f"{name}_url", None):
            targets.append(
                {
                    "name": name,
                    "url": url.rstrip("/"),
                    "projectPath": str(path),
                    "envKey": env_key,
                    "required": name in required,
                }
            )
    return targets


# ── 自动启动服务 ────────────────────────────────────────────


def _port_listening(port: int) -> int | None:
    """返回占用指定端口的进程 PID，未占用返回 None。"""
    if os.name == "nt":
        try:
            out = subprocess.check_output(["netstat", "-ano"], text=True, timeout=5)
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    return int(parts[-1])
        except Exception:
            pass
        return None
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True, timeout=5)
        return int(out.strip().splitlines()[0]) if out.strip() else None
    except Exception:
        return None


def _kill_pid(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(pid, 9)
        time.sleep(1)
        return True
    except Exception:
        return False


def _service_from_config(services: list[dict[str, Any]], service_id: str) -> dict[str, Any] | None:
    for svc in services:
        if not isinstance(svc, dict):
            continue
        sid = svc.get("id") or svc.get("name", "")
        if sid == service_id:
            return svc
    return None


def _auto_start_one(
    target: dict[str, Any],
    services: list[dict[str, Any]],
    repo: Path,
    timeout: int,
    no_kill: bool,
) -> dict[str, Any]:
    """尝试启动一个服务。返回 {"ok": bool, "detail": str, "started": bool}。"""
    name = target["name"]
    url = target["url"]
    svc = _service_from_config(services, name)
    if not svc:
        return {"ok": False, "detail": f"未找到 {name} 服务的 startCmd 配置（检查 config/services.json / services.local.json）", "started": False}
    start_cmd = svc.get("startCmd")
    if not start_cmd:
        return {"ok": False, "detail": f"{name} 未配置 startCmd，无法自动启动", "started": False}
    work_dir = svc.get("dir", "")
    if work_dir:
        work_dir = str(repo / work_dir) if not Path(work_dir).is_absolute() else work_dir
    else:
        work_dir = target.get("projectPath", str(repo))
    ready_signal = svc.get("readySignal", "")
    health_url = svc.get("healthUrl", url)
    if not health_url and url:
        health_url = url
    platform_cmd = svc.get("platformWindows", "") if os.name == "nt" else svc.get("platformUnix", "")

    # 1. 如果已可达，跳过
    probe = probe_http_url(health_url, timeout=5)
    if probe.get("ok"):
        return {"ok": True, "detail": "already reachable", "started": False}

    # 2. 端口冲突处理
    port = _extract_port(health_url or url)
    if port:
        pid = _port_listening(port)
        if pid is not None:
            health_probe = probe_http_url(health_url, timeout=3)
            if health_probe.get("ok"):
                return {"ok": True, "detail": f"port {port} occupied by working {name}", "started": False}
            if no_kill:
                return {"ok": False, "detail": f"port {port} occupied (PID {pid})，--no-kill 模式跳过", "started": False}
            print(f"  [doctor] 端口 {port} 被 PID {pid} 占用，尝试终止…")
            _kill_pid(pid)
            time.sleep(2)

    # 2.5 前端 npm 命令：缺 node_modules 则先 npm install
    if _is_npm_cmd(start_cmd) and not _ensure_node_modules(work_dir):
        return {"ok": False, "detail": f"{name} 缺少 node_modules 且 npm install 失败", "started": False}

    # 3. 启动
    log_file = repo / ".qa-agent" / "current" / f"{name}.out.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    effective_cmd = platform_cmd or start_cmd
    print(f"  [doctor] 启动 {name}: {_redact_cmd(effective_cmd)}")
    print(f"  [doctor] 日志: {log_file}")
    try:
        # Windows: 显式修正 TMP/TEMP 环境变量，避免 Git Bash 下 java.io.tmpdir=C:\Windows 导致 maven argfile 写入失败
        spawn_env = None
        if os.name == "nt":
            userprofile = os.environ.get("USERPROFILE", "")
            user_tmp = os.path.join(userprofile, "AppData", "Local", "Temp") if userprofile else os.environ.get("TMP", tempfile.gettempdir())
            spawn_env = os.environ.copy()
            spawn_env["TMP"] = user_tmp
            spawn_env["TEMP"] = user_tmp
            java_tool_opts = spawn_env.get("JAVA_TOOL_OPTIONS", "")
            if f"-Djava.io.tmpdir=" not in java_tool_opts:
                spawn_env["JAVA_TOOL_OPTIONS"] = f"{java_tool_opts} -Djava.io.tmpdir={user_tmp}" if java_tool_opts else f"-Djava.io.tmpdir={user_tmp}"
        if platform_cmd:
            subprocess.Popen(platform_cmd, cwd=work_dir, shell=True,
                             stdout=open(str(log_file), "w"), stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        elif os.name == "nt" and _is_maven_cmd(start_cmd):
            # Windows + Maven: 通过 cmd.exe /c + 修正 TMP 环境 启动
            cmd_line = f"cmd.exe /c {start_cmd} > \"{log_file}\" 2>&1"
            subprocess.Popen(cmd_line, cwd=work_dir, shell=True, env=spawn_env,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            subprocess.Popen(start_cmd, cwd=work_dir, shell=True, env=spawn_env,
                             stdout=open(str(log_file), "w"), stderr=subprocess.STDOUT,
                             **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
    except Exception as exc:
        return {"ok": False, "detail": f"启动失败: {exc}", "started": False}

    # 4. 等待 ready 信号
    started = time.time()
    last_print = 0
    last_log_pos = 0
    while time.time() - started < timeout:
        # 实时打印日志新增内容，让用户看到服务启动进度
        if log_file.exists():
            try:
                with open(str(log_file), encoding="utf-8", errors="replace") as fh:
                    fh.seek(last_log_pos)
                    new_content = fh.read()
                    last_log_pos = fh.tell()
                for line in new_content.strip().splitlines():
                    print(f"  [doctor] {name}: {line}")
            except Exception:
                pass
        # 先检查 ready signal（从日志中）——更快的检测方式
        if ready_signal and log_file.exists():
            try:
                with open(str(log_file), encoding="utf-8", errors="replace") as fh:
                    log_content = fh.read()
                if ready_signal in log_content:
                    time.sleep(1)  # 额外等1秒让服务初始化完成
                    probe2 = probe_http_url(health_url, timeout=5)
                    if probe2.get("ok"):
                        elapsed = round(time.time() - started, 1)
                        return {"ok": True, "detail": f"启动成功 ({elapsed}s)", "started": True}
            except Exception:
                pass
        # fallback：直接探测 health URL
        probe2 = probe_http_url(health_url, timeout=3)
        if probe2.get("ok"):
            elapsed = round(time.time() - started, 1)
            return {"ok": True, "detail": f"启动成功 ({elapsed}s)", "started": True}
        elapsed = round(time.time() - started, 1)
        if elapsed - last_print >= 10:
            print(f"  [doctor] 等待 {name} 就绪… ({elapsed}s / {timeout}s)")
            last_print = elapsed
        time.sleep(2)
    return {"ok": False, "detail": f"启动超时 ({timeout}s)，请检查日志 {log_file}", "started": True}


def _auto_start_services(
    repo: Path, args: argparse.Namespace, targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """自动启动不可达的服务。返回修正后的 targets 列表。"""
    services = load_services_config(repo).get("services", [])
    if not isinstance(services, list):
        services = []
    timeout = getattr(args, "start_timeout", 120)
    no_kill = getattr(args, "no_kill", False)
    print(f"\n[doctor] --auto-start 模式：检查并启动 {len(targets)} 个服务…")
    for target in targets:
        if target.get("ok"):
            continue
        result = _auto_start_one(target, services, repo, timeout, no_kill)
        if result["ok"]:
            target["ok"] = True
            target["detail"] = result["detail"]
            print(f"  [doctor] {target['name']}: OK ({result['detail']})")
        else:
            target["detail"] = result["detail"]
            print(f"  [doctor] {target['name']}: FAIL ({result['detail']})")
    return targets


def _extract_port(url: str) -> int | None:
    import re as _re
    m = _re.search(r":(\d+)", url)
    return int(m.group(1)) if m else None


def _is_maven_cmd(cmd: str) -> bool:
    return bool(cmd) and ("mvn " in cmd or cmd.startswith("mvn ") or cmd == "mvn")


def _is_npm_cmd(cmd: str) -> bool:
    return bool(cmd) and ("npm " in cmd or cmd.startswith("npm ") or cmd == "npm")


def _ensure_node_modules(work_dir: str) -> bool:
    """检测 node_modules，缺则 npm install。返回是否已就绪。"""
    nm = Path(work_dir) / "node_modules"
    if nm.exists():
        return True
    print(f"  [doctor] 未检测到 node_modules，执行 npm install（cwd={work_dir}）…")
    try:
        # 不带 capture_output，让 npm install 的进度实时打印到终端
        proc = subprocess.run("npm install", cwd=work_dir, shell=True, timeout=600)
        if proc.returncode == 0 and (Path(work_dir) / "node_modules").exists():
            print(f"  [doctor] npm install 完成")
            return True
        print(f"  [doctor] npm install 失败（exit={proc.returncode}）")
        return False
    except Exception as exc:
        print(f"  [doctor] npm install 异常：{exc}")
        return False


def _redact_cmd(cmd: str) -> str:
    max_len = 100
    return cmd if len(cmd) <= max_len else cmd[:max_len] + "…"


# ──


def check_local_stack(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    targets = default_local_stack_targets(repo, args)
    checks: list[dict[str, Any]] = []
    for target in targets:
        probe_url = target["url"] + str(getattr(args, "health_path", "") or "")
        result = probe_http_url(probe_url, timeout=getattr(args, "timeout", 5))
        checks.append({**target, **result, "probeUrl": probe_url})
    required_failed = [check for check in checks if check.get("required") and not check.get("ok")]
    summary = {
        "generatedAt": utc_now(),
        "repo": str(repo),
        "status": "passed" if not required_failed else "blocked",
        "mode": "real-local",
        "checks": checks,
        "environmentChecks": [
            {
                "id": "LOCAL-" + str(check.get("name", "")).upper(),
                "name": f"local stack:{check.get('name')}",
                "status": "passed" if check.get("ok") else ("blocked" if check.get("required") else "skipped"),
                "detail": f"{check.get('probeUrl')} -> {check.get('detail')}",
                "requiredFor": ["real-local-e2e"] if check.get("required") else [],
            }
            for check in checks
        ],
    }
    if args.json:
        write_json(Path(args.json).resolve(), summary)
    for check in checks:
        mark = "OK" if check.get("ok") else ("BLOCKED" if check.get("required") else "SKIP")
        print(f"[{mark}] {check.get('name')}: {check.get('probeUrl')} ({check.get('detail')})")
    if required_failed and args.strict:
        raise SystemExit(1)


def classify_browser_event(event: dict[str, Any]) -> dict[str, Any]:
    text = str(event.get("text") or event.get("failure") or event.get("url") or "")
    url = str(event.get("url") or "")
    lower = (text + " " + url).lower()
    event_type = str(event.get("type", "")).lower()
    status = event.get("status")
    if "webpack-hmr" in lower or "/@vite/client" in lower or "[vite]" in lower or "websocket connection" in lower:
        return {"category": "dev-server-noise", "severity": "info", "reason": "HMR/dev-server transport"}
    if "net::err_aborted" in lower and ("/node_modules/.vite/" in lower or "cdn." in lower or ".mp4" in lower):
        return {"category": "third-party-resource-noise", "severity": "info", "reason": "aborted static/media resource"}
    if event_type == "failed" and "/api/" in lower and "net::err_aborted" in lower:
        return {"category": "non-blocking-warning", "severity": "warn", "reason": "API request was aborted; correlate with later successful responses"}
    if event_type == "failed" and "/api/" in lower:
        return {"category": "business-api-error", "severity": "error", "reason": "API request failed"}
    if isinstance(status, int) and status >= 500:
        return {"category": "business-api-error", "severity": "error", "reason": f"HTTP {status}"}
    if "apierror" in lower or "failed to fetch" in lower:
        return {"category": "non-blocking-warning", "severity": "warn", "reason": "fetch error observed; correlate with later successful responses"}
    if event_type == "error":
        return {"category": "blocking-error", "severity": "error", "reason": "browser console error"}
    return {"category": "normal", "severity": "info", "reason": "not classified as a problem"}


def summarize_browser_noise(flow: dict[str, Any]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for event in flow.get("console", []) + flow.get("requests", []):
        classified = classify_browser_event(event)
        events.append({**event, **classified})
    counts: dict[str, int] = {}
    blocking = 0
    for event in events:
        category = str(event.get("category", "normal"))
        counts[category] = counts.get(category, 0) + 1
        if event.get("severity") == "error" and category not in {"dev-server-noise", "third-party-resource-noise"}:
            blocking += 1
    return {"counts": counts, "blockingErrorCount": blocking, "events": events[:80]}


def real_local_readiness(summary: dict[str, Any], risks: list[dict[str, Any]]) -> dict[str, Any]:
    if summary.get("status") != "passed":
        return {"decision": "Not Ready", "status": "failed", "reason": "real-local E2E did not pass"}
    blocking = [risk for risk in risks if risk.get("level") == "blocking"]
    if blocking:
        return {"decision": "Not Ready", "status": "failed", "reason": f"{len(blocking)} blocking risks"}
    non_blocking = [risk for risk in risks if risk.get("level") in {"risk", "gap"}]
    if non_blocking:
        return {"decision": "Conditionally Ready", "status": "warn", "reason": f"{len(non_blocking)} non-blocking gaps recorded"}
    return {"decision": "Ready", "status": "passed", "reason": "all real-local checks passed without recorded gaps"}


def normalize_real_local_e2e_summary(summary: dict[str, Any], run_path: Path | None = None) -> dict[str, Any]:
    flow = summary.get("realLocalFlow", {})
    h5 = flow.get("h5", {})
    admin = flow.get("admin", {})
    db = flow.get("dbFinal", {})
    existing = summary.get("existingPlaywrightSuites", {})
    run_dir = Path(summary.get("runDir") or (run_path.parent if run_path else ".")).resolve()
    risks = [
        {
            "id": "R1",
            "level": "gap",
            "summary": "H5 real-local flow may use test-profile/dev login; authentication provider login is a separate capability.",
        },
        {
            "id": "R2",
            "level": "gap",
            "summary": "If a QA seed/review candidate is used, natural async candidate generation still needs separate evidence.",
        },
        {
            "id": "R3",
            "level": "risk",
            "summary": "Existing Playwright suites can be mock-backed supplemental regression evidence, not a replacement for real backend E2E.",
        },
    ]
    if summary.get("status") == "failed":
        risks.insert(0, {"id": "R0", "level": "blocking", "summary": "Main real-local E2E status is failed."})
    business_flows = [
        {
            "id": "BF-001",
            "priority": "P0",
            "actor": "达人端用户",
            "operationPath": "H5 登录 -> Earn 任务列表 -> 任务详情",
            "mapping": "real-local E2E",
            "status": h5.get("status", summary.get("status", "unknown")),
            "evidence": "H5 browser steps and API responses",
        },
        {
            "id": "BF-002",
            "priority": "P0",
            "actor": "达人端用户",
            "operationPath": "提交 mock TikTok 视频链接",
            "mapping": "real backend API",
            "status": "passed" if db.get("submissionId") else h5.get("status", "unknown"),
            "evidence": f"submissionId={db.get('submissionId', 'N/A')}, videoId={db.get('videoId', 'N/A')}",
        },
        {
            "id": "BF-003",
            "priority": "P0",
            "actor": "后台管理员",
            "operationPath": "Admin 登录 -> 视频审核列表 -> Approve",
            "mapping": "real-local E2E",
            "status": admin.get("status", summary.get("status", "unknown")),
            "evidence": f"reviewVideoId={db.get('reviewVideoId', 'N/A')}, reviewStatus={db.get('reviewStatus', 'N/A')}",
        },
        {
            "id": "BF-004",
            "priority": "P0",
            "actor": "系统",
            "operationPath": "审核通过 -> 投稿状态/播放量/奖励落库",
            "mapping": "DB verification",
            "status": "passed" if db.get("submissionStatus") and db.get("reviewStatus") else "unknown",
            "evidence": f"submissionStatus={db.get('submissionStatus', 'N/A')}, reward={db.get('rewardAmount', 'N/A')}, views={db.get('viewCount', 'N/A')}",
        },
    ]
    h5_noise = summarize_browser_noise(h5)
    admin_noise = summarize_browser_noise(admin)
    artifacts = [
        {"name": Path(path).name, "path": path, "exists": Path(path).exists()}
        for path in summary.get("artifacts", [])
    ]
    screenshots = [
        path
        for path in summary.get("artifacts", [])
        if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    ]
    quality_gates = [
        {
            "id": "G0",
            "category": "environment",
            "name": "real-local services",
            "status": "passed" if summary.get("environment") else "unknown",
            "summary": json.dumps(summary.get("environment", {}), ensure_ascii=False),
        },
        {
            "id": "G1",
            "category": "e2e",
            "name": "H5 real-local flow",
            "status": h5.get("status", "unknown"),
            "summary": f"{len(h5.get('steps', []))} browser steps",
        },
        {
            "id": "G2",
            "category": "e2e",
            "name": "Admin real-local review flow",
            "status": admin.get("status", "unknown"),
            "summary": f"{len(admin.get('steps', []))} browser steps",
        },
        {
            "id": "G3",
            "category": "database",
            "name": "DB final state",
            "status": "passed" if db else "unknown",
            "summary": f"submissionId={db.get('submissionId', 'N/A')}, reviewVideoId={db.get('reviewVideoId', 'N/A')}",
        },
        {
            "id": "G4",
            "category": "supplemental-regression",
            "name": "existing Playwright suites",
            "status": "passed" if all(v.get("status") == "passed" for v in existing.values()) and existing else "unknown",
            "summary": json.dumps(existing, ensure_ascii=False),
        },
    ]
    normalized = {
        "version": "1.0",
        "generatedAt": utc_now(),
        "sourceRun": str(run_path) if run_path else None,
        "runDir": str(run_dir),
        "status": summary.get("status", "unknown"),
        "environment": summary.get("environment", {}),
        "businessFlows": business_flows,
        "qualityGates": quality_gates,
        "dbFinal": db,
        "steps": {"h5": h5.get("steps", []), "admin": admin.get("steps", [])},
        "browserNoise": {"h5": h5_noise, "admin": admin_noise},
        "existingPlaywrightSuites": existing,
        "artifacts": artifacts,
        "screenshots": screenshots,
        "risks": risks,
        "notes": summary.get("notes", []),
    }
    normalized["readiness"] = real_local_readiness(summary, risks)
    return normalized


def html_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(header))}</th>" for header in headers)
    body = []
    for row in rows:
        cells = []
        for cell in row:
            if isinstance(cell, str) and (cell.startswith("<span") or cell.startswith("<code")):
                cells.append(f"<td>{cell}</td>")
            else:
                cells.append(f"<td>{html.escape(str(cell))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def image_data_uri(path: Path, max_bytes: int = 1_500_000) -> str | None:
    if not path.exists() or path.stat().st_size > max_bytes:
        return None
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if suffix == "jpg" else suffix
    import base64

    return f"data:image/{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def render_real_local_e2e_report(args: argparse.Namespace) -> None:
    run_path = Path(args.run).resolve()
    summary = read_json(run_path)
    normalized = normalize_real_local_e2e_summary(summary, run_path)
    if args.normalized_json:
        write_json(Path(args.normalized_json).resolve(), normalized)
    title = args.title or "QA Agent 真实本地 E2E 测试报告"
    flow_rows = [
        [
            item.get("id"),
            item.get("priority"),
            item.get("actor"),
            item.get("operationPath"),
            item.get("mapping"),
            badge(item.get("status", "")),
            item.get("evidence"),
        ]
        for item in normalized["businessFlows"]
    ]
    gate_rows = [
        [gate.get("id"), gate.get("category"), gate.get("name"), badge(gate.get("status", "")), gate.get("summary")]
        for gate in normalized["qualityGates"]
    ]
    h5_step_rows = [
        [index + 1, step.get("name"), badge("passed" if step.get("ok") else "failed"), step.get("detail"), step.get("at")]
        for index, step in enumerate(normalized["steps"].get("h5", []))
    ]
    admin_step_rows = [
        [index + 1, step.get("name"), badge("passed" if step.get("ok") else "failed"), step.get("detail"), step.get("at")]
        for index, step in enumerate(normalized["steps"].get("admin", []))
    ]
    db = normalized.get("dbFinal", {})
    db_rows = [[key, value] for key, value in db.items()] or [["N/A", "N/A"]]
    noise_rows = []
    for owner, noise in normalized["browserNoise"].items():
        for category, count in noise.get("counts", {}).items():
            noise_rows.append([owner, category, count, noise.get("blockingErrorCount", 0)])
    artifact_rows = [
        [item.get("name"), item.get("path"), "存在" if item.get("exists") else "缺失"]
        for item in normalized["artifacts"]
    ]
    risk_rows = [[risk.get("id"), risk.get("level"), risk.get("summary")] for risk in normalized["risks"]]
    env_rows = [[key, value] for key, value in normalized.get("environment", {}).items()] or [["N/A", "N/A"]]
    screenshot_html = ""
    if args.embed_screenshots:
        figures = []
        for path_text in normalized.get("screenshots", []):
            path = Path(path_text)
            uri = image_data_uri(path)
            if uri:
                figures.append(f"<figure><img src=\"{uri}\" alt=\"{html.escape(path.name)}\"><figcaption><code>{html.escape(str(path))}</code></figcaption></figure>")
        if figures:
            screenshot_html = "<section id=\"screenshots\"><h3>截图证据</h3><div class=\"screens\">" + "".join(figures) + "</div></section>"
    readiness = normalized["readiness"]
    body = f"""
      <div class="hero" id="summary">
        <h2>{html.escape(title)}</h2>
        <p>生成时间：{html.escape(utc_now())}</p>
        <p>Run：<code>{html.escape(str(run_path))}</code></p>
      </div>
      <div class="grid cards">
        <div class="card"><div class="label">最终状态</div><div class="value">{badge(normalized.get("status", ""))}</div></div>
        <div class="card"><div class="label">Readiness</div><div class="value">{html.escape(readiness.get("decision", "N/A"))}</div></div>
        <div class="card"><div class="label">业务流</div><div class="value">{len(normalized["businessFlows"])}</div></div>
        <div class="card"><div class="label">质量门</div><div class="value">{len(normalized["qualityGates"])}</div></div>
      </div>
      <section id="readiness">
        <h3>最终决策</h3>
        <p>{badge(readiness.get("status", ""))} {html.escape(readiness.get("decision", "N/A"))}：{html.escape(readiness.get("reason", ""))}</p>
      </section>
      <section id="environment"><h3>本地环境</h3>{html_table(["Key", "Value"], env_rows)}</section>
      <section id="business-flows"><h3>业务验收流</h3>{html_table(["ID", "优先级", "业务参与者", "业务操作路径", "执行映射", "结果", "证据"], flow_rows)}</section>
      <section id="quality-gates"><h3>质量门</h3>{html_table(["ID", "类别", "名称", "结果", "摘要"], gate_rows)}</section>
      <section id="steps"><h3>H5 步骤</h3>{html_table(["序号", "步骤", "结果", "详情", "时间"], h5_step_rows) if h5_step_rows else "<p>N/A</p>"}<h3>Admin 步骤</h3>{html_table(["序号", "步骤", "结果", "详情", "时间"], admin_step_rows) if admin_step_rows else "<p>N/A</p>"}</section>
      <section id="db"><h3>数据库终态</h3>{html_table(["字段", "值"], db_rows)}</section>
      <section id="noise"><h3>Console/Request 噪声分类</h3>{html_table(["来源", "类别", "数量", "阻断错误数"], noise_rows) if noise_rows else "<p>N/A</p>"}</section>
      {screenshot_html}
      <section id="artifacts"><h3>原始产物</h3>{html_table(["文件", "路径", "状态"], artifact_rows) if artifact_rows else "<p>N/A</p>"}</section>
      <section id="risks"><h3>残余风险</h3>{html_table(["ID", "级别", "说明"], risk_rows)}</section>
      <section id="raw"><h3>规范化摘要</h3><pre>{html.escape(json.dumps(normalized, ensure_ascii=False, indent=2)[:24000])}</pre></section>
    """
    template = (ASSETS / "report-template.html").read_text(encoding="utf-8")
    styles = """
    <style>
      .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:12px; }
      .screens { display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:14px; }
      figure { margin:0; background:white; border:1px solid var(--line); border-radius:12px; overflow:hidden; }
      figure img { display:block; width:100%; }
      figcaption { padding:10px; color:var(--muted); }
    </style>
    """
    html_doc = template.replace("</head>", styles + "\n</head>").replace("{{TITLE}}", html.escape(title)).replace("{{BODY}}", body)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_doc, encoding="utf-8")
    enforce_no_mojibake(output, allow_mojibake=getattr(args, "allow_mojibake", False))
    print(f"已写入 real-local E2E 报告：{output}")


def _partition_cases(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split cases into (in-scope-this-round, historical-legacy).

    A case is "in-scope" (has been executed or is ready to execute) if:
    - It has expected/operationPath fields (new schema), OR
    - It has a terminal status (passed/failed/blocked) with execution evidence (old schema, executed).
    Legacy = lacks all of the above (truly unused historical artifact).
    """
    EXECUTED_STATUSES = {"passed", "failed", "blocked", "cancelled", "skipped"}
    in_scope: list[dict] = []
    legacy: list[dict] = []
    for c in cases:
        has_new_schema = bool(c.get("expected")) or bool(c.get("operationPath"))
        has_status = str(c.get("status", "")).lower() in EXECUTED_STATUSES
        has_result = bool((c.get("result") or {}).get("outcome"))
        if has_new_schema or has_status or has_result:
            in_scope.append(c)
        else:
            legacy.append(c)
    return in_scope, legacy

# === 项目增强函数 (from project) ===
def _pri_badge(pri: str) -> str:
    p = str(pri or "").upper()
    cls = {"P0": "p0", "P1": "p1", "P2": "p2", "P3": "p3"}.get(p, "mute")
    return f'<span class="b {cls}">{html.escape(p)}</span>'

# === 项目增强函数 (from project) ===
# === 项目增强函数 (from project) ===
def _b(text: str, cls: str = "") -> str:
    return f'<span class="b {cls}">{html.escape(str(text))}</span>'

# === 项目增强函数 (from project) ===
def _compute_hash_of_paths(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        if p.exists():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


def _file_sha256(path: Path) -> str:
    """文件内容 SHA-256 摘要（前 16 字符），供报告签名自包含。

    即使 current/ 路径在提交后失效，内容摘要仍能证明报告渲染时依据的数据快照。
    """
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ""

# === 项目增强函数 (from project) ===
def _detect_skill_source() -> tuple[Path | None, str]:
    """检测当前 qa_agent.py 是从哪种安装运行的。

    Returns:
        (skill_root, mode): skill_root 是 skill 目录的绝对路径，mode 为 'user' | 'project' | 'unknown'
    """
    script_path = Path(__file__).resolve()
    home = Path.home().resolve()
    try:
        # 用户级：~/.claude/skills/quality-assurance-agent/scripts/qa_agent.py
        if str(script_path).startswith(str(home)):
            return script_path.parent.parent, "user"
    except Exception:
        pass
    # 否则视为项目级（已在项目 .claude/skills/ 内）
    return script_path.parent.parent, "project"

# === 项目增强函数 (from project) ===
def _find_run_for_case(run_data: dict, case_id: str) -> dict | None:
    for c in run_data.get("cases", []) or []:
        if c.get("caseId") == case_id:
            return c
    return None


def _case_execution_passed(case: dict, run_data: dict) -> bool:
    """判断一条用例的执行结果是否通过。

    真相源优先级（执行结果优先，生命周期状态只作兜底）：
      1. latest-run.finalOutcome（执行结果真相源，PASS/FAIL）
      2. case.result.outcome（执行结果）
      3. case.status 里的 passed/failed/blocked（兜底；confirmed 是生命周期状态，不算执行结果）
    case.status 恒为 confirmed（生命周期）时，不能当作执行结果；执行真相在 finalOutcome。
    """
    run = _find_run_for_case(run_data, case.get("id", ""))
    if run and run.get("finalOutcome"):
        return str(run["finalOutcome"]).upper() == "PASS"
    outcome = (case.get("result") or {}).get("outcome")
    if outcome:
        return str(outcome).lower() == "passed"
    status = str(case.get("status", "") or "").lower()
    if status == "passed":
        return True
    if status in {"failed", "blocked", "skipped"}:
        return False
    return False

# === 项目增强函数 (from project) ===
def _outcome_badge(outcome: str) -> str:
    o = str(outcome or "").upper()
    cls = {"PASS": "passed", "FAIL": "failed", "BLOCKED": "blocked"}.get(o, "mute")
    return f'<span class="b {cls}">{html.escape(o)}</span>'


_REPORT_CSS = """
    :root {
      --ink:#0f172a; --ink-soft:#334155; --muted:#64748b;
      --line:#e2e8f0; --line-strong:#cbd5e1;
      --bg:#f8fafc; --panel:#fff;
      --brand:#087f75; --brand-soft:#e6f5f3; --brand-deep:#045c55;
      --ok:#087443; --ok-soft:#d1fadf;
      --warn:#b7791f; --warn-soft:#fef0c7;
      --danger:#b42318; --danger-soft:#fee4e2;
      --info:#175cd3; --info-soft:#dbeafe;
      --grey-soft:#eef2f6;
    }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; }
    body {
      color:var(--ink); background:var(--bg);
      font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
    }
    code, pre, .mono { font-family:"SF Mono",Menlo,Consolas,"Roboto Mono",monospace; }
    .wrap { max-width:1160px; margin:0 auto; padding:40px 32px 80px; }

    header.top { border-bottom:1px solid var(--line); padding-bottom:20px; margin-bottom:28px; }
    header.top .kicker { color:var(--brand); font-size:12px; font-weight:700; letter-spacing:.16em; text-transform:uppercase; margin-bottom:6px; }
    header.top h1 { margin:0 0 6px; font-size:26px; line-height:1.25; letter-spacing:-.01em; }
    header.top .meta { color:var(--muted); font-size:13px; }
    header.top .meta span + span::before { content:" · "; margin:0 6px; color:var(--line-strong); }

    section.block { margin:36px 0; }
    section.block > h2 {
      font-size:12px; font-weight:700; letter-spacing:.18em; text-transform:uppercase;
      color:var(--brand); margin:0 0 14px; padding-left:10px; border-left:3px solid var(--brand);
    }

    .verdict {
      background:linear-gradient(135deg,#fef0c7 0%,#fef7e0 100%);
      border:1px solid #f4c86d; border-radius:20px; padding:34px 40px;
      display:flex; align-items:center; gap:32px;
      box-shadow:0 10px 30px rgba(183,121,31,.10);
    }
    .verdict .badge-huge { font-size:34px; font-weight:800; letter-spacing:-.02em; color:#78530f; white-space:nowrap; }
    .verdict .stats { display:flex; gap:28px; flex:1; flex-wrap:wrap; padding-left:32px; border-left:1px solid rgba(183,121,31,.25); }
    .verdict .stat .n { font-size:22px; font-weight:700; color:#78530f; }
    .verdict .stat .l { font-size:12px; color:#8a6412; text-transform:uppercase; letter-spacing:.1em; }
    .verdict.ready { background:linear-gradient(135deg,#d1fadf 0%,#e8fbef 100%); border-color:#6ce9a6; }
    .verdict.ready .badge-huge, .verdict.ready .stat .n { color:#054f31; }
    .verdict.notready { background:linear-gradient(135deg,#fee4e2 0%,#ffefee 100%); border-color:#f97066; }
    .verdict.notready .badge-huge, .verdict.notready .stat .n { color:#7a271a; }
    .verdict a.stat { text-decoration:none; padding:8px 14px 8px 0; border-radius:10px; transition:background .15s,transform .15s; display:block; color:#78530f; }
    .verdict a.stat:hover { background:rgba(120,83,15,.08); transform:translateY(-1px); }
    .verdict.ready a.stat { color:#054f31; }
    .verdict.ready a.stat:hover { background:rgba(5,79,49,.08); }
    .verdict.notready a.stat { color:#7a271a; }
    .verdict.notready a.stat:hover { background:rgba(122,39,26,.08); }
    .verdict a.stat .n, .verdict a.stat .l { color:inherit; }

    .panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:22px 24px; margin-top:12px; }
    .panel + .panel { margin-top:14px; }
    .panel h3 { margin:0 0 14px; font-size:15px; font-weight:700; color:var(--ink); }
    .panel h4 { margin:18px 0 8px; font-size:13px; font-weight:700; color:var(--ink-soft); }
    .panel p { margin:0 0 8px; color:var(--ink-soft); }
    .panel .kv { display:grid; grid-template-columns:100px 1fr; gap:6px 16px; color:var(--ink-soft); font-size:13px; }
    .panel .kv dt { color:var(--muted); }

    table { width:100%; border-collapse:collapse; }
    th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; font-size:13px; }
    th { color:var(--muted); font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:.08em; }
    tr:last-child td { border-bottom:none; }
    .compact th, .compact td { padding:6px 10px; font-size:12.5px; }

    .b { display:inline-flex; align-items:center; gap:5px; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:700; letter-spacing:.04em; white-space:nowrap; }
    .b.passed { color:var(--ok); background:var(--ok-soft); }
    .b.failed { color:var(--danger); background:var(--danger-soft); }
    .b.blocked { color:var(--warn); background:var(--warn-soft); }
    .b.warn { color:var(--warn); background:var(--warn-soft); }
    .b.info { color:var(--info); background:var(--info-soft); }
    .b.mute { color:var(--muted); background:var(--grey-soft); }
    .b.p0 { color:var(--danger); background:var(--danger-soft); }
    .b.p1 { color:var(--warn); background:var(--warn-soft); }
    .b.p2 { color:var(--info); background:var(--info-soft); }
    .b.p3 { color:var(--muted); background:var(--grey-soft); }

    ul.assertions { list-style:none; padding:0; margin:6px 0; }
    ul.assertions li { padding:6px 0 6px 26px; position:relative; color:var(--ink-soft); font-size:13px; }
    ul.assertions li::before { position:absolute; left:0; top:6px; font-weight:700; }
    ul.assertions li.ok::before { content:"\\2713"; color:var(--ok); }
    ul.assertions li.no::before { content:"\\2717"; color:var(--danger); }
    ul.assertions li.blk::before { content:"\\2298"; color:var(--muted); }
    .assert-two-col { columns:2 320px; column-gap:32px; }
    .assert-two-col ul { break-inside:avoid; }

    .conditions { background:#fffbea; border-left:3px solid var(--warn); padding:14px 18px; border-radius:0 10px 10px 0; margin-top:14px; }
    .conditions ol { margin:6px 0 0; padding-left:22px; color:var(--ink-soft); }
    .conditions li { padding:4px 0; }
    .conditions li strong { color:var(--warn); }

    details { margin:6px 0; }
    details > summary { cursor:pointer; padding:8px 10px; border-radius:8px; transition:background .15s; list-style:none; }
    details > summary::-webkit-details-marker { display:none; }
    details > summary::before { content:"\\25B8"; display:inline-block; width:14px; color:var(--muted); transition:transform .15s; margin-right:4px; }
    details[open] > summary::before { transform:rotate(90deg); }
    details > summary:hover { background:var(--brand-soft); }
    details[open] > summary { background:var(--brand-soft); color:var(--brand-deep); }
    details .details-body { padding:10px 24px 6px; border-left:2px solid var(--brand-soft); margin-left:8px; }

    .case-list-head, .case-row > summary {
      display:grid; grid-template-columns:24px 108px 46px 66px 1fr 74px 90px;
      gap:12px; align-items:center; padding:10px 14px;
    }
    .case-list-head {
      color:var(--muted); font-size:11px; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
      background:var(--grey-soft); border-radius:8px 8px 0 0; border:1px solid var(--line); border-bottom:none;
    }
    .case-list { border:1px solid var(--line); border-radius:0 0 8px 8px; overflow:hidden; background:var(--panel); }
    .case-row { border-bottom:1px solid var(--line); }
    .case-row:last-child { border-bottom:none; }
    .case-row > summary::before { content:"\\25B8"; color:var(--muted); font-size:11px; transition:transform .15s; }
    .case-row[open] > summary::before { transform:rotate(90deg); display:inline-block; }
    .case-row > summary { list-style:none; cursor:pointer; }
    .case-row > summary::-webkit-details-marker { display:none; }
    .case-row > summary:hover { background:var(--brand-soft); }
    .case-row > summary .cid { font-family:"SF Mono",monospace; font-weight:600; font-size:12px; color:var(--ink); }
    .case-row > summary .ctitle { color:var(--ink); font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .case-row[open] > summary .ctitle { white-space:normal; }
    .case-row > summary .clayer { color:var(--muted); font-size:11.5px; }
    .case-row > summary .cruns { color:var(--muted); font-size:11.5px; text-align:right; }
    .case-row > summary .cruns .rerun { color:var(--warn); font-weight:600; }
    .case-row > .details-body { border-left:none; padding:16px 22px 20px 60px; background:var(--brand-soft); }
    .blocker-box { background:rgba(249,115,22,.08); border:1px solid rgba(249,115,22,.25); border-radius:8px; padding:12px 16px; margin-bottom:14px; }
    .blocker-box h4 { margin:0 0 8px; color:#f97316; font-size:14px; }
    .blocker-box .kv { margin-bottom:0; }
    .blocker-box .kv dt { color:#fdba74; }

    .risk-viz { display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-top:8px; }
    .bar-row { display:grid; grid-template-columns:40px 44px 1fr; gap:8px; align-items:center; padding:4px 0; font-size:13px; }
    .bar { height:10px; background:var(--line); border-radius:4px; overflow:hidden; }
    .bar > span { display:block; height:100%; background:var(--brand); border-radius:4px; }
    .bar-row.p0 .bar > span { background:var(--danger); }
    .bar-row.p1 .bar > span { background:var(--warn); }
    .bar-row.p2 .bar > span { background:var(--info); }

    pre.log { background:#0f172a; color:#e2e8f0; padding:12px 14px; border-radius:8px; font-size:12px; line-height:1.5; overflow-x:auto; margin:8px 0; max-height:260px; }
    pre.log .ok { color:#6ee7b7; }
    pre.log .fail { color:#fca5a5; }
    pre.log .dim { color:#94a3b8; }
    .note { color:var(--muted); font-size:12.5px; margin-top:8px; font-style:italic; }
    .rerun { display:inline-flex; align-items:center; gap:4px; color:var(--warn); font-size:11.5px; font-weight:600; }

    @media print {
      body { background:white; }
      .wrap { max-width:100%; padding:20px; }
      details { break-inside:avoid; }
      details > summary::before { display:none; }
      details:not([open]) > .details-body { display:block !important; }
      details > .details-body { border-left:none; padding-left:8px; }
      pre.log { max-height:none; }
    }
    @media (max-width:720px) {
      .verdict { flex-direction:column; align-items:flex-start; gap:18px; padding:24px; }
      .verdict .stats { padding-left:0; border-left:none; }
      .case-row > summary { grid-template-columns:1fr auto; grid-template-rows:auto auto; }
      .risk-viz { grid-template-columns:1fr; }
    }
"""

# === 项目增强函数 (from project) ===
_RUN_LOG_NAME_RE = re.compile(
    r"^run(?:-task-(?P<task_order>\d+))?-(?P<case_id>tc-p\d+-\d+)-(?P<slug>[a-z0-9\-]+?)-(?P<epoch>\d{10,})\.log$",
    re.IGNORECASE,
)
_RUN_LOG_HEADER_RE = re.compile(
    r"^# run-with-env\s+(?P<script>\S+)\s*\n"
    r"# executed:\s+(?P<executed_at>\S+)\s*\n"
    r"# exit_code:\s+(?P<exit_code>-?\d+)\s*$",
    re.MULTILINE,
)


def _parse_run_log(path: Path) -> dict[str, Any] | None:
    """Parse one run-*.log into a structured attempt dict, or None if unmatched."""
    m = _RUN_LOG_NAME_RE.match(path.name)
    if not m:
        return None
    case_id = m.group("case_id").upper()
    task_order = int(m.group("task_order")) if m.group("task_order") else None
    script_stem = f"{'task-' + m.group('task_order') + '-' if task_order else ''}{m.group('case_id')}-{m.group('slug')}"
    epoch = int(m.group("epoch"))

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    header = _RUN_LOG_HEADER_RE.search(text)
    if not header:
        return None
    exit_code = int(header.group("exit_code"))
    executed_at = header.group("executed_at")
    script = header.group("script")

    # STDERR noise detection: any non-empty content after "=== STDERR ==="
    stderr_noise = False
    if "=== STDERR ===" in text:
        stderr_tail = text.split("=== STDERR ===", 1)[1].strip()
        stderr_noise = bool(stderr_tail)

    return {
        "caseId": case_id,
        "taskOrder": task_order,
        "scriptStem": script_stem,
        "script": script,
        "logFile": path.name,
        "epoch": epoch,
        "executedAt": executed_at,
        "exitCode": exit_code,
        "outcome": "PASS" if exit_code == 0 else "FAIL",
        "hasStderrNoise": stderr_noise,
    }


def _parse_run_sidecar(path: Path) -> dict[str, Any] | None:
    """Read one run-*.meta.json sidecar into a structured attempt dict, or None if absent/invalid.

    Sidecar 是聚合唯一事实源，字段由 run-with-env 显式写入，避免脆弱地解析文件名。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    case_id = str(data.get("caseId", "") or "").upper()
    if not case_id:
        return None
    case_ids: list[str] = []
    raw_case_ids = data.get("caseIds")
    if isinstance(raw_case_ids, list):
        for item in raw_case_ids:
            value = str(item or "").strip().upper()
            if value and value not in case_ids:
                case_ids.append(value)
    if case_id not in case_ids:
        case_ids.insert(0, case_id)
    try:
        epoch = int(data.get("runId", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not epoch:
        return None
    log_file = str(data.get("logFile", "") or path.stem + ".log")
    # scriptStem 从 logFile 提取：run-<case_id>-<slug>-<epoch>.log → <case_id>-<slug>
    stem = log_file
    if stem.startswith("run-"):
        stem = stem[len("run-"):]
    if stem.endswith(".log"):
        stem = stem[:-len(".log")]
    stem = re.sub(rf"-{epoch}$", "", stem)
    try:
        exit_code = int(data.get("exitCode", 0) or 0)
    except (TypeError, ValueError):
        exit_code = 0
    return {
        "caseId": case_id,
        "caseIds": case_ids,
        "taskId": str(data.get("taskId", "") or ""),
        "taskOrder": None,
        "scriptStem": stem,
        "script": str(data.get("script", "") or ""),
        "logFile": log_file,
        "epoch": epoch,
        "executedAt": str(data.get("executedAt", "") or ""),
        "exitCode": exit_code,
        "outcome": str(data.get("outcome", "PASS" if exit_code == 0 else "FAIL") or ("PASS" if exit_code == 0 else "FAIL")),
        "hasStderrNoise": False,
    }

# === 项目增强函数 (from project) ===
def _render_appendix(legacy: list[dict], scope_ledger: dict, env_checks: list[dict], meta: dict) -> str:
    # A: Legacy cases (truly un-executed, no status/result/expected/operationPath)
    legacy_rows = ""
    for c in legacy[:20]:
        cid = html.escape(str(c.get("id", "-")))
        pri = str(c.get("priority", ""))
        title = html.escape(str(c.get("title", "-")))
        st = str(c.get("status", ""))
        st_label = st if st else "no schema"
        legacy_rows += f'<tr><td><code>{cid}</code></td><td>{_pri_badge(pri)}</td><td>{title}</td><td>{_status_badge(st_label)}</td></tr>'
    if len(legacy) > 20:
        legacy_rows += f'<tr><td colspan="4" style="text-align:center;color:var(--muted);">…… 还有 {len(legacy)-20} 条</td></tr>'
    legacy_html = f'''
  <details id="appendix-a">
    <summary><strong>附录 A · 历史存量用例（{len(legacy)} 条 · 缺少执行证据）</strong></summary>
    <div class="details-body">
      <p class="note">这批用例缺少 expected / operationPath / status / result 字段，无执行证据链。建议后续补充 schema 后重新纳入回归。</p>
      <table class="compact">
        <thead><tr><th>ID</th><th>优先级</th><th>标题</th><th>状态</th></tr></thead>
        <tbody>{legacy_rows}</tbody>
      </table>
    </div>
  </details>''' if legacy else ""

    # B: Scope change
    scope_html = ""
    if scope_ledger:
        entries = scope_ledger.get("entries", []) or []
        excluded = [e for e in entries if str(e.get("scopeDecision", "")).lower() == "excluded"]
        if excluded:
            rows = ""
            for e in excluded:
                rows += f'<tr><td><code>{html.escape(str(e.get("caseId","-")))}</code></td><td><span class="b mute">{html.escape(str(e.get("scopeDecision","")))}</span></td><td>{html.escape(str(e.get("reason","")))}</td></tr>'
            scope_html = f'''
  <details>
    <summary><strong>附录 B · 本轮取舍说明</strong></summary>
    <div class="details-body">
      <table class="compact">
        <thead><tr><th>ID</th><th>取舍</th><th>原因</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </details>'''

    # C: Env checks
    env_html = ""
    if env_checks:
        passed_n = sum(1 for e in env_checks if e.get("ok"))
        blocked_n = sum(1 for e in env_checks if not e.get("ok") and e.get("required"))
        skipped_n = sum(1 for e in env_checks if not e.get("ok") and not e.get("required"))
        rows = ""
        for e in env_checks:
            eid = html.escape(str(e.get("name","-")))
            name = html.escape(str(e.get("name","-")))
            st = "passed" if e.get("ok") else ("blocked" if e.get("required") else "skipped")
            detail = html.escape(str(e.get("detail",""))[:80])
            rows += f'<tr><td>{eid}</td><td>{name}</td><td>{_status_badge(st)}</td><td>{detail}</td></tr>'
        env_html = f'''
  <details>
    <summary><strong>附录 C · 环境检查（{len(env_checks)} 项 · {passed_n} passed · {blocked_n} blocked · {skipped_n} skipped）</strong></summary>
    <div class="details-body">
      <table class="compact">
        <thead><tr><th>ID</th><th>项目</th><th>状态</th><th>详情</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </details>'''

    # E: Signature
    sig_rows = ""
    for src in meta.get("sourceFiles", []) or []:
        sha = html.escape(str(src.get("sha256", "-")))
        sig_rows += f'<tr><td>{html.escape(str(src.get("label","-")))}</td><td class="mono">{html.escape(str(src.get("path","-")))}</td><td class="mono" style="font-size:11px;">{sha}</td><td>{html.escape(str(src.get("mtime","-")))}</td></tr>'
    sig_html = f'''
  <details>
    <summary><strong>附录 E · 报告可信度签名</strong></summary>
    <div class="details-body">
      <dl class="kv">
        <dt>生成时间</dt><dd>{html.escape(str(meta.get("generatedAt","-")))}</dd>
        <dt>源指纹</dt><dd><code style="font-size:11px;">{html.escape(str(meta.get("fingerprint","-")))}</code></dd>
      </dl>
      <table class="compact">
        <thead><tr><th>来源</th><th>路径</th><th>内容摘要(sha256)</th><th>修改时间(东八区)</th></tr></thead>
        <tbody>{sig_rows}</tbody>
      </table>
    </div>
  </details>''' if sig_rows else ""

    return f"""
<section class="block">
  <h2>附录</h2>
  {legacy_html}
  {scope_html}
  {env_html}
  {sig_html}
</section>
"""

# === 项目增强函数 (from project) ===
def _render_case_matrix(in_scope: list[dict], run_data: dict, spec_tasks: dict) -> str:
    rows_html = "".join(_render_case_row(c, run_data, spec_tasks) for c in in_scope)
    return f"""
<section class="block" id="cases">
  <h2>用例矩阵 · 本轮范围（{len(in_scope)} 条）</h2>
  <p class="note" style="margin-bottom:10px;">全部默认折叠。点击行首箭头 ▸ 展开用例明细，展开后再点执行证据可看具体日志。</p>
  <div class="case-list-head">
    <span></span>
    <span>用例编号</span>
    <span>优先级</span>
    <span>状态</span>
    <span>标题</span>
    <span>层级</span>
    <span style="text-align:right;">执行次数</span>
  </div>
  <div class="case-list">
    {rows_html}
  </div>
</section>
"""

# === 项目增强函数 (from project) ===
def _render_case_row(case: dict, run_data: dict, spec_tasks: dict) -> str:
    cid = html.escape(str(case.get("id", "-")))
    pri = str(case.get("priority", ""))
    status = str(case.get("status", ""))
    title = html.escape(str(case.get("title", "-")))
    layer = html.escape(str(case.get("layer", "-")))
    actor = html.escape(str(case.get("businessActor", "-")))
    op_path = html.escape(str(case.get("operationPath", "-")))
    mapped_risk = case.get("mappedRisk", "")
    expected = case.get("expected", []) or []

    # Look up blocker info from spec-tasks for blocked cases
    blocker_html = ""
    if status == "blocked" and spec_tasks:
        tasks = spec_tasks.get("tasks", []) or []
        for t in tasks:
            if t.get("sourceCaseId") == case.get("id") and t.get("executionStatus") == "blocked":
                blocker = html.escape(str(t.get("blocker", "-")))
                owner = html.escape(str(t.get("owner", "-")))
                next_action = html.escape(str(t.get("nextAction", "-")))
                blocker_html = f'''
      <div class="blocker-box">
        <h4>🚫 阻塞原因</h4>
        <dl class="kv">
          <dt>原因</dt><dd>{blocker}</dd>
          <dt>负责人</dt><dd>{owner}</dd>
          <dt>下一步</dt><dd style="font-size:13px;">{next_action}</dd>
        </dl>
      </div>'''
                break

    # Run info
    run = _find_run_for_case(run_data, case.get("id", ""))
    if run:
        attempts = run.get("totalAttempts", 0)
        has_rerun = run.get("hasRerun", False)
        if has_rerun:
            runs_cell = f'<span class="rerun">⚠ {attempts} 次执行</span>'
        else:
            runs_cell = f'{attempts} 次执行'
    else:
        runs_cell = '—'

    # Assertions
    assert_items = "".join(f'<li class="ok">{html.escape(str(a))}</li>' for a in expected[:6])

    # Evidence block
    evidence_html = ""
    if run:
        att_rows = ""
        for a in run.get("attempts", []):
            n = a.get("n", "?")
            log_file = html.escape(str(a.get("logFile", "-"))[-60:])
            exit_code = a.get("exitCode", "?")
            outcome = a.get("outcome", "?")
            att_rows += f'<tr><td>{n}</td><td class="mono" style="font-size:11px;">{log_file}</td><td>{exit_code}</td><td>{_outcome_badge(outcome)}</td></tr>'
        evidence_html = f'''
      <details>
        <summary>执行证据 · {run.get("totalAttempts",0)} 次执行 · 最终 {_outcome_badge(run.get("finalOutcome",""))}</summary>
        <div class="details-body">
          <table class="compact">
            <thead><tr><th>#</th><th>脚本 / 日志</th><th>退出码</th><th>结果</th></tr></thead>
            <tbody>{att_rows}</tbody>
          </table>
        </div>
      </details>'''
    else:
        evidence_html = '<p class="note" style="margin-top:12px;">无 run 日志（可能通过审查/其它方式验证）</p>'

    body = f'''
    <div class="details-body">
      {blocker_html}
      <dl class="kv">
        <dt>业务参与者</dt><dd>{actor}</dd>
        <dt>操作路径</dt><dd>{op_path}</dd>
        {f'<dt>映射风险</dt><dd><code>{html.escape(str(mapped_risk))}</code></dd>' if mapped_risk else ''}
      </dl>
      {f'<h4>关键预期</h4><ul class="assertions">{assert_items}</ul>' if assert_items else ''}
      {evidence_html}
    </div>'''

    return f'''
  <details class="case-row" id="{cid}">
    <summary>
      <span><code>{cid}</code></span>
      <span>{_pri_badge(pri)}</span>
      <span>{_outcome_badge(run.get("finalOutcome", "")) if run and run.get("finalOutcome") else _status_badge(status)}</span>
      <span class="ctitle">{title}</span>
      <span class="clayer">{layer}</span><span class="cruns">{runs_cell}</span>
    </summary>
    {body}
  </details>'''

def _finding_title(finding: dict) -> str:
    """取 finding 的标题。

    产物契约（references/code-review.md）里这个字段叫 title；本函数历史上读的是
    summary，导致按契约产出的 code-review.json 渲染出空标题。按 title → summary
    → message 依次回退，兼容两种写法。
    """
    for key in ("title", "summary", "message"):
        value = finding.get(key)
        if value:
            return str(value)
    return ""


# === 项目增强函数 (from project) ===
def _render_code_review(cr_data: dict, review_scope: list[str] | None = None) -> str:
    findings = cr_data.get("findings", []) or []
    if not findings and not review_scope:
        return ""
    cr_summary = cr_data.get("summary")
    if isinstance(cr_summary, dict):
        blocking = cr_summary.get("blocking", 0)
    else:
        # 契约里 summary 是一句话结论（字符串），不是 {blocking: N} 字典。
        # 传字符串时按其字段结构取 blocking 会抛 AttributeError 中断整份报告渲染，
        # 这里退化成从 findings 的优先级现算。
        blocking = sum(1 for f in findings if str(f.get("severity", "")).upper() in ("P0", "P1"))
    title_note = " · 均非阻塞，不阻塞本次合并" if blocking == 0 else f" · 含 {blocking} 条阻塞"
    # Summary table
    rows = ""
    for f in findings:
        fid = html.escape(str(f.get("id", "-")))
        sev = str(f.get("severity", "")).upper()
        cat = _zh_cat(str(f.get("category", "")))
        summary = html.escape(_finding_title(f))
        rows += f'<tr><td><code>{fid}</code></td><td>{_pri_badge(sev)}</td><td>{cat}</td><td>{summary}</td><td>{_verdict_badge(str(f.get("verdict", "")))}</td></tr>'

    # Expandable details per finding
    details_html = ""
    for f in findings:
        fid = html.escape(str(f.get("id", "-")))
        file = html.escape(str(f.get("file", "-")))
        line = f.get("line")
        location = f"<code>{file}{':' + str(line) if line else ''}</code>"
        sev = str(f.get("severity", "")).upper()
        summary = html.escape(_finding_title(f))
        scenario = html.escape(str(f.get("failureScenario") or f.get("evidence") or ""))
        recommendation = html.escape(str(f.get("recommendation", "")))
        details_html += f'''
    <details>
      <summary>{fid} · {_pri_badge(sev)} · {html.escape(_finding_title(f)[:60])}</summary>
      <div class="details-body">
        <p><strong>位置：</strong>{location}</p>
        <p><strong>问题：</strong>{summary}</p>
        {f'<p><strong>失败场景：</strong>{scenario}</p>' if scenario else ''}
        {f'<p><strong>建议：</strong>{recommendation}</p>' if recommendation else ''}
      </div>
    </details>'''

    scope_block = ""
    if review_scope:
        items = "".join(f'<li><code>{html.escape(str(f))}</code></li>' for f in review_scope)
        scope_block = f'''
  <div class="panel">
    <h3>代码审查范围</h3>
    <details>
      <summary>审查覆盖源码（{len(review_scope)} 个文件）</summary>
      <ul style="margin:6px 0 0;padding-left:20px;font-size:12.5px;color:var(--ink-soft);line-height:1.8;">{items}</ul>
    </details>
  </div>'''

    findings_html = ""
    if findings:
        findings_html = f'''
  <div class="panel" id="code-review">
    <h3>代码审查发现（{len(findings)} 条{title_note}）</h3>
    <table class="compact">
      <thead><tr><th style="width:80px;">ID</th><th style="width:56px;">优先级</th><th style="width:170px;">类型</th><th>摘要</th><th style="width:110px;">处置</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div style="margin-top:12px;">{details_html}</div>
  </div>'''

    return f"""
<section class="block">
  <h2>代码审查</h2>
  {findings_html}
  {scope_block}
</section>
"""

# === 项目增强函数 (from project) ===
def _render_executive_summary(scope: dict, assertions: list[dict], conditions: list[str], cr_notes: str = "", cr_next_steps: list[str] | None = None, gates_html: str = "", test_scripts: list[str] | None = None, product_src: list[str] | None = None, qa_fix_files: list[str] | None = None) -> str:
    cr_next_steps = cr_next_steps or []
    test_scripts = test_scripts or []
    product_src = product_src or []
    qa_fix_files = qa_fix_files or []
    scope_kv = ""
    for k, v in [
        ("模块", scope.get("module", "-")),
        ("改动", scope.get("changes", "-")),
        ("本轮用例", scope.get("inScopeCases", "-")),
        ("历史存量", scope.get("legacy", "-")),
    ]:
        scope_kv += f'<dt>{html.escape(k)}</dt><dd>{v}</dd>'

    cond_html = ""
    if conditions:
        items = "".join(f'<li>{c}</li>' for c in conditions)
        cond_html = f'''
  <div class="conditions" id="conditions">
    <h4 style="margin:0 0 4px;color:var(--warn);">条件项 · 不阻塞本次合并，需下轮跟进</h4>
    <ol>{items}</ol>
  </div>'''

    summary_block = ""
    if cr_notes or cr_next_steps:
        notes_html = f'<p style="margin:0 0 10px;">{html.escape(str(cr_notes))}</p>' if cr_notes else ""
        steps_html = ""
        if cr_next_steps:
            items = "".join(f'<li>{html.escape(str(s))}</li>' for s in cr_next_steps)
            steps_html = f'<h4 style="margin:12px 0 6px;">下一步建议</h4><ul style="padding-left:20px;color:var(--ink-soft);margin:0;">{items}</ul>'
        summary_block = f'''
  <div class="panel">
    <h3>总结与下一步建议</h3>
    {notes_html}
    {steps_html}
  </div>'''

    delivery_block = ""
    if test_scripts or product_src or qa_fix_files:
        scripts_html = ""
        if test_scripts:
            items = "".join(f'<li><code>{html.escape(str(s))}</code></li>' for s in test_scripts)
            scripts_html = f'''
    <details>
      <summary>新增/修改测试脚本（{len(test_scripts)} 个）</summary>
      <ul style="margin:6px 0 0;padding-left:20px;font-size:12.5px;color:var(--ink-soft);line-height:1.8;">{items}</ul>
    </details>'''
        src_html = ""
        if product_src:
            items = "".join(f'<li><code>{html.escape(str(f))}</code></li>' for f in product_src)
            src_html = f'''
    <details>
      <summary>涉及产品源码（{len(product_src)} 个）</summary>
      <ul style="margin:6px 0 0;padding-left:20px;font-size:12.5px;color:var(--ink-soft);line-height:1.8;">{items}</ul>
    </details>'''
        qa_fix_html = ""
        if qa_fix_files:
            items = "".join(f'<li><code>{html.escape(str(f))}</code></li>' for f in qa_fix_files)
            qa_fix_html = f'''
    <details>
      <summary>QA 修复的代码（{len(qa_fix_files)} 个）</summary>
      <ul style="margin:6px 0 0;padding-left:20px;font-size:12.5px;color:var(--ink-soft);line-height:1.8;">{items}</ul>
    </details>'''
        delivery_block = f'''
  <div class="panel">
    <h3>本次交付</h3>
    {scripts_html}
    {src_html}
    {qa_fix_html}
  </div>'''

    return f"""
<section class="block">
  <h2>执行摘要</h2>
  {gates_html}
  {summary_block}
  <div class="panel">
    <h3>本轮范围</h3>
    <dl class="kv">{scope_kv}</dl>
  </div>
  {delivery_block}{cond_html}
</section>
"""

# === 项目增强函数 (from project) ===
def _render_gates(readiness: dict, completion: dict, cr_data: dict) -> str:
    completion_status = str(completion.get("status", "missing"))
    completion_decision = str(completion.get("decision", ""))
    cr_status = str(readiness.get("summary", {}).get("codeReviewStatus", "missing"))

    def _status_b(s: str) -> str:
        return _status_badge(s if s else "missing")

    # Explanation for completion failed
    findings = completion.get("findings", []) or []
    summary = completion.get("summary", {}) or {}
    has_real_failure = summary.get("failed", 0) > 0
    # Detect legacy/schema/blocked-only failures: no real test failures, only schema + blocked
    is_legacy_only_failure = (
        completion_status == "failed"
        and not has_real_failure
        and findings
        and all(f.get("type") in {"invalid-case-file", "task-blocked"} for f in findings)
    )
    display_status = "warn" if is_legacy_only_failure else completion_status
    explain = ""
    if is_legacy_only_failure:
        blocked = summary.get("blocked", 0)
        schema_count = sum(1 for f in findings if f.get("type") == "invalid-case-file")
        cases_affected = len(set(
            f.get("message", "").split()[0]
            for f in findings if f.get("type") == "invalid-case-file"
        ))
        explain = f'<td>* 由历史存量数据不完整触发（{cases_affected} 条老用例缺字段，{schema_count} 条 schema 警告，{blocked} 条 blocked），<strong>不代表本轮验收失败</strong>。0 条真实测试失败。</td>'
    elif completion_status == "failed":
        explain = f'<td>completion-check 失败：{summary.get("failed", 0)} 条失败，{summary.get("blocked", 0)} 条阻塞</td>'
    else:
        cases = summary.get("casesChecked", 0)
        implemented = summary.get("implemented", 0)
        executed = summary.get("executed", 0)
        failed = summary.get("failed", 0)
        blocked = summary.get("blocked", 0)
        explain = f'<td>{cases} 条用例 · 实现 {implemented} · 执行 {executed} · 失败 {failed} · 阻塞 {blocked}</td>'

    rows = f"""
      <tr>
        <td><strong>验收完备度</strong><br><span class="mono" style="color:var(--muted);font-size:11px;">completion</span></td>
        <td>{_status_b(display_status)}</td>
        {explain}
      </tr>
      <tr>
        <td><strong>代码审查</strong><br><span class="mono" style="color:var(--muted);font-size:11px;">code-review</span></td>
        <td>{_status_b(cr_status)}</td>
        <td>{len(cr_data.get("findings", []))} 条 findings 已登记</td>
      </tr>
    """

    return f"""
  <div class="panel">
    <h3>验收门禁 · 验收完备度 / 代码审查</h3>
    <table>
      <thead><tr><th>门禁</th><th>状态</th><th>说明</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
"""

# === 项目增强函数 (from project) ===
def _render_header_and_verdict(title: str, meta: dict, readiness: dict, summary_stats: dict) -> str:
    branch = html.escape(str(meta.get("branch", "unknown")))
    generated_at = html.escape(str(meta.get("generatedAt") or ""))
    fingerprint = html.escape(str(meta.get("fingerprint", ""))[:10])
    vclass, big_label, subtitle = _verdict_class(readiness)
    total_cases = summary_stats.get("totalInScope", 0)
    pass_rate = summary_stats.get("passRate", "-")
    coverage_rate = summary_stats.get("coverageRate", "-")
    executed_n = summary_stats.get("executedCount", 0)
    conditions_n = summary_stats.get("conditionsCount", 0)
    cr_n = summary_stats.get("codeReviewCount", 0)

    return f"""
<header class="top">
  <div class="kicker">QA Agent 验收报告</div>
  <h1>{html.escape(title)}</h1>
  <div class="meta">
    <span>分支 <code>{branch}</code></span>
    <span>生成于 {generated_at}</span>
    <span>签名 <code>{fingerprint}…</code></span>
  </div>
</header>

<div class="verdict {vclass}">
  <div class="badge-huge">{html.escape(big_label)}</div>
  <div class="stats">
    <a class="stat" href="#cases"><div class="n">{total_cases}</div><div class="l">本轮用例</div></a>
    <a class="stat" href="#cases"><div class="n">{pass_rate}</div><div class="l">通过率（{executed_n} 条已执行）</div></a>
    <a class="stat" href="#cases"><div class="n">{coverage_rate}</div><div class="l">验证覆盖（{executed_n}/{total_cases}）</div></a>
    <a class="stat" href="#conditions"><div class="n">{conditions_n}</div><div class="l">条件项</div></a>
    <a class="stat" href="#code-review"><div class="n">{cr_n}</div><div class="l">代码审查发现</div></a>
  </div>
</div>
"""

def _render_evidence_gap(cases: list[dict]) -> str:
    gap_cases = [
        c for c in cases
        if (c.get("result", {}) or {}).get("classification") == "execution-evidence-missing"
    ]
    if not gap_cases:
        return ""
    ids = ", ".join(html.escape(str(c.get("id", ""))) for c in gap_cases[:20])
    more = f" 等共 {len(gap_cases)} 条" if len(gap_cases) > 20 else ""
    return f"""
<div class="callout" style="border:1px solid var(--warn);background:var(--warn-soft);border-radius:8px;padding:12px 16px;margin:16px 0;">
  <strong>待核实：{len(gap_cases)} 个用例缺少有效执行证据</strong>
  <div style="color:var(--muted);font-size:13px;margin-top:4px;">
    这些用例的状态未被自动变更（不会被判定为 blocked），需要人工核实证据链后再确认最终状态：{ids}{more}
  </div>
</div>
"""


def _render_unverified_cases(cases: list[dict], spec_tasks: dict) -> str:
    """blocked 用例独立呈现：不被「通过率」掩盖，让读者一眼看到「N 个用例未验证」。"""
    unverified = [c for c in cases if str(c.get("status", "") or "").lower() == "blocked"]
    if not unverified:
        return ""
    items = ""
    for c in unverified:
        cid = html.escape(str(c.get("id", "-")))
        title = html.escape(str(c.get("title", "-")))
        pri = html.escape(str(c.get("priority", "")))
        blocker = ""
        if spec_tasks:
            for t in spec_tasks.get("tasks", []) or []:
                if t.get("sourceCaseId") == c.get("id") and t.get("executionStatus") == "blocked":
                    blocker = html.escape(str(t.get("blocker", "-")))
                    break
        items += f'<li><code>{cid}</code> <span style="color:var(--muted);">{_pri_badge(pri)}</span> {title}{" — " + blocker if blocker else ""}</li>'
    return f"""
<div class="callout" style="border:1px solid var(--danger);background:var(--danger-soft);border-radius:8px;padding:14px 18px;margin:16px 0;">
  <strong style="color:var(--danger);">⚠️ {len(unverified)} 个用例未验证（blocked），不计入通过率</strong>
  <div style="color:var(--ink-soft);font-size:13px;margin-top:6px;">这些用例的业务行为未被真实执行验证，属于「流程走完但业务没验证完」：</div>
  <ul style="margin:8px 0 0;padding-left:20px;color:var(--ink-soft);font-size:13px;">{items}</ul>
</div>
"""


# === 项目增强函数 (from project) ===
_RISK_ID_RE = re.compile(r"^RISK-[A-Za-z0-9]+-\d+$")
_RISK_ID_TOKEN_RE = re.compile(r"RISK-[A-Za-z0-9]+-\d+")
# 原始证据/日志里出现风险号不构成「关联声明」，不参与完整性比对
_RISK_SCAN_EXCLUDED_KEYS = frozenset({"result"})


def _case_risk_ids(case: dict[str, Any]) -> list[str]:
    """取出用例显式关联的风险 id。

    主源是 `riskIds`（显式字段，见 references/test-case-schema.md）。另兼容历史数据：
    早期用例把风险 id 写在 `traceability` 里——但该字段按 schema 还允许 requirement id /
    代码路径 / API 路径，所以**只认符合风险 id 形态的条目**，其余一律不当作风险关联。

    历史缺陷：这里曾把 traceability 的每一项都当风险号。使用者照 schema 写 API 路径，
    投影结果为空，报告便输出「识别 N 条，已覆盖 0 条」——与事实完全相反，且长得很像真缺口。

    注意别混淆：test-spec-tasks.json 也有个 `traceability`，那个的语义确实是
    「关联的 risk ID 列表」（见 qa-test-script-generator/SKILL.md），两者不同名同义。
    """
    ids: list[str] = []
    explicit = case.get("riskIds")
    if isinstance(explicit, list):
        for item in explicit:
            value = str(item or "").strip()
            if value:
                _append_unique(ids, value)
    trace = case.get("traceability")
    if isinstance(trace, list):
        for item in trace:
            value = str(item or "").strip()
            if value and _RISK_ID_RE.match(value):
                _append_unique(ids, value)
    return ids


def project_risk_coverage(cases_data: dict[str, Any]) -> dict[str, list[str]]:
    """从用例的风险关联反推「风险 → 覆盖它的用例」映射。

    这是覆盖关系的唯一投影源（只读计算，不写回任何产物）。关联来源见 `_case_risk_ids`：
    显式 `riskIds` 为主，兼容旧数据里形态正确的 `traceability` 条目。
    """
    cases = cases_data.get("cases", []) if isinstance(cases_data.get("cases"), list) else []
    coverage: dict[str, list[str]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id", "") or "").strip()
        if not case_id:
            continue
        for risk_id in _case_risk_ids(case):
            coverage.setdefault(risk_id, []).append(case_id)
    # 去重，保持首次出现顺序
    return {risk_id: list(dict.fromkeys(ids)) for risk_id, ids in coverage.items()}


def _render_risk(risk_data: dict, coverage_map: dict[str, list[str]] | None = None) -> str:
    if not risk_data:
        return ""
    summary = risk_data.get("summary", {}) or {}
    total = summary.get("total", 0)
    by_pri = summary.get("byPriority", {}) or {}
    by_cat = summary.get("byCategory", {}) or {}
    risks = risk_data.get("risks", []) or []
    # 覆盖缺口也从 traceability 投影：无用例覆盖的风险才是真缺口（不再读 coverageGaps 预判）
    coverage_map = coverage_map or {}
    gaps = [
        {"riskId": r.get("id"), "gap": f"{r.get('priority', '')} 风险未被任何用例覆盖"}
        for r in risks
        if not coverage_map.get(str(r.get("id", "")), [])
    ]

    # Priority bars
    p_rows = ""
    if total > 0:
        for pri in ["P0", "P1", "P2", "P3"]:
            n = by_pri.get(pri, 0)
            if n == 0:
                continue
            pct = int(n * 100 / total)
            cls = pri.lower()
            p_rows += f'<div class="bar-row {cls}"><span>{_pri_badge(pri)}</span><span>{n}</span><span class="bar"><span style="width:{pct}%"></span></span></div>'

    # Category small table
    c_rows = ""
    for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        c_rows += f'<tr><td>{_risk_category_label(cat)}</td><td>{n}</td></tr>'

    # Coverage: 从用例 traceability 投影（覆盖真相唯一来源），不回读 coverageStatus 预判
    coverage_map = coverage_map or {}
    covered = sum(1 for r in risks if coverage_map.get(str(r.get("id", "")), []))
    missing = len(risks) - covered

    # Risk details table
    risk_rows = ""
    for r in risks:
        rid = html.escape(str(r.get("id", "-")))
        pri = str(r.get("priority", ""))
        risk_desc = html.escape(str(r.get("risk", ""))[:120])
        mapped_cases = coverage_map.get(str(r.get("id", "")), [])
        if mapped_cases:
            case_links = " ".join(f"<code>{html.escape(cid)}</code>" for cid in mapped_cases[:3])
            cov_html = f'<span class="b passed">已覆盖</span> → {case_links}'
        else:
            cov_html = f'<span class="b warn">缺口</span>'
        risk_rows += f'<tr><td><code>{rid}</code></td><td>{_pri_badge(pri)}</td><td>{risk_desc}</td><td>{cov_html}</td></tr>'

    # Coverage gaps list
    gaps_html = ""
    if gaps:
        gap_items = ""
        for g in gaps:
            gap_items += f'<li><code>{html.escape(str(g.get("riskId","-")))}</code> — {html.escape(str(g.get("gap","")))}</li>'
        gaps_html = f'''
  <div class="panel">
    <h3>覆盖缺口 · {len(gaps)} 条</h3>
    <ul style="padding-left:20px;color:var(--ink-soft);">{gap_items}</ul>
  </div>'''

    return f"""
<section class="block">
  <h2>风险与覆盖缺口</h2>
  <div class="panel">
    <h3>风险概览 · 识别 {total} 条，已覆盖 {covered} 条，缺口 {missing} 条</h3>
    <div class="risk-viz">
      <div>
        <h4>按优先级</h4>
        {p_rows}
      </div>
      <div>
        <h4>按类别</h4>
        <table class="compact"><tbody>{c_rows}</tbody></table>
      </div>
    </div>
  </div>
  <div class="panel">
    <h3>风险明细 · 覆盖对照</h3>
    <table class="compact">
      <thead><tr><th>风险 ID</th><th>优先级</th><th>业务路径 / 影响</th><th>覆盖状态</th></tr></thead>
      <tbody>{risk_rows}</tbody>
    </table>
  </div>{gaps_html}
</section>
"""

# === 项目增强函数 (from project) ===
def _status_badge(status: str) -> str:
    s = str(status or "").lower()
    cls = {"passed": "passed", "failed": "failed", "blocked": "blocked", "draft": "info", "": "mute"}.get(s, "mute")
    label = s if s else "—"
    return f'<span class="b {cls}">{html.escape(label)}</span>'

# === 项目增强函数 (from project) ===
def _sync_skill_to_project(repo: Path, *, force: bool = False) -> None:
    """若从用户级安装运行，同步 skill 到项目的 .claude/skills/ 目录。

    项目已有同名 skill 时跳过（除非 force=True）。
    """
    skill_root, mode = _detect_skill_source()
    if mode != "user":
        return  # 项目级安装，无需同步

    project_skills_dir = repo / ".claude" / "skills"
    target_dir = project_skills_dir / "quality-assurance-agent"

    if target_dir.exists() and not force:
        print(f"项目已有 skill：{target_dir}，跳过同步。")
        return

    # 收集需要同步的 skill 目录（顶层路由 + 所有子 skill）
    skill_names = [
        "quality-assurance-agent",
        "qa-context-profiler",
        "qa-risk-analyzer",
        "qa-testcase-designer",
        "qa-test-script-generator",
        "qa-test-runner",
        "qa-code-reviewer",
        "qa-report-generator",
    ]

    synced = 0
    for name in skill_names:
        src = skill_root.parent / name  # 子 skill 与 quality-assurance-agent 同级
        if not src.exists():
            # 兼容：子 skill 可能在 quality-assurance-agent 目录内
            src = skill_root / ".." / name
            src = src.resolve()
        if not src.exists():
            continue
        dst = project_skills_dir / name
        if dst.exists() and not force:
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(str(dst))
            shutil.copytree(str(src), str(dst))
            synced += 1
        except Exception as e:
            print(f"同步 {name} 失败：{e}")

    if synced:
        print(f"已从用户级安装同步 {synced} 个 skill 到 {project_skills_dir}")
        print("Claude Code 将自动加载项目级 skill（优先于用户级）。")

# === 项目增强函数 (from project) ===
def _table_html(headers: list[str], rows: list[list[str]], klass: str = "") -> str:
    if not rows:
        return ""
    thead = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = ""
    for row in rows:
        tds = "".join(f"<td>{cell}</td>" for cell in row)
        body += f"<tr>{tds}</tr>"
    return f'<table class="{klass}"><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>'

# === 项目增强函数 (from project) ===
def _verdict_class(readiness: dict[str, Any]) -> tuple[str, str, str]:
    """Return (css_class, big_label, subtitle) based on readiness decision."""
    decision = str(readiness.get("decision", "")).strip().lower().replace(" ", "-")
    if decision == "ready":
        return "ready", "可以合并（就绪）", ""
    if decision == "conditionally-ready":
        return "", "可以合并（有条件就绪）", ""
    if decision in {"not-ready", "notready"}:
        return "notready", "暂不建议合并", "见下方阻塞项"
    return "notready", "验收未完成", "证据不齐或流程未走完"

# === 项目增强函数 (from project) ===
def _zh_cat(en: str) -> str:
    zh = _ZH_CATEGORY.get(en)
    if not zh:
        return html.escape(en)
    return f'{zh} <span style="color:var(--muted);font-size:11px;">({html.escape(en)})</span>'

# === 项目增强函数 (from project) ===
def _zh_verdict(en: str) -> str:
    # 行动语言：让用户一眼看懂「要修 / 要确认」，而非「状态是什么」
    m = {"CONFIRMED": "待修复", "PLAUSIBLE": "待确认", "REJECTED": "已排除"}
    return m.get(en, en)


def _verdict_badge(verdict: str) -> str:
    """verdict 行动标签 + 阻塞性上色：确认缺陷=红(要修)，疑似=黄(要确认)。"""
    v = str(verdict or "").upper()
    label = html.escape(_zh_verdict(v))
    cls = {"CONFIRMED": "danger", "PLAUSIBLE": "warn", "REJECTED": "mute"}.get(v, "info")
    if v not in {"CONFIRMED", "PLAUSIBLE", "REJECTED"}:
        label = html.escape(str(verdict or "?"))
    return f'<span class="b {cls}">{label}</span>'

# === 项目增强函数 (from project) ===
def aggregate_runs(args: argparse.Namespace) -> None:
    """Aggregate .qa-agent/runs/*.log into structured latest-run.json."""
    repo = Path(args.repo).resolve()
    runs_dir = repo / ".qa-agent" / "runs"
    output = Path(args.output).resolve() if args.output else repo / ".qa-agent" / "current" / "latest-run.json"

    if not runs_dir.is_dir():
        raise QaAgentError(f"runs 目录不存在：{runs_dir}")

    log_files = sorted(runs_dir.glob("*.log"))
    parsed: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for lf in log_files:
        # sidecar 优先：聚合唯一事实源；缺失时回退到文件名/头解析
        sidecar_path = lf.with_suffix(".meta.json")
        entry = _parse_run_sidecar(sidecar_path) if sidecar_path.exists() else None
        if entry is None:
            entry = _parse_run_log(lf)
        if entry is None:
            unmatched.append(lf.name)
        else:
            parsed.append(entry)

    # Group by caseId——一次执行可能覆盖多条用例，每条都要算进去，
    # 否则套件里除首条以外的用例都会被判「没有执行记录」。
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in parsed:
        for covered in e.get("caseIds") or [e["caseId"]]:
            grouped.setdefault(covered, []).append(e)

    cases: list[dict[str, Any]] = []
    total_rerun_overhead = 0
    cases_with_rerun = 0
    cases_passed = 0
    cases_failed = 0
    for case_id, attempts in grouped.items():
        attempts.sort(key=lambda a: a["epoch"])
        for i, a in enumerate(attempts, start=1):
            a["n"] = i
        # Trim internal-only fields from attempts for output
        clean_attempts = [
            {
                "n": a["n"],
                "logFile": a["logFile"],
                "executedAt": a["executedAt"],
                "exitCode": a["exitCode"],
                "outcome": a["outcome"],
                "hasStderrNoise": a["hasStderrNoise"],
            }
            for a in attempts
        ]
        final_outcome = attempts[-1]["outcome"]
        has_rerun = len(attempts) > 1
        if has_rerun:
            cases_with_rerun += 1
            total_rerun_overhead += len(attempts) - 1
        if final_outcome == "PASS":
            cases_passed += 1
        else:
            cases_failed += 1
        cases.append({
            "caseId": case_id,
            "taskOrder": attempts[0].get("taskOrder"),
            "scriptStem": attempts[0]["scriptStem"],
            "totalAttempts": len(attempts),
            "finalOutcome": final_outcome,
            "hasRerun": has_rerun,
            "attempts": clean_attempts,
        })

    cases.sort(key=lambda c: (c["taskOrder"] if c["taskOrder"] is not None else 9999, c["caseId"]))

    result = {
        "version": "1.0",
        "generatedAt": dt.datetime.now(_CN_TZ).isoformat(timespec="seconds"),
        "sourceDir": str(runs_dir.relative_to(repo)).replace("\\", "/"),
        "summary": {
            "totalCases": len(cases),
            "totalAttempts": len(parsed),
            "casesPassed": cases_passed,
            "casesFailed": cases_failed,
            "casesBlocked": 0,
            "casesWithRerun": cases_with_rerun,
            "totalRerunOverhead": total_rerun_overhead,
        },
        "cases": cases,
        "unmatchedLogs": unmatched,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, result)
    print(f"已写入执行汇总：{output}（{len(cases)} 用例 / {len(parsed)} 次执行 / 未匹配 {len(unmatched)}）")


_ZH_CATEGORY = {
    "correctness": "正确性",
    "concurrency-design": "并发设计",
    "maintainability": "可维护性",
    "test-coverage": "测试覆盖",
    "security": "安全",
    "performance": "性能",
    "error-handling": "错误处理",
    "api-contract": "接口契约",
    "money-fund-consistency": "资金一致性",
    "concurrent-safety": "并发安全",
    "permission-access-control": "权限访问",
    "state-machine": "状态机",
    "data-consistency": "数据一致",
    "frontend-consistency": "前端一致",
    "dual-entry": "双入口",
    "audit-log": "审计日志",
    "input-validation": "输入校验",
    "race-condition": "竞态条件",
    "error-recovery": "错误恢复",
    "dual-path-coexistence": "双路径共存",
    "ui-resilience": "界面健壮性",
}


def _extract_cr_scope_files(cr_data: dict[str, Any]) -> list[str]:
    """从 code-review 的 scope 字段提取文件列表。

    scope 合法格式为对象 `{"files": [...], "description": "..."}`。
    兼容历史字符串 scope（返回空列表并告警），避免下游 AttributeError。
    """
    scope = cr_data.get("scope")
    if isinstance(scope, dict):
        files = scope.get("files", [])
        return [str(f) for f in files] if isinstance(files, list) else []
    if scope is not None:
        print(f"警告：code-review.json 的 scope 应为对象 {{files:[]}}，当前为 {type(scope).__name__}，已忽略", file=sys.stderr)
    return []


def render_report(args: argparse.Namespace) -> None:
    """Render QA verdict report aligned with mockup-v1 design."""
    cases_data = read_json(Path(args.cases).resolve()) if args.cases else {"cases": []}
    _, cases_data = validate_cases_data(cases_data, mutate=True)
    all_cases = cases_data.get("cases", [])

    run_data = read_json(Path(args.run).resolve()) if getattr(args, "run", None) and Path(args.run).exists() else {}
    cr_data = read_json(Path(args.code_review).resolve()) if getattr(args, "code_review", None) and Path(args.code_review).exists() else {}
    risk_data = read_json(Path(args.risk_analysis).resolve()) if getattr(args, "risk_analysis", None) and Path(args.risk_analysis).exists() else {}
    completion_data = read_json(Path(args.completion_check).resolve()) if getattr(args, "completion_check", None) and Path(args.completion_check).exists() else {}
    readiness_data = read_json(Path(args.readiness_check).resolve()) if getattr(args, "readiness_check", None) and Path(args.readiness_check).exists() else {}
    spec_tasks_data = read_json(Path(args.spec_tasks).resolve()) if getattr(args, "spec_tasks", None) and Path(args.spec_tasks).exists() else {}

    output = Path(args.output).resolve()
    scope_ledger_path = output.parent.parent / "current" / "scope-change-ledger.json"
    scope_ledger = read_json(scope_ledger_path) if scope_ledger_path.exists() else {}
    env_checks_path = output.parent.parent / "current" / "environment-checks.json"
    env_data = read_json(env_checks_path) if env_checks_path.exists() else {}
    env_checks = env_data.get("checks", []) if isinstance(env_data, dict) else []

    # QA 修复的代码（atomic-commit 记录的 qa-fixes.json）
    qa_fixes_path = output.parent.parent / "current" / "qa-fixes.json"
    qa_fixes_data = read_json(qa_fixes_path) if qa_fixes_path.exists() else {}
    qa_fix_files: list[str] = []
    for _f in (qa_fixes_data.get("fixes") or []):
        for _p in (_f.get("paths") or []):
            _p = str(_p)
            if _p and _p not in qa_fix_files:
                qa_fix_files.append(_p)

    # Partition cases
    in_scope, legacy = _partition_cases(all_cases)

    # Summary stats for verdict card
    #
    # 两个口径分开，因为它们回答的是两个不同的问题：
    #   通过率   = 已执行里通过的比例  -> 「跑过的都过了吗」
    #   验证覆盖 = 已执行占范围内的比例 -> 「验了多少」
    # 只给一个数字时，用范围内总数当分母会把「还没跑到」和「跑失败」混在一起——
    # 10/21 这个值既不表示质量也不表示进度，容易被读成 47% 不合格。
    in_scope_total = len(in_scope)
    executed_count = sum(
        1 for c in in_scope if _find_run_for_case(run_data, str(c.get("id", "")))
    )
    in_scope_passed = sum(1 for c in in_scope if _case_execution_passed(c, run_data))
    pass_rate = f"{int(in_scope_passed * 100 / executed_count)}%" if executed_count else "—"
    coverage_rate = f"{int(executed_count * 100 / in_scope_total)}%" if in_scope_total else "—"
    conditions = readiness_data.get("conditions") or readiness_data.get("readinessConstraints") or []
    cr_findings = cr_data.get("findings", []) if cr_data else []

    summary_stats = {
        "totalInScope": in_scope_total,
        "executedCount": executed_count,
        "passRate": pass_rate,
        "coverageRate": coverage_rate,
        "conditionsCount": len(conditions),
        "codeReviewCount": len(cr_findings),
    }

    # Meta
    fingerprint = _compute_hash_of_paths([
        Path(p).resolve() for p in [
            getattr(args, "cases", None),
            getattr(args, "spec_tasks", None),
            getattr(args, "risk_analysis", None),
            getattr(args, "completion_check", None),
            getattr(args, "code_review", None),
            getattr(args, "readiness_check", None),
        ] if p
    ])
    branch = "unknown"
    try:
        import subprocess as _sp
        branch = _sp.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(output.parent.parent.parent), text=True).strip()
    except Exception:
        pass

    source_files = []
    for lbl, pth in [
        ("cases", getattr(args, "cases", None)),
        ("spec-tasks", getattr(args, "spec_tasks", None)),
        ("risk-analysis", getattr(args, "risk_analysis", None)),
        ("completion-check", getattr(args, "completion_check", None)),
        ("code-review", getattr(args, "code_review", None)),
        ("readiness-check", getattr(args, "readiness_check", None)),
        ("run", getattr(args, "run", None)),
    ]:
        if pth and Path(pth).exists():
            p = Path(pth)
            source_files.append({
                "label": lbl,
                "path": str(p),
                "mtime": dt.datetime.fromtimestamp(p.stat().st_mtime, tz=_CN_TZ).isoformat(),
                "sha256": _file_sha256(p),
            })

    meta = {
        "branch": branch,
        "fingerprint": fingerprint,
        "generatedAt": dt.datetime.now(_CN_TZ).isoformat(timespec="seconds"),
        "sourceFiles": source_files,
    }

    # Build assertions from in-scope cases
    assertions_map: dict[str, dict] = {}
    for c in in_scope:
        title = c.get("title", "")
        status = c.get("status", "")
        if not title:
            continue
        key = title[:12]
        if key in assertions_map:
            continue
        cls = "ok" if _case_execution_passed(c, run_data) else ("blk" if status == "blocked" else "no")
        assertions_map[key] = {"title": title[:32], "desc": html.escape(str(c.get("expected", [""])[0] if c.get("expected") else "")[:60]), "cls": cls}
    assertions = list(assertions_map.values())[:8]

    # Scope for executive summary
    module = (in_scope[0].get("module") if in_scope else "-") or "-"
    cr_scope_files = _extract_cr_scope_files(cr_data) if cr_data else []
    # 测试脚本列表（从 spec-tasks 的 targetFile / command 提取）
    test_scripts: list[str] = []
    _seen_scripts: set[str] = set()
    for _t in (spec_tasks_data.get("tasks") or []):
        _tf = str(_t.get("targetFile") or "").strip()
        if _tf and _tf not in _seen_scripts:
            _seen_scripts.add(_tf)
            test_scripts.append(_tf)
        elif not _tf:
            _m = re.search(r'(e2e/[\w/\-]+\.spec\.ts)', str(_t.get("command") or ""))
            if _m and _m.group(1) not in _seen_scripts:
                _seen_scripts.add(_m.group(1))
                test_scripts.append(_m.group(1))
    scope = {
        "module": html.escape(str(module)),
        "changes": f"{len(cr_scope_files)} 个源码文件" if cr_scope_files else "—",
        "inScopeCases": f"{len(in_scope)} 条",
        "legacy": f"{len(legacy)} 条（缺少执行证据，见 <a href='#appendix-a'>附录 A</a>）" if legacy else "0 条",
    }

    # 总结与下一步建议（来自 code-review 的 notes 与 readinessConstraints）
    cr_notes = (cr_data.get("notes") or "") if cr_data else ""
    cr_next_steps = (cr_data.get("readinessConstraints") or []) if cr_data else []

    # Conditions as HTML-escaped strings
    conditions_html = [html.escape(str(c)) for c in conditions]

    title = args.title or cases_data.get("metadata", {}).get("requirement") or "QA 验收报告"

    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>',
        f'<style>{_REPORT_CSS}</style></head><body><div class="wrap">',
        _render_header_and_verdict(title, meta, readiness_data, summary_stats),
        _render_evidence_gap(all_cases),
        _render_unverified_cases(all_cases, spec_tasks_data),
        _render_executive_summary(scope, assertions, conditions_html, cr_notes, cr_next_steps, _render_gates(readiness_data, completion_data, cr_data), test_scripts, cr_scope_files, qa_fix_files),
        _render_code_review(cr_data, cr_scope_files),
        _render_risk(risk_data, project_risk_coverage(cases_data)),
        _render_case_matrix(in_scope, run_data, spec_tasks_data),
        _render_appendix(legacy, scope_ledger, env_checks, meta),
        '<footer style="margin-top:60px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12px;text-align:center;">',
        f'QA Agent · quality-assurance-agent · 报告 hash <code>{html.escape(fingerprint[:10])}…</code></footer>',
        '</div></body></html>',
    ]
    html_doc = "\n".join(parts)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_doc, encoding="utf-8")
    enforce_no_mojibake(output, allow_mojibake=getattr(args, "allow_mojibake", False))
    print(f"已写入 HTML 报告：{output}")

    # 方案 A：当指定 --module 和 --run-type 时，额外输出归档副本
    module = getattr(args, "module", None)
    run_type = getattr(args, "run_type", None)
    if module and run_type:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_name = f"{module}-{run_type}-{ts}.html"
        archive_path = output.parent / archive_name
        archive_path.write_text(html_doc, encoding="utf-8")
        print(f"已归档副本：{archive_path}")


def _check_sc001_pass_rate(latest_run: dict, report_html: str) -> dict | None:
    """SC-001：通过率 vs 执行结果一致性。

    口径：通过率 = 已执行中 finalOutcome=PASS 的占比（分母是**已执行**用例数，
    不是范围内的用例总数）。范围内总数走另一个指标「验证覆盖」，两者分开呈现。

    分母曾是分歧点：渲染端用范围内总数（10/21 = 47%），本检查用已执行数
    （10/10 = 100%），于是每次运行自检都红。现在两边统一为「已执行」，
    覆盖情况由「验证覆盖」卡片承担。
    """
    summary = latest_run.get("summary", {}) or {}
    total = int(summary.get("totalCases", 0) or 0)
    passed = int(summary.get("casesPassed", 0) or 0)
    if total <= 0:
        return None
    expected_rate = round(passed * 100 / total)

    # 标签里带上了「N 条已执行」的括注，所以不能要求 </div> 紧跟「通过率」
    m = re.search(r'<div class="n">(\d+)%</div><div class="l">通过率[^<]*</div>', report_html)
    if not m:
        return {
            "id": "SC-001",
            "severity": "high",
            "category": "data-consistency",
            "check": "通过率 vs 执行结果",
            "summary": "报告未找到「通过率」字段",
            "evidence": {"expected": f"{expected_rate}%（{passed}/{total}）", "actual": "缺失"},
            "fix": {
                "file": "scripts/qa_agent.py",
                "function": "render_report",
                "line": None,
                "root_cause": "报告 verdict 卡片缺少通过率字段",
                "change": "渲染 verdict 卡片时输出通过率（基于 finalOutcome 计算）",
                "verify": f"重渲染后通过率 = {expected_rate}%",
            },
        }
    actual_rate = int(m.group(1))
    if actual_rate != expected_rate:
        return {
            "id": "SC-001",
            "severity": "high",
            "category": "data-consistency",
            "check": "通过率 vs 执行结果",
            "summary": f"报告通过率 {actual_rate}%，但执行结果为 {passed}/{total}（应为 {expected_rate}%）",
            "evidence": {
                "expected": f"通过率 = {expected_rate}%（{passed}/{total} PASS）",
                "actual": f"通过率 = {actual_rate}%",
                "fields": [
                    {"source": "latest-run.json", "path": "summary.casesPassed", "value": str(passed)},
                    {"source": "latest-run.json", "path": "summary.totalCases", "value": str(total)},
                ],
            },
            "fix": {
                "file": "scripts/qa_agent.py",
                "function": "render_report",
                "line": None,
                "root_cause": "通过率计算读错字段：case.status 是用例生命周期状态（恒 confirmed），执行结果在 latest-run.json 的 finalOutcome",
                "change": "通过率应基于 latest-run.json 的 casesPassed/totalCases（或 finalOutcome）计算",
                "verify": f"重渲染后通过率 = {expected_rate}%，且与正文用例 PASS 数一致",
            },
        }
    return None


def _check_sc002_case_status(report_html: str, latest_run: dict) -> dict | None:
    """SC-002：有执行结果的用例，状态列是否真的渲染成了执行结果。

    逐用例核对，而不是数徽章总数。原判据是「存在 confirmed 徽章 且 存在 PASS」，
    但 confirmed 徽章只在「该用例没有 run」时出现——那是合法的（本轮没跑到它）。
    只要一次运行是部分执行，原判据就必然误报。

    更麻烦的是它给出的 fix（一律改用 finalOutcome 渲染）：照做会让未执行用例的
    状态列变空，把「这条没跑过」这个事实藏起来。渲染正确性 与 执行完整性 是两件事，
    这个检查只管前者。
    """
    runs = latest_run.get("cases", []) if isinstance(latest_run, dict) else []
    if not isinstance(runs, list):
        return None

    mismatched: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        case_id = str(run.get("caseId") or "").strip()
        outcome = str(run.get("finalOutcome") or "").upper()
        if not case_id or outcome not in ("PASS", "FAIL"):
            continue
        # 定位该用例那一行（_render_case_row 输出 <details class="case-row" id="<cid>">）
        match = re.search(
            rf'<details class="case-row" id="{re.escape(case_id)}">(.*?)</summary>',
            report_html,
            re.S,
        )
        if not match:
            continue
        # 有 run 就必须展示执行结果徽章；渲染成生命周期状态才是本检查要抓的 bug
        if f">{outcome}<" not in match.group(1):
            mismatched.append({"caseId": case_id, "finalOutcome": outcome})

    if not mismatched:
        return None

    sample = ", ".join(item["caseId"] for item in mismatched[:5])
    more = f" 等 {len(mismatched)} 条" if len(mismatched) > 5 else ""
    return {
        "id": "SC-002", "severity": "high", "category": "data-consistency",
        "check": "用例状态列 vs 执行结果",
        "summary": f"{len(mismatched)} 条已有执行结果的用例，状态列却未展示执行结果：{sample}{more}",
        "evidence": {
            "expected": "该用例有 finalOutcome 时，状态列展示 PASS/FAIL",
            "actual": f"{len(mismatched)} 条渲染成了生命周期状态",
            "fields": [{"source": "latest-run.json", "path": "cases[].finalOutcome", "value": str(len(mismatched))}],
        },
        "fix": {
            "file": "scripts/qa_agent.py", "function": "_render_case_row", "line": None,
            "root_cause": "该用例有 run 却仍走了 _status_badge（生命周期状态）分支",
            "change": "有 finalOutcome 时必须复用 _outcome_badge；没有 run 的用例保留生命周期状态——那种情况下它是唯一可展示的事实",
            "verify": "重渲染后每一条有 run 的用例都显示 PASS/FAIL",
        },
    }


def _check_sc003_coverage(report_html: str, risk_data: dict, coverage_map: dict[str, list[str]]) -> dict | None:
    """SC-003：traceability 投影 vs 报告覆盖展示（跨产物交叉校验）。

    覆盖真相只来自用例 traceability，用投影结果对比报告渲染的「已覆盖」badge 数，
    两者不一致即为覆盖状态失真。不再依赖 risk-analysis 的 coverageStatus 预判。
    """
    covered_badges = len(re.findall(r'class="b passed">已覆盖', report_html))
    risks = risk_data.get("risks", []) or []
    risk_ids = {str(r.get("id", "")) for r in risks if r.get("id")}
    covered_risks = {rid for rid in risk_ids if coverage_map.get(rid, [])}
    covered_expected = len(covered_risks)
    if covered_expected != covered_badges:
        return {
            "id": "SC-003", "severity": "high", "category": "data-consistency",
            "check": "覆盖投影 vs 报告展示",
            "summary": f"用例 traceability 投影覆盖 {covered_expected} 条风险，但报告展示 {covered_badges} 个「已覆盖」",
            "evidence": {
                "expected": f"已覆盖 = {covered_expected}",
                "actual": f"已覆盖 = {covered_badges}",
                "fields": [{"source": "test-cases.json", "path": "cases[].traceability", "value": f"covered_risks={sorted(covered_risks)[:10]}"}],
            },
            "fix": {
                "file": "scripts/qa_agent.py", "function": "_render_risk", "line": None,
                "root_cause": "覆盖状态未从 traceability 投影，而是读失真的 coverageStatus 预判",
                "change": "覆盖判断改为从 test-cases 的 traceability 动态投影 coveredByCases",
                "verify": f"重渲染后「已覆盖」badge = {covered_expected}",
            },
        }
    return None


def _risk_ids_mentioned_in_case(case: dict[str, Any]) -> set[str]:
    """独立于投影，从用例原始字段文本里扫出所有形态正确的风险 id。"""
    probe = {k: v for k, v in case.items() if k not in _RISK_SCAN_EXCLUDED_KEYS}
    try:
        text = json.dumps(probe, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(probe)
    return set(_RISK_ID_TOKEN_RE.findall(text))


def _check_sc007_projection_completeness(cases_data: dict, coverage_map: dict[str, list[str]]) -> dict | None:
    """SC-007：投影完整性（独立于投影的原始扫描）。

    SC-003 校验的是「渲染是否忠实于投影」，而两边同源于 `project_risk_coverage`——
    投影本身误读时两边一起错、数值恒等，SC-003 必然通过。本检查用**独立的文本扫描**
    取用例里实际出现的风险 id，与投影结果比对，专抓「投影漏读」。

    历史缺陷：投影曾把 traceability 的每一项都当风险号，而该字段按 schema 还允许
    代码路径 / API 路径。使用者照 schema 写 API 路径 → 投影全空 → 报告输出
    「识别 7 条，已覆盖 0 条」，与事实相反，且长得像真实缺口，使用者会去补不存在的覆盖。
    """
    cases = cases_data.get("cases", []) if isinstance(cases_data.get("cases"), list) else []
    missed: dict[str, list[str]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id", "") or "").strip() or "(无 id)"
        linked = set(_case_risk_ids(case))
        for risk_id in sorted(_risk_ids_mentioned_in_case(case) - linked):
            missed.setdefault(risk_id, []).append(case_id)
    if not missed:
        return None
    sample = ", ".join(sorted(missed)[:10])
    return {
        "id": "SC-007", "severity": "high", "category": "data-consistency",
        "check": "风险关联投影完整性",
        "summary": f"{len(missed)} 个风险 id 出现在用例里却未被投影关联（{sample}）",
        "evidence": {
            "expected": "用例里出现的风险 id 都应通过 riskIds 显式关联（或写成形态正确的 traceability 条目）",
            "actual": f"未关联：{ {k: v[:3] for k, v in list(missed.items())[:5]} }",
            "fields": [{"source": "test-cases.json", "path": "cases[].riskIds",
                        "value": f"当前仅关联到 {sorted(coverage_map)[:10]}"}],
        },
        "fix": {
            "file": "test-cases.json", "function": None, "line": None,
            "root_cause": "用例提到了风险 id 但没写进 riskIds——覆盖投影读不到，报告会谎报该风险未被覆盖",
            "change": "把该风险 id 补进对应用例的 riskIds；若只是行文提及、并非覆盖声明，删掉该提及",
            "verify": "重跑 self-check，SC-007 消失且报告覆盖数上升",
        },
    }


def _check_sc004_gate_verdict(report_html: str) -> dict | None:
    """SC-004：门禁 vs 结论。"""
    verdict_ready = ("可以合并（就绪）" in report_html) or ("可以合并（有条件就绪）" in report_html)
    gate_not_passed = ('class="b failed">failed' in report_html) or ('class="b mute">completed' in report_html)
    if verdict_ready and gate_not_passed:
        return {
            "id": "SC-004", "severity": "high", "category": "data-consistency",
            "check": "门禁 vs 结论",
            "summary": "verdict 显示「可以合并」，但验收门禁中存在非 passed 状态（failed/completed）",
            "evidence": {"expected": "所有门禁 passed", "actual": "存在 failed/completed 门禁"},
            "fix": {
                "file": "scripts/qa_agent.py", "function": "_render_gates", "line": None,
                "root_cause": "门禁状态读错字段（code-review 读 code-review.json 的 status=completed，应为判定结果 passed）",
                "change": "门禁状态统一读 readiness.summary 的判定结果",
                "verify": "重渲染后所有门禁 passed，与 verdict 一致",
            },
        }
    return None


def _check_sc005_env_stats(report_html: str, env_checks: list) -> dict | None:
    """SC-005：环境检查统计。"""
    m = re.search(r'附录 C · 环境检查（(\d+) 项 · (\d+) passed', report_html)
    ok_count = sum(1 for e in env_checks if e.get("ok"))
    if m and ok_count > 0:
        passed = int(m.group(2))
        if passed == 0:
            return {
                "id": "SC-005", "severity": "high", "category": "data-consistency",
                "check": "环境检查统计",
                "summary": f"报告环境检查 passed=0，但 environment-checks.json 有 {ok_count} 项 ok=true",
                "evidence": {
                    "expected": f"passed = {ok_count}",
                    "actual": "passed = 0",
                    "fields": [{"source": "environment-checks.json", "path": "checks[].ok", "value": f"ok=true 共 {ok_count} 项"}],
                },
                "fix": {
                    "file": "scripts/qa_agent.py", "function": "_render_appendix", "line": None,
                    "root_cause": "环境检查统计读错字段（数据是 ok，渲染读 status）",
                    "change": "环境检查统计读 ok 字段",
                    "verify": f"重渲染后 passed = {ok_count}",
                },
            }
    return None


def _check_sc006_unverified_cases(report_html: str, completion_data: dict) -> dict | None:
    """SC-006：未验证用例不能被「可以合并」掩盖。

    若 completion 有 case-not-verified（用例层未验证），但报告 verdict 显示「可以合并」，
    说明 readiness 漏判了未验证用例，属于严重错误。
    """
    findings = completion_data.get("findings", []) if isinstance(completion_data, dict) else []
    unverified = [f for f in findings if f.get("type") == "case-not-verified"]
    if not unverified:
        return None
    verdict_ready = ("可以合并（就绪）" in report_html) or ("可以合并（有条件就绪）" in report_html)
    if not verdict_ready:
        return None
    case_ids = sorted({str(f.get("sourceCaseId", "")) for f in unverified})
    return {
        "id": "SC-006", "severity": "high", "category": "data-consistency",
        "check": "未验证用例 vs 结论",
        "summary": f"completion 有 {len(unverified)} 个 case-not-verified（{', '.join(case_ids[:5])}），但报告判「可以合并」，未验证用例被通过率掩盖",
        "evidence": {
            "expected": "有未验证用例时 verdict 应为 Not Ready/Incomplete",
            "actual": "可以合并",
            "fields": [{"source": "completion-check.json", "path": "findings[type=case-not-verified]", "value": f"{len(unverified)} 个未验证用例"}],
        },
        "fix": {
            "file": "scripts/qa_agent.py", "function": "assert_readiness_data", "line": None,
            "root_cause": "readiness 未消费 completion 的 case-not-verified，导致未验证用例被判 Ready",
            "change": "readiness 消费 case-not-verified，有未验证用例则判 Incomplete/Not Ready",
            "verify": f"有 {len(unverified)} 个未验证用例时重跑 readiness 应为 Incomplete",
        },
    }


def _check_sc301_artifacts(current: Path) -> list[dict]:
    """SC-301：8 个关键产物齐全性。"""
    stage_map = {
        "context.json": "collect-context",
        "risk-analysis.json": "analyze-risks",
        "test-cases.json": "testcase-designer",
        "test-spec-tasks.json": "generate-spec-tasks",
        "latest-run.json": "aggregate-runs",
        "code-review.json": "code-reviewer",
        "completion-check.json": "assert-completion",
        "readiness-check.json": "assert-readiness",
    }
    findings = []
    for name, stage in stage_map.items():
        p = current / name
        if not p.exists() or p.stat().st_size == 0:
            findings.append({
                "id": "SC-301", "severity": "high", "category": "missing-artifact",
                "check": "产物齐全性",
                "summary": f"关键产物缺失或为空：{name}",
                "evidence": {"expected": f"{name} 存在且非空", "actual": "缺失或为空"},
                "fix": {
                    "file": "qa 流程", "function": stage, "line": None,
                    "root_cause": f"{name} 未生成，{stage} 阶段未跑通或失败",
                    "change": f"补跑 {stage} 生成 {name}",
                    "verify": f"{name} 生成且非空",
                },
            })
    return findings


def _check_sc302_anchors(report_html: str) -> dict | None:
    """SC-302：报告关键锚点。"""
    anchors = {"verdict 卡片": "badge-huge", "用例矩阵": "case-row", "风险明细": "覆盖对照", "代码审查": "代码审查发现"}
    missing = [k for k, v in anchors.items() if v not in report_html]
    if missing:
        return {
            "id": "SC-302", "severity": "medium", "category": "missing-artifact",
            "check": "报告关键锚点",
            "summary": f"报告缺少关键区块：{', '.join(missing)}",
            "evidence": {"expected": "报告含 verdict/用例/风险/代码审查", "actual": f"缺失 {missing}"},
            "fix": {
                "file": "scripts/qa_agent.py", "function": "render_report", "line": None,
                "root_cause": "报告渲染缺少关键 section，渲染逻辑分支未命中",
                "change": f"检查 {missing} 对应的渲染逻辑，确保输出",
                "verify": "重渲染后报告含全部关键锚点",
            },
        }
    return None


_SEV_ZH = {"high": "高", "medium": "中", "low": "低"}


def _resolve_webhook(args: argparse.Namespace, current: Path) -> str:
    """解析 webhook 优先级：命令行 --webhook > config notify.webhook。

    没有内置默认值——两处都未配置时返回空字符串，即不发送任何通知、不发任何网络请求。
    配置里若存在 notify.webhook 字段，无论空字符串还是 URL 都直接生效（空字符串 = 关闭通知）。
    """
    webhook = getattr(args, "webhook", None)
    if webhook:
        return webhook
    config_path = current.parent / "config" / "qa-agent.config.yaml"
    if config_path.exists():
        try:
            cfg = load_config(config_path)
            notify = cfg.get("notify")
            if isinstance(notify, dict) and "webhook" in notify:
                return notify.get("webhook") or ""
        except Exception:
            pass
    return ""


def _post_webhook(webhook_url: str, content: str, title: str = "") -> None:
    """POST markdown 消息到 webhook，失败不抛异常。

    负载同时带上 markdown.content（企业微信）与 markdown.text/title（钉钉）互为兼容字段。
    Slack / 飞书 的机器人负载格式不同，需经转换网关，见 docs/configuration.md。
    """
    payload = json.dumps(
        {
            "msgtype": "markdown",
            "markdown": {"content": content, "text": content, "title": title or "QA 报告质量告警"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        result = json.loads(body) if body else {}
        code = result.get("errcode", result.get("code", 0))
        if code not in (0, None):
            print(f"告警推送失败：{result}", file=sys.stderr)
        else:
            print("告警已推送")
    except Exception as exc:  # noqa: BLE001 - 告警失败不阻断自检
        print(f"告警推送异常：{exc}", file=sys.stderr)


def _send_webhook(webhook_url: str, findings: list[dict], title: str) -> None:
    """把 findings 按 4096 字节分片推送到 webhook（markdown）。"""
    if not webhook_url:
        return
    blocks = []
    for f in findings:
        sev = str(f.get("severity", "medium"))
        sev_zh = _SEV_ZH.get(sev, sev)
        fix = f.get("fix", {}) or {}
        line = f":{fix['line']}" if fix.get("line") else ""
        blocks.append(
            f"> **{f.get('id')} [{sev_zh}] {f.get('check')}**\n"
            f"> 现象：{f.get('summary', '')}\n"
            f"> 根因：{fix.get('root_cause', '')}\n"
            f"> 位置：{fix.get('file', '')}{line} {fix.get('function', '')}\n"
            f"> 改法：{fix.get('change', '')}\n"
            f"> 验证：{fix.get('verify', '')}"
        )
    MAX_BYTES = 4096
    header = f"### <font color=\"warning\">QA 报告质量告警</font>\n**报告**：{title}\n**发现**：{len(findings)} 个问题\n\n"
    avail = MAX_BYTES - len(header.encode("utf-8"))

    chunks: list[str] = []
    cur = ""
    for b in blocks:
        candidate = (cur + "\n\n" + b) if cur else b
        if cur and len(candidate.encode("utf-8")) > avail:
            chunks.append(cur)
            cur = b
        else:
            cur = candidate
    if cur:
        chunks.append(cur)

    for i, c in enumerate(chunks):
        if len(chunks) > 1:
            content = f"### <font color=\"warning\">QA 报告质量告警</font>（{i + 1}/{len(chunks)}）\n**报告**：{title}\n\n" + c
        else:
            content = header + c
        _post_webhook(webhook_url, content, title)


def _check_sc201_202_field_mismatch(qa_source: str) -> list[dict]:
    """SC-201/202：字段错位检测——grep 渲染源码里读错/不存在的字段模式。"""
    rules = [
        {
            "id": "SC-201", "check": "字段语义错位（执行结果）",
            "patterns": [
                r'c\.get\("status"\)\s*==\s*"passed"',
                r'case\.get\("status"\)\s*==\s*"passed"',
            ],
            "root_cause": "读 case.status 判断执行结果，但 status 是用例生命周期状态（confirmed/draft）",
            "change": "改用 finalOutcome / result.outcome 判断执行结果",
        },
        {
            "id": "SC-202", "check": "字段不存在（覆盖/映射）",
            "patterns": [
                r'get\("mappedToExistingCase"\)',
                r'get\("mappedRisk"\)',
                r'get\("mapped_risk"\)',
            ],
            "root_cause": "读不存在的字段 mappedToExistingCase / mappedRisk，数据里实际是 coveredByCases",
            "change": "改用 coveredByCases",
        },
    ]
    findings = []
    for rule in rules:
        lines = []
        for pat in rule["patterns"]:
            for i, line in enumerate(qa_source.splitlines(), start=1):
                if re.search(pat, line):
                    lines.append(i)
        if lines:
            findings.append({
                "id": rule["id"], "severity": "high", "category": "field-mismatch",
                "check": rule["check"],
                "summary": f"源码存在字段错位模式，共 {len(lines)} 处：第 {', '.join(map(str, lines[:8]))} 行",
                "evidence": {"expected": rule["change"], "actual": f"读错字段（行号 {lines[:8]}）"},
                "fix": {
                    "file": "scripts/qa_agent.py", "function": "（见行号）", "line": lines[0] if lines else None,
                    "root_cause": rule["root_cause"],
                    "change": rule["change"],
                    "verify": "改后重新跑自检，该错位不再出现",
                },
            })
    return findings


def qa_self_check(args: argparse.Namespace) -> None:
    """QA 自检（一期）：报告数据自洽性 + 产物齐全性检测。"""
    current = Path(args.current).resolve()
    report_path = Path(args.report).resolve()
    report_html = report_path.read_text(encoding="utf-8") if report_path.exists() else ""

    latest_run = read_json(current / "latest-run.json") if (current / "latest-run.json").exists() else {}
    risk_data = read_json(current / "risk-analysis.json") if (current / "risk-analysis.json").exists() else {}
    cases_data = read_json(current / "test-cases.json") if (current / "test-cases.json").exists() else {}
    coverage_map = project_risk_coverage(cases_data)
    env_data = read_json(current / "environment-checks.json") if (current / "environment-checks.json").exists() else {}
    env_checks = env_data.get("checks", []) if isinstance(env_data, dict) else []
    completion_data = read_json(current / "completion-check.json") if (current / "completion-check.json").exists() else {}

    findings: list[dict] = []
    for f in (
        _check_sc001_pass_rate(latest_run, report_html),
        _check_sc002_case_status(report_html, latest_run),
        _check_sc003_coverage(report_html, risk_data, coverage_map),
        _check_sc007_projection_completeness(cases_data, coverage_map),
        _check_sc004_gate_verdict(report_html),
        _check_sc005_env_stats(report_html, env_checks),
        _check_sc006_unverified_cases(report_html, completion_data),
        _check_sc302_anchors(report_html),
    ):
        if f:
            findings.append(f)
    findings.extend(_check_sc301_artifacts(current))
    findings.extend(_check_sc201_202_field_mismatch(Path(__file__).resolve().read_text(encoding="utf-8")))

    result = {
        "version": "1.0",
        "generatedAt": utc_now(),
        "status": "passed" if not findings else "failed",
        "findings": findings,
    }
    output = Path(args.output).resolve() if args.output else (current / "self-check.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, result)

    if findings:
        high = sum(1 for f in findings if f.get("severity") == "high")
        print(f"QA 自检未通过：{len(findings)} 个问题（{high} 个 high）")
        for f in findings:
            print(f"  [{f['severity']}] {f['id']} {f['check']}: {f['summary']}")
        webhook = _resolve_webhook(args, current)
        if webhook:
            title = ""
            m = re.search(r"<title>(.*?)</title>", report_html, re.DOTALL)
            if m:
                title = html.unescape(m.group(1).strip())
            _send_webhook(webhook, findings, title)
        raise SystemExit(1)
    print("QA 自检通过：无问题")
    print(f"已写入自检结果：{output}")


def render_cases(args: argparse.Namespace) -> None:
    render_args = argparse.Namespace(
        cases=args.cases,
        run=None,
        review=None,
        code_review=None,
        completion_check=None,
        output=args.output,
        title=args.title or "测试用例确认页",
        allow_mojibake=getattr(args, "allow_mojibake", False),
    )
    render_report(render_args)
    cases_data = read_json(Path(args.cases).resolve())
    if not getattr(args, "no_summary", False):
        print_case_summary(cases_data)
    language_warnings = case_language_warnings(cases_data)
    if language_warnings:
        print_language_warnings(language_warnings)


def self_test(_: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="ming-qa-self-test-") as tmp:
        tmp_path = Path(tmp)
        init_layout(argparse.Namespace(repo=str(tmp_path), write_gitignore=True, root_gitignore=False, json=None))
        init_project(
            argparse.Namespace(
                repo=str(tmp_path),
                root_gitignore=False,
                force=False,
                install_playwright_agents=False,
                force_playwright_agents=False,
                install_mysql_mcp=False,
                verify_mysql_mcp=False,
                mysql_mcp_server_name="mysql_mcp",
                loop="codex",
                timeout=30,
                dry_run=True,
                no_yes=False,
                json=None,
            )
        )
        for required_path in [
            tmp_path / ".qa-agent" / "config" / "qa-agent.config.yaml",
            tmp_path / ".qa-agent" / "profiles" / "project-test-profile.json",
            tmp_path / ".qa-agent" / "local" / ".env",
        ]:
            if not required_path.exists():
                raise QaAgentError(f"self-test init-project missing {required_path}")
        cases_path = tmp_path / "test-cases.json"
        plan_path = tmp_path / "test-plan.json"
        spec_tasks_path = tmp_path / "test-spec-tasks.json"
        coverage_path = tmp_path / "coverage-balance.json"
        review_path = tmp_path / "model-review.json"
        config_path = tmp_path / "ming-qa.config.json"
        run_path = tmp_path / "run.json"
        report_path = tmp_path / "report.html"
        risk_path = tmp_path / "risk-analysis.json"
        completion_path = tmp_path / "completion-check.json"
        code_review_path = tmp_path / "code-review.json"
        code_review_check_path = tmp_path / "code-review-check.json"
        readiness_path = tmp_path / "readiness-check.json"
        sample_server = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@executeautomation/database-server", "--mysql", "--host", "127.0.0.1", "--user", "qa", "--password", "secret", "--database", "demo"],
        }
        sanitized = sanitize_mcp_server(sample_server)
        if sanitized["args"][4] != "***" or sanitized["args"][6] != "***" or sanitized["args"][8] != "***":
            raise QaAgentError("self-test MCP sanitization failed")
        rendered = render_mcp_server_toml("mysql_mcp", sample_server)
        if "[mcp_servers.mysql_mcp]" not in rendered or "secret" not in rendered:
            raise QaAgentError("self-test MCP TOML rendering failed")
        if "secret" in redact_mcp_text("password=secret", sample_server):
            raise QaAgentError("self-test MCP text redaction failed")
        mojibake_sample = "\u6d4b\u8bd5\u7528\u4f8b".encode("utf-8").decode("gbk", errors="ignore")
        if detect_mojibake_text("\u6d4b\u8bd5\u7528\u4f8b")["ok"] is False:
            raise QaAgentError("self-test mojibake false positive")
        if detect_mojibake_text(mojibake_sample)["ok"]:
            raise QaAgentError("self-test mojibake detection failed")
        cases = {
            "version": "1.0",
            "metadata": {"project": "self-test", "requirement": "QA Agent 自检"},
            "assumptions": [],
            "openQuestions": [],
            "cases": [
                {
                    "id": "TC-P0-001",
                    "title": "核心流程成功",
                    "priority": "P0",
                    "layer": "e2e",
                    "type": "functional",
                    "module": "self-test",
                    "source": ["self-test"],
                    "preconditions": ["测试数据存在"],
                    "steps": ["执行流程"],
                    "expected": ["流程成功"],
                    "data": [],
                    "automation": "candidate",
                    "status": "confirmed",
                },
                {
                    "id": "TC-P1-001",
                    "title": "接口契约失败返回错误",
                    "priority": "P1",
                    "layer": "api",
                    "type": "exception",
                    "module": "self-test",
                    "source": ["self-test"],
                    "preconditions": [],
                    "steps": ["发送非法请求"],
                    "expected": ["返回业务错误"],
                    "data": [],
                    "automation": "candidate",
                    "status": "confirmed",
                },
            ],
        }
        write_json(cases_path, cases)
        baseline_dir = tmp_path / ".qa-agent"
        baseline_cases_path = baseline_dir / "test-cases-baseline.json"
        baseline_cases = {
            "version": "1.0",
            "metadata": {"project": "self-test", "requirement": "baseline"},
            "cases": [cases["cases"][0]],
        }
        write_json(baseline_cases_path, baseline_cases)
        existing_index = index_existing_cases_data(tmp_path)
        if existing_index.get("summary", {}).get("cases", 0) < 1:
            raise QaAgentError("self-test existing case indexing failed")
        merged_cases = merge_existing_cases_data(cases, existing_index)
        if merged_cases.get("metadata", {}).get("existingCaseReuse", {}).get("reused") != 1:
            raise QaAgentError("self-test existing case reuse failed")
        errors, _ = validate_cases_data(read_json(cases_path), mutate=True)
        if errors:
            raise QaAgentError("self-test validation failed: " + "; ".join(errors))
        promoted_path = tmp_path / ".qa-agent" / "cases" / "self-test.json"
        promote_cases(argparse.Namespace(cases=str(cases_path), repo=str(tmp_path), module="self-test", output=str(promoted_path)))
        if not promoted_path.exists():
            raise QaAgentError("self-test promote cases failed")
        write_json(plan_path, classify_cases(read_json(cases_path)))
        # 假项目 manifest，供 spec-task 生成识别目标项目（无 manifest 时无法生成 task）
        (tmp_path / "pom.xml").write_text(
            "<project><modelVersion>4.0.0</modelVersion><artifactId>self-test</artifactId></project>",
            encoding="utf-8",
        )
        spec_tasks = generate_spec_tasks_data(read_json(cases_path), repo=tmp_path)
        write_json(spec_tasks_path, spec_tasks)
        if len(spec_tasks.get("tasks", [])) < 13:
            raise QaAgentError("self-test spec task generation produced too few tasks")
        if not all(isinstance(task.get("oracle"), dict) and any(task["oracle"].values()) for task in spec_tasks.get("tasks", [])):
            raise QaAgentError("self-test spec task oracle generation failed")
        risk_source = tmp_path / "ReviewService.java"
        risk_source.write_text("class ReviewService { void approveRewardStatus() { /* admin reward status */ } }", encoding="utf-8")
        risk_data = analyze_risks_data(tmp_path, context={"scope": "self-test"}, existing_index={})
        write_json(risk_path, risk_data)
        if risk_data.get("summary", {}).get("total", 0) < 1:
            raise QaAgentError("self-test risk analysis failed to detect risky source")
        coverage = coverage_balance_data(spec_tasks)
        write_json(coverage_path, coverage)
        if coverage.get("summary", {}).get("byLayer", {}).get("unit", 0) <= coverage.get("summary", {}).get("byLayer", {}).get("e2e", 0):
            raise QaAgentError("self-test coverage balance did not keep unit above e2e")
        completion = assert_completion_data(read_json(cases_path), spec_tasks)
        if completion.get("status") != "failed" or completion.get("summary", {}).get("unimplemented", 0) == 0:
            raise QaAgentError("self-test completion gate failed to catch pending tasks")
        for task in spec_tasks.get("tasks", []):
            task["implementationStatus"] = "implemented"
            task["executionStatus"] = "passed"
            task["evidence"] = ["self-test run evidence"]
        completion = assert_completion_data(read_json(cases_path), spec_tasks)
        if completion.get("status") != "passed":
            raise QaAgentError("self-test completion gate failed a completed task set")
        write_json(completion_path, completion)
        write_json(spec_tasks_path, spec_tasks)
        bad_review_check = assert_code_review_data({"status": "passed", "findings": [{"severity": "P1", "status": "open", "message": "blocking"}]})
        if bad_review_check.get("status") != "failed":
            raise QaAgentError("self-test code review gate failed to catch blocking finding")
        good_review = {"status": "passed", "findings": []}
        code_review_check = assert_code_review_data(good_review)
        if code_review_check.get("status") != "passed":
            raise QaAgentError("self-test code review gate rejected clean review")
        write_json(code_review_path, good_review)
        write_json(code_review_check_path, code_review_check)
        write_json(
            review_path,
            synthesize_reviews(
                [
                    {
                        "model": "dry-run",
                        "ok": True,
                        "review": {"findings": [], "summary": "ok"},
                    }
                ]
            ),
        )
        write_json(
            config_path,
            {
                "qualityGates": {"maxRepairLoops": 5},
                "commands": {
                    "unit": [f'"{sys.executable}" -c "import sys; sys.exit(0)"'],
                    "api": [f'"{sys.executable}" -c "import sys; sys.exit(0)"'],
                    "integration": [],
                    "e2e": [f'"{sys.executable}" -c "import sys; sys.exit(0)"'],
                    "review": [],
                },
            },
        )
        loop_args = argparse.Namespace(
            repo=str(tmp_path),
            config=str(config_path),
            gates="unit,api,e2e",
            output=str(run_path),
            timeout=30,
            continue_on_failure=False,
        )
        run_loop(loop_args)
        update_args = argparse.Namespace(cases=str(cases_path), run=str(run_path), output=str(cases_path), repo=str(tmp_path))
        update_results(update_args)
        render_args = argparse.Namespace(
            cases=str(cases_path),
            run=str(run_path),
            review=str(review_path),
            code_review=str(code_review_path),
            completion_check=str(completion_path),
            risk_analysis=str(risk_path),
            readiness_check=None,
            output=str(report_path),
            title="QA Agent Self Test",
            allow_mojibake=False,
        )
        render_report(render_args)
        if not report_path.exists() or "QA Agent Self Test" not in report_path.read_text(encoding="utf-8"):
            raise QaAgentError("self-test report rendering failed")
        readiness = assert_readiness_data(
            completion,
            good_review,
            report_path=report_path,
            report_freshness_data={"status": "passed", "decision": "Ready"},
            evidence_integrity_data={"status": "passed", "findings": []},
        )
        write_json(readiness_path, readiness)
        if readiness.get("status") != "passed" or readiness.get("decision") != "Ready":
            raise QaAgentError("self-test readiness gate failed completed QA")
        real_local_summary_path = tmp_path / "real-local-e2e-summary.json"
        real_local_report_path = tmp_path / "real-local-e2e-report.html"
        real_local_normalized_path = tmp_path / "real-local-e2e-normalized.json"
        write_json(
            real_local_summary_path,
            {
                "status": "passed",
                "runDir": str(tmp_path),
                "environment": {
                    "h5BaseUrl": "http://127.0.0.1:3002",
                    "adminBaseUrl": "http://127.0.0.1:4010",
                    "backendBaseUrl": "http://127.0.0.1:8080/api",
                    "mode": "real-local",
                },
                "realLocalFlow": {
                    "h5": {
                        "status": "passed",
                        "steps": [{"name": "h5_submit", "ok": True, "detail": "status=200"}],
                        "console": [{"type": "error", "text": "WebSocket connection to ws://127.0.0.1/_next/webpack-hmr failed"}],
                        "requests": [{"type": "response", "url": "http://127.0.0.1:3002/api/creator/social-submissions", "status": 200}],
                    },
                    "admin": {
                        "status": "passed",
                        "steps": [{"name": "admin_approve", "ok": True, "detail": "status=200"}],
                        "console": [{"type": "debug", "text": "[vite] connected."}],
                        "requests": [{"type": "response", "url": "http://127.0.0.1:4010/api/creator-video-review/videos/1/review", "status": 200}],
                    },
                    "dbFinal": {
                        "submissionId": 1,
                        "submissionStatus": "REWARD_ISSUED",
                        "reviewVideoId": 2,
                        "reviewStatus": "approved",
                        "rewardAmount": "3.00",
                    },
                },
                "existingPlaywrightSuites": {"h5": {"status": "passed", "tests": 1}},
                "artifacts": [],
                "notes": [],
            },
        )
        render_real_local_e2e_report(
            argparse.Namespace(
                run=str(real_local_summary_path),
                output=str(real_local_report_path),
                normalized_json=str(real_local_normalized_path),
                title=None,
                embed_screenshots=False,
                allow_mojibake=False,
            )
        )
        normalized = read_json(real_local_normalized_path)
        if normalized.get("readiness", {}).get("decision") != "Conditionally Ready":
            raise QaAgentError("self-test real-local readiness failed")
        if normalized.get("browserNoise", {}).get("h5", {}).get("counts", {}).get("dev-server-noise") != 1:
            raise QaAgentError("self-test browser noise classification failed")
        check_local_stack(
            argparse.Namespace(
                repo=str(tmp_path),
                h5_url="http://127.0.0.1:1",
                admin_url=None,
                backend_url=None,
                required="",
                health_path="",
                timeout=1,
                json=str(tmp_path / "local-stack.json"),
                strict=False,
            )
        )
        bad_path = tmp_path / "mojibake.txt"
        bad_path.write_text(mojibake_sample, encoding="utf-8")
        if scan_mojibake_paths([bad_path])["status"] != "failed":
            raise QaAgentError("self-test mojibake path scan failed")
        print("SELF_TEST_OK")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quality Assurance Agent helper CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-layout", help="initialize structured .qa-agent directories and gitignore")
    p.add_argument("--repo", default=".")
    p.add_argument("--write-gitignore", action="store_true", help="overwrite/create .qa-agent/.gitignore")
    p.add_argument("--root-gitignore", action="store_true", help="append recommended root .gitignore snippet")
    p.add_argument("--json", help="optional layout summary output path")
    p.set_defaults(func=init_layout)

    p = sub.add_parser("init-project", help="一键初始化 .qa-agent 布局、配置、模板和 Playwright/MySQL 集成")
    p.add_argument("--repo", default=".")
    p.add_argument("--force", action="store_true", help="覆盖已生成的模板和 profile（不会覆盖本地密钥文件）")
    p.add_argument("--root-gitignore", action="store_true", help="追加推荐的根目录 .gitignore 片段")
    p.add_argument("--agent", default=AGENT_CLAUDE, type=normalize_agent_name, choices=list(AGENT_CHOICES),
                   help="Agent 类型：claude-code / codex / both（两者都装，不覆盖已存在的）。兼容旧名 claude")
    p.add_argument("--verify-mysql-mcp", action="store_true", help="初始化后短暂启动 mysql_mcp 以验证数据库连通性")
    p.add_argument("--mysql-mcp-timeout", type=int, default=12, help="--verify-mysql-mcp 的连接超时秒数（默认 12）")
    p.add_argument("--skip-playwright-runtime", action="store_true", help="初始化时不自动安装 Playwright 运行时（@playwright/test + 浏览器）")
    p.add_argument("--json", help="可选的初始化摘要 JSON 输出路径")
    p.set_defaults(func=init_project)

    p = sub.add_parser("init-config", help="write example config")
    p.add_argument("--output", default=".qa-agent/config/qa-agent.config.yaml")
    p.add_argument("--repo", default=".", help="target repo for optional Playwright Test Agents auto-install")
    p.add_argument("--loop", default=AGENT_CODEX, type=normalize_agent_name, choices=[AGENT_CODEX, AGENT_CLAUDE],
                   help="agent loop for Playwright Test Agents（codex / claude-code）")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--skip-playwright-agents", action="store_true", help="only write config; do not auto-install Playwright Test Agents")
    p.add_argument("--skip-detect-commands", action="store_true", help="write the generic example config instead of detected project commands")
    p.add_argument("--force-playwright-agents", action="store_true", help="install Playwright Test Agents even if Playwright is not detected")
    p.add_argument("--skip-mysql-mcp", action="store_true", help="do not auto-sync mysql_mcp from .mcp.json to Codex config")
    p.add_argument("--verify-mysql-mcp", action="store_true", help="start mysql_mcp briefly after sync to verify DB connectivity")
    p.add_argument("--mysql-mcp-server-name", default="mysql_mcp")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-yes", action="store_true", help="do not pass -y to npx")
    p.set_defaults(func=init_config)

    p = sub.add_parser("init", help="initialize .qa-agent config")
    p.add_argument("--output", default=".qa-agent/config/qa-agent.config.yaml")
    p.add_argument("--repo", default=".", help="target repo for optional Playwright Test Agents auto-install")
    p.add_argument("--loop", default=AGENT_CODEX, type=normalize_agent_name, choices=[AGENT_CODEX, AGENT_CLAUDE],
                   help="agent loop for Playwright Test Agents（codex / claude-code）")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--skip-playwright-agents", action="store_true", help="only write config; do not auto-install Playwright Test Agents")
    p.add_argument("--skip-detect-commands", action="store_true", help="write the generic example config instead of detected project commands")
    p.add_argument("--force-playwright-agents", action="store_true", help="install Playwright Test Agents even if Playwright is not detected")
    p.add_argument("--skip-mysql-mcp", action="store_true", help="do not auto-sync mysql_mcp from .mcp.json to Codex config")
    p.add_argument("--verify-mysql-mcp", action="store_true", help="start mysql_mcp briefly after sync to verify DB connectivity")
    p.add_argument("--mysql-mcp-server-name", default="mysql_mcp")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-yes", action="store_true", help="do not pass -y to npx")
    p.set_defaults(func=init_config)

    p = sub.add_parser("collect-context", help="collect repo context")
    p.add_argument("--repo", default=".")
    p.add_argument("--base", default=None)
    p.add_argument(
        "--scope",
        default="branch",
        choices=[
            "requirement",
            "current-requirement",
            "module",
            "current-module",
            "uncommitted",
            "current-changes",
            "working-tree",
            "latest-commit",
            "head",
            "branch",
            "current-branch",
        ],
    )
    p.add_argument("--module")
    p.add_argument("--commit")
    p.add_argument("--output", default=".qa-agent/current/context.json")
    p.add_argument("--doc-limit", type=int, default=12000)
    p.set_defaults(func=collect_context)

    p = sub.add_parser("validate-cases", help="validate test-cases.json")
    p.add_argument("--cases", required=True)
    p.add_argument("--normalize", action="store_true")
    p.add_argument("--summary", action="store_true", help="print a human-readable test-case summary table")
    p.add_argument("--check-mojibake", action="store_true", help="fail if the cases file contains mojibake or unreadable text")
    p.add_argument("--strict-language", action="store_true", help="fail if business case narrative is not Simplified Chinese")
    p.set_defaults(func=validate_cases)

    p = sub.add_parser("promote-cases", help="copy confirmed working cases into tracked .qa-agent/cases")
    p.add_argument("--cases", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--module", help="stable module/domain name used for the target file name")
    p.add_argument("--output", help="default: .qa-agent/cases/<module-or-requirement>.json")
    p.set_defaults(func=promote_cases)

    p = sub.add_parser("summarize-cases", help="print a human-readable test-case summary table")
    p.add_argument("--cases", required=True)
    p.set_defaults(func=summarize_cases)

    p = sub.add_parser("review-cases", help="run 3-model review")
    p.add_argument("--cases", required=True)
    p.add_argument("--context")
    p.add_argument("--output", default=".qa-agent/current/model-review.json")
    # 默认值一律留 None：具体默认值由 resolve_llm_settings 决定，
    # 这样配置文件 llm 段才有机会生效（传了才叫显式指定）。
    p.add_argument("--config", default=None,
                   help=f"配置文件路径，默认 {DEFAULT_LLM_CONFIG_PATH}")
    p.add_argument("--models", default=None,
                   help="逗号分隔的模型名；缺省读 config 的 llm.models，再缺省用内置默认三个")
    p.add_argument("--base-url", default=None)
    p.add_argument("--base-url-env", default=None)
    p.add_argument("--api-key-env", default=None)
    p.add_argument("--timeout", type=int, default=None, help=f"单模型超时秒数，默认 {DEFAULT_LLM_TIMEOUT_SECONDS}")
    p.add_argument("--stream", dest="stream", action="store_true", default=None,
                   help="强制流式（默认即流式；端点不支持时自动降级一次非流式）")
    p.add_argument("--no-stream", dest="stream", action="store_false",
                   help="关闭流式，改用普通 JSON 响应")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-retries", type=int, default=2, help="max retries per model on retryable transport/HTTP errors")
    p.add_argument("--retry-backoff-seconds", type=float, default=2.0, help="base seconds for exponential backoff between retries")
    p.set_defaults(func=review_cases)

    p = sub.add_parser("split-plan", help="split cases by test layer")
    p.add_argument("--cases", required=True)
    p.add_argument("--output", default=".qa-agent/current/test-plan.json")
    p.set_defaults(func=split_plan)

    p = sub.add_parser("index-existing-cases", help="index existing business cases, spec tasks, and test files before generating new work")
    p.add_argument("--repo", default=".")
    p.add_argument("--output", default=".qa-agent/current/existing-case-index.json")
    p.set_defaults(func=index_existing_cases)

    p = sub.add_parser("merge-existing-cases", help="reuse matching existing cases and append only missing generated cases")
    p.add_argument("--repo", default=".")
    p.add_argument("--generated", required=True, help="newly generated test-cases.json")
    p.add_argument("--existing-index", help="existing-case-index.json; defaults to scanning repo")
    p.add_argument("--output", default=".qa-agent/current/test-cases.json")
    p.set_defaults(func=merge_existing_cases)

    p = sub.add_parser("analyze-risks", help="identify high-risk business paths before case generation")
    p.add_argument("--repo", default=".")
    p.add_argument("--context")
    p.add_argument("--existing-index")
    p.add_argument("--module", help="逗号分隔的模块路径（如 src/main/java/.../service/impl/OpenBoxServiceImpl.java），限定风险扫描范围；未指定时回退至 git diff，无 diff 时扫描全仓库")
    p.add_argument("--output", default=".qa-agent/current/risk-analysis.json")
    p.set_defaults(func=analyze_risks)

    p = sub.add_parser("generate-spec-tasks", help="expand confirmed business cases into executable spec tasks")
    p.add_argument("--cases", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--output", default=".qa-agent/current/test-spec-tasks.json")
    p.add_argument("--ratio", default="unit=0.60,integration=0.20,api=0.15,e2e=0.05")
    p.add_argument("--min-specs-by-priority", default="P0=8,P1=5,P2=3,P3=1")
    p.add_argument("--max-e2e-per-case", type=int, default=2)
    p.set_defaults(func=generate_spec_tasks)

    p = sub.add_parser("coverage-balance", help="validate spec task pyramid ratio and per-case task counts")
    p.add_argument("--spec-tasks", required=True)
    p.add_argument("--output", default=".qa-agent/current/coverage-balance.json")
    p.add_argument("--ratio", default=None, help="目标配比；默认读 spec-tasks 的 targetRatio")
    p.add_argument("--min-specs-by-priority", default=None, help="每优先级最小 task 数；默认读 spec-tasks 的 minSpecsByPriority")
    p.add_argument("--tolerance", type=float, default=0.15)
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=coverage_balance)

    p = sub.add_parser("assert-oracle-mapping", help="verify every P0/P1 risk's requiredAssertions appear in spec-task oracle")
    p.add_argument("--risk-analysis", required=True, help="path to risk-analysis.json")
    p.add_argument("--spec-tasks", required=True, help="path to test-spec-tasks.json")
    p.add_argument("--output", help="optional output path for gate result JSON")
    p.set_defaults(func=assert_oracle_mapping)

    p = sub.add_parser("assert-script-implementation", help="fail when targetFile 映射的任务数超过测试方法数（映射≠实现）")
    p.add_argument("--spec-tasks", required=True, help="path to test-spec-tasks.json")
    p.add_argument("--repo", default=".", help="repo root（用于解析 targetFile 相对路径）")
    p.add_argument("--output", help="optional output path for gate result JSON")
    p.set_defaults(func=assert_script_implementation)

    p = sub.add_parser("assert-completion", help="fail if confirmed spec tasks are not implemented and executed")
    p.add_argument("--cases", required=True)
    p.add_argument("--spec-tasks", required=True)
    p.add_argument("--output", default=".qa-agent/current/completion-check.json")
    p.add_argument("--priorities", default="P0,P1", help="comma-separated priorities to enforce")
    p.add_argument("--allow-blocked", action="store_true", help="allow blocked tasks only when blocker/evidence/owner/nextAction are present")
    p.add_argument("--allow-deferred", action="store_true", help="allow deferred tasks only when blocker/evidence/owner/nextAction are present")
    p.add_argument("--allow-skipped", action="store_true", help="allow skipped tasks only when blocker/evidence/owner/nextAction are present")
    p.add_argument("--fail-on-failed", action="store_true", help="also fail when a task executed and failed; without this, failures mean complete but not ready")
    p.add_argument("--min-specs-by-priority", default="P0=8,P1=5,P2=3,P3=1", help="minimum task count per enforced case priority")
    p.set_defaults(func=assert_completion)

    p = sub.add_parser("assert-code-review", help="fail if code review is missing or has unresolved blocking findings")
    p.add_argument("--code-review", required=True)
    p.add_argument("--output", default=".qa-agent/current/code-review-check.json")
    p.add_argument("--repo", default=".")
    p.set_defaults(func=assert_code_review)

    p = sub.add_parser("aggregate-runs", help="aggregate .qa-agent/runs/*.log into latest-run.json")
    p.add_argument("--repo", default=".", help="repo root (default: cwd)")
    p.add_argument("--output", help="output path (default: <repo>/.qa-agent/current/latest-run.json)")
    p.set_defaults(func=aggregate_runs)

    p = sub.add_parser("assert-readiness", help="combine completion, code review, and report evidence into final readiness")
    p.add_argument("--completion-check", required=True)
    p.add_argument("--code-review", required=True)
    p.add_argument("--report")
    p.add_argument("--report-freshness-check")
    p.add_argument("--evidence-integrity-check", help="evidence-integrity-check.json 路径（可选）")
    p.add_argument("--output", default=".qa-agent/current/readiness-check.json")
    p.set_defaults(func=assert_readiness)

    p = sub.add_parser("assert-evidence-integrity", help="fail when execution evidence chain is incomplete (empty run / unmatched logs / missing task runs)")
    p.add_argument("--cases", help="test-cases.json 路径")
    p.add_argument("--spec-tasks", help="test-spec-tasks.json 路径")
    p.add_argument("--run", help="latest-run.json 路径")
    p.add_argument("--code-review", help="code-review.json 路径（校验 scope schema）")
    p.add_argument("--output", default=".qa-agent/current/evidence-integrity-check.json")
    p.set_defaults(func=assert_evidence_integrity)

    p = sub.add_parser("assert-report-freshness", help="fail when the rendered report is older than source gate artifacts")
    p.add_argument("--report", required=True)
    p.add_argument("--cases")
    p.add_argument("--run")
    p.add_argument("--review")
    p.add_argument("--code-review")
    p.add_argument("--completion-check")
    p.add_argument("--risk-analysis")
    p.add_argument("--readiness-check")
    p.add_argument("--spec-tasks")
    p.add_argument("--output", default=".qa-agent/current/report-freshness-check.json")
    p.set_defaults(func=assert_report_freshness)

    p = sub.add_parser("separate-technical-checks", help="move tool/environment pseudo-cases into qualityGates/environmentChecks")
    p.add_argument("--cases", required=True)
    p.add_argument("--output", help="default overwrites --cases")
    p.set_defaults(func=separate_technical_checks)

    p = sub.add_parser("check-mojibake", help="scan text artifacts for mojibake/unreadable UTF-8")
    p.add_argument("paths", nargs="+")
    p.add_argument("--json", help="optional JSON output path")
    p.add_argument("--strict", action="store_true", help="exit non-zero when mojibake is found")
    p.add_argument("--max-bytes", type=int, default=2_000_000)
    p.add_argument("--max-findings-per-file", type=int, default=20)
    p.set_defaults(func=check_mojibake)

    p = sub.add_parser("run-commands", help="run configured quality-gate commands")
    p.add_argument("--repo", default=".")
    p.add_argument("--config", default=".qa-agent/config/qa-agent.config.yaml")
    p.add_argument("--gate", required=True, choices=["unit", "api", "integration", "e2e", "review"])
    p.add_argument("--output", default=".qa-agent/runs/run.json")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--continue-on-failure", action="store_true")
    p.set_defaults(func=run_commands)

    p = sub.add_parser("run-loop", help="run configured gates as a loop harness")
    p.add_argument("--repo", default=".")
    p.add_argument("--config", default=".qa-agent/config/qa-agent.config.yaml")
    p.add_argument("--gates", default="unit,api,integration,e2e,review")
    p.add_argument("--output", default=".qa-agent/runs/latest-run.json")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--continue-on-failure", action="store_true")
    p.set_defaults(func=run_loop)

    p = sub.add_parser("update-results", help="update test-case statuses from run evidence")
    p.add_argument("--cases", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--output")
    p.add_argument("--repo", default=".")
    p.add_argument("--legacy-gate-mapping", action="store_true", help="old behavior: mark all cases in a passed layer as passed; prefer explicit caseResults instead")
    p.set_defaults(func=update_results)

    p = sub.add_parser("doctor", help="环境健康检查，自动补全 Playwright 和 MySQL 配置")
    p.add_argument("--repo", default=".")
    p.add_argument("--json")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--ignore", action="append", default=[], metavar="CHECK",
                   help="显式豁免某个必须项（可重复），例如 --ignore service:web:reachable。"
                        "被豁免的项仍会执行并出现在输出里，只是不再阻塞退出码——"
                        "用于「我知道它现在不可达，仍要继续」的场景。")
    p.add_argument("--agent", default=AGENT_CLAUDE, type=normalize_agent_name, choices=list(AGENT_CHOICES),
                   help="Agent 类型：claude-code / codex / both。兼容旧名 claude")
    p.add_argument("--check-services", action="store_true", help="探测 .qa-agent/local/.env 中配置的服务 URL 可达性")
    p.add_argument(
        "--require-mobile-e2e",
        action="store_true",
        dest="require_mobile_e2e",
        help="将移动端检测项升级为阻断：缺 adb 或 mobile-mcp 未注册时 doctor 失败",
    )
    p.add_argument("--auto-start", action="store_true", help="若服务不可达则自动启动（使用 init 推导的 startCmd/readySignal/dir，可在 services.local.json 覆盖）")
    p.add_argument("--no-kill", action="store_true", help="--auto-start 时不杀占用端口的旧进程，仅跳过")
    p.add_argument("--start-timeout", type=int, default=120, help="自动启动等待超时秒数（默认 120）")
    p.add_argument("--service-timeout", type=int, default=5)
    p.add_argument("--config-mysql-mcp", action="store_true", help="自动配置 mysql_mcp（.mcp.json + .codex/config.toml），默认只检查不改配置")
    p.add_argument("--verify-mysql-mcp", action="store_true", help="短暂启动 mysql_mcp 以验证数据库连通性（默认只检查配置是否存在，不做连接验证）")
    p.add_argument("--mysql-mcp-timeout", type=int, default=12, help="--verify-mysql-mcp 的连接超时秒数（默认 12）")
    p.set_defaults(func=doctor)

    p = sub.add_parser("check-local-stack", help="probe real-local H5/Admin/Backend service readiness")
    p.add_argument("--repo", default=".")
    p.add_argument("--h5-url")
    p.add_argument("--admin-url")
    p.add_argument("--backend-url")
    p.add_argument("--required", default="h5,admin,backend", help="comma-separated required services")
    p.add_argument("--health-path", default="", help="optional path appended to every base URL")
    p.add_argument("--timeout", type=int, default=5)
    p.add_argument("--json")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=check_local_stack)

    p = sub.add_parser("summarize-surefire", help="summarize Maven Surefire XML reports")
    p.add_argument("--reports", required=True, help="path to target/surefire-reports")
    p.add_argument("--output", help="optional JSON output path")
    p.add_argument("--quality-gate-output", help="optional qualityGate JSON output path")
    p.add_argument("--command", default="mvn -q test -DskipITs", help="展示用命令；建议显式传入 cd <project> && ... 以反映实际项目")
    p.set_defaults(func=summarize_surefire)

    p = sub.add_parser("install-playwright-agents", help="install official Playwright Test Agents")
    p.add_argument("--repo", default=".")
    # 不设 choices：取值最终透传给 Playwright init-agents，除 claude/codex 外它还支持
    # copilot / opencode / vscode 等。这里只做旧名归一（claude → claude-code），
    # 调用 Playwright 时再翻译回去。
    p.add_argument("--loop", default=AGENT_CODEX, type=normalize_agent_name,
                   help="Playwright Test Agents 的 loop（claude-code / codex / copilot / opencode / vscode…）")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--skip-if-present", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-yes", action="store_true", help="do not pass -y to npx")
    p.set_defaults(func=install_playwright_agents)

    p = sub.add_parser("install-playwright-runtime", help="install Playwright runtime (@playwright/test + browsers)")
    p.add_argument("--repo", default=".")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--skip-if-present", action="store_true", help="若运行时已就绪则跳过")
    p.add_argument("--skip-browsers", action="store_true", help="跳过浏览器二进制安装")
    p.add_argument("--dry-run", action="store_true", help="仅打印命令不执行")
    p.set_defaults(func=install_playwright_runtime)

    p = sub.add_parser("install-mysql-mcp", help="自动从 .env 生成 MySQL 凭证和 MCP 配置")
    p.add_argument("--repo", default=".")
    p.add_argument("--server-name", default="mysql_mcp")
    p.add_argument("--verify", action="store_true", help="启动 mysql_mcp 验证数据库连接")
    p.add_argument("--timeout", type=int, default=12)
    p.add_argument("--json")
    p.set_defaults(func=install_mysql_mcp)

    p = sub.add_parser("install-mobile-e2e", help="准备 Android 真机 E2E 工具链（mobile-mcp + adb）")
    p.add_argument("--repo", default=".")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--no-adb", action="store_true", help="跳过 Android Platform Tools 安装")
    p.add_argument("--with-mobilewright", action="store_true", help="一并安装 @mobilewright/test")
    p.add_argument("--add-to-path", action="store_true",
                   help="把 platform-tools 加入用户 PATH（Windows 写注册表；幂等）")
    p.add_argument("--dry-run", action="store_true", help="仅打印命令不执行")
    p.set_defaults(func=install_mobile_e2e)

    p = sub.add_parser("config-mysql-mcp", help="根据 QA_AGENT 配置 mysql_mcp（.mcp.json / .codex/config.toml）")
    p.add_argument("--repo", default=".")
    p.set_defaults(func=config_mysql_mcp)

    p = sub.add_parser("install-skill", help="install this skill into a supported agent")
    p.add_argument("--target", default=AGENT_CODEX, type=normalize_agent_name,
                   choices=[AGENT_CLAUDE, AGENT_CODEX],
                   help="安装目标 agent：claude-code / codex。兼容旧名 claude")
    p.add_argument("--path", help="skills directory; defaults by target")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=install_skill)

    p = sub.add_parser("version", help="print skill version")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("ensure-branch", help="create or switch QA feature branch")
    p.add_argument("--repo", default=".")
    p.add_argument("--branch")
    p.add_argument("--slug")
    p.set_defaults(func=ensure_branch)

    p = sub.add_parser("atomic-commit", help="commit a focused QA fix")
    p.add_argument("--repo", default=".")
    p.add_argument("--message", required=True)
    p.add_argument("--body")
    p.add_argument("--config", default=".qa-agent/config/qa-agent.config.yaml", help="used to read repair.allowedPaths/deniedPaths; ignored if the file does not exist")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=atomic_commit)

    p = sub.add_parser("render-report", help="render self-contained HTML report")
    p.add_argument("--cases")
    p.add_argument("--run")
    p.add_argument("--review")
    p.add_argument("--code-review")
    p.add_argument("--spec-tasks")
    p.add_argument("--completion-check", help="completion-check.json from assert-completion; missing explicit path marks report incomplete")
    p.add_argument("--risk-analysis", help="risk-analysis.json from analyze-risks")
    p.add_argument("--readiness-check", help="readiness-check.json from assert-readiness")
    p.add_argument("--output", default=".qa-agent/reports/latest-report.html")
    p.add_argument("--title")
    # 归档副本功能（见 render_report 尾部的「方案 A」）一直没被接出来：
    # 实现里用 getattr(args, "module"/"run_type") 取值，而这两个参数从未注册，
    # 所以永远取到 None——文档里写的「渲染时通过 --module --run-type 自动生成归档副本」
    # 实际不生效，使用者只能自己 cp。
    p.add_argument("--module", help="模块名；与 --run-type 一起给出时额外输出归档副本 {module}-{runType}-{时间戳}.html")
    p.add_argument("--run-type", dest="run_type", help="运行类型（如 acceptance / regression）")
    p.add_argument("--allow-mojibake", action="store_true", help="do not fail if rendered HTML contains known raw mojibake evidence")
    p.set_defaults(func=render_report)

    p = sub.add_parser("qa-self-check", help="QA 自检：报告数据自洽性/产物齐全性检测（一期 SC-001）")
    p.add_argument("--report", required=True, help="报告 HTML 路径")
    p.add_argument("--current", required=True, help=".qa-agent/current 目录")
    p.add_argument("--output", help="自检结果 JSON 路径，默认 .qa-agent/current/self-check.json")
    p.add_argument("--webhook", help="webhook URL（可选，优先于 config notify.webhook；两者都未配置则不发送通知）")
    p.set_defaults(func=qa_self_check)

    p = sub.add_parser("render-real-local-e2e-report", help="render a dedicated real-local E2E HTML report from a summary JSON")
    p.add_argument("--run", required=True, help="real-local-e2e-summary.json path")
    p.add_argument("--output", default=".qa-agent/reports/latest-real-local-e2e-report.html")
    p.add_argument("--normalized-json", help="optional normalized evidence JSON output")
    p.add_argument("--title")
    p.add_argument("--embed-screenshots", action="store_true")
    p.add_argument("--allow-mojibake", action="store_true", help="do not fail if rendered HTML contains known raw mojibake evidence")
    p.set_defaults(func=render_real_local_e2e_report)

    p = sub.add_parser("render-cases", help="render HTML test-case confirmation page")
    p.add_argument("--cases", required=True)
    p.add_argument("--output", default=".qa-agent/current/test-cases.html")
    p.add_argument("--title")
    p.add_argument("--no-summary", action="store_true", help="do not print the test-case summary table")
    p.add_argument("--allow-mojibake", action="store_true", help="do not fail if rendered HTML contains known raw mojibake evidence")
    p.set_defaults(func=render_cases)

    p = sub.add_parser("save-knowledge", help="追加一条项目经验到 .qa-agent/knowledge/")
    p.add_argument("--repo", default=".")
    p.add_argument("--module", required=True, help="模块名，如 order")
    p.add_argument("--category", required=True, help="分类：api-quirk / environment / data-prep / test-pattern / bug-pattern")
    p.add_argument("--summary", required=True, help="一句话描述")
    p.add_argument("--detail", help="详细说明")
    p.add_argument("--tags", help="逗号分隔的标签")
    p.set_defaults(func=cmd_save_knowledge)

    p = sub.add_parser("show-knowledge", help="查看 .qa-agent/knowledge/ 中的经验记录")
    p.add_argument("--repo", default=".")
    p.add_argument("--module", help="指定模块，不指定则显示全部")
    p.add_argument("--category", help="过滤分类：api-quirk / environment / data-prep / test-pattern / bug-pattern")
    p.set_defaults(func=cmd_show_knowledge)

    p = sub.add_parser("manifest", help="查看当前 QA 流程 manifest（上游产物路径和阶段状态）")
    p.add_argument("--repo", default=".")
    p.add_argument("--brief", action="store_true", help="输出人类可读汇总：当前阶段、产物是否已落盘、状态键")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("run-with-env", help="分层加载环境变量（config/env.shared → local/.env）并执行测试脚本")
    p.add_argument("--repo", default=".")
    p.add_argument("--script", required=True, help="测试脚本路径（相对于 --repo 或绝对路径）")
    p.add_argument("--extra", nargs="*", default=[], help="额外环境变量（VAR=value 格式）")
    p.add_argument("--case-id", help="用例 ID（如 TC-P0-001），写入 sidecar；缺省从脚本名推断")
    p.add_argument("--case-ids", help="一次执行覆盖的多条用例 ID（逗号分隔）。一个套件跑多条用例时必填，否则只有首条会被算作有执行记录")
    p.add_argument("--task-id", help="spec-task ID（如 SPEC-TC-P0-001-API-001），写入 sidecar")
    p.add_argument("--dry-run", action="store_true", help="仅打印命令不执行")
    p.add_argument("--timeout", type=int, default=120, help="超时秒数")
    p.set_defaults(func=run_with_env)

    p = sub.add_parser("run-e2e", help="在 playwrightDir 执行 npx playwright test <spec> 并写 run 证据（log+sidecar）")
    p.add_argument("--repo", default=".")
    p.add_argument("--spec", required=True, help="spec 文件路径（相对于 --repo 或绝对路径）")
    p.add_argument("--extra", nargs="*", default=[], help="额外环境变量（VAR=value 格式，如 QA_BOX_ID=232）")
    p.add_argument("--case-id", help="用例 ID（如 TC-P2-001），写入 sidecar；缺省从 spec 文件名推断")
    p.add_argument("--task-id", help="spec-task ID（如 SPEC-TC-P2-001-E2E-001），写入 sidecar")
    p.add_argument("--project", default="", help="Playwright project（可选）")
    p.add_argument("--timeout", type=int, default=180, help="超时秒数")
    p.set_defaults(func=run_e2e)

    p = sub.add_parser("safe-write-json", help="写入 JSON 文件并立即做编码完整性检查（规避 Write/Edit 工具的中文编码损坏风险）")
    p.add_argument("path", help="目标 JSON 文件路径")
    p.add_argument("--from-stdin", action="store_true", help="从 stdin 读取 JSON 内容（推荐，避免 AI 工具直接写文件）")
    p.add_argument("--json-string", help="直接传入 JSON 字符串")
    p.set_defaults(func=safe_write_json_cmd)

    p = sub.add_parser("self-test", help="run CLI self-test")
    p.set_defaults(func=self_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    if argv is None:
        argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print(SKILL_VERSION)
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_local_env(Path.cwd())
        if getattr(args, "repo", None):
            load_local_env(Path(args.repo).resolve())
        args.func(args)
        return 0
    except QaAgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


