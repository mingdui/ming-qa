"""无源码/移动端 profile 的回归测试。

这些路径共同解决一个问题：被测对象没有仓库、没有源码、没有单元测试层时，
原有流程会卡在「不是 git 仓库」「扫不到代码」「金字塔配比不达标」上。
每个用例都对应一个真实踩过的坑，不是为覆盖率凑数。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# --- collect-context：无 git 工作区 ---


def test_collect_context_rejects_non_git_without_flag(qa, tmp_path):
    """默认仍然拒绝裸目录——静默产出空上下文会让后续阶段误以为收集过。"""
    args = SimpleNamespace(
        repo=str(tmp_path), base=None, scope="branch", module=None, commit=None,
        output=str(tmp_path / "context.json"), doc_limit=1000, allow_non_git=False,
    )
    with pytest.raises(qa.QaAgentError) as exc:
        qa.collect_context(args)
    assert "--allow-non-git" in str(exc.value)


def test_collect_context_allow_non_git_builds_minimal_context(qa, tmp_path):
    out = tmp_path / "context.json"
    args = SimpleNamespace(
        repo=str(tmp_path), base=None, scope="branch", module=None, commit=None,
        output=str(out), doc_limit=1000, allow_non_git=True,
    )
    qa.collect_context(args)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["gitAvailable"] is False
    assert data["scope"]["mode"] == "non-git"
    assert data["gitDiffNameOnly"] == []
    assert data["currentBranch"] == ""
    # 不能编造 diff——无 git 时这些字段必须为空
    assert data["gitDiffStat"] == ""


# --- analyze-risks --surface mobile ---


def test_analyze_risks_mobile_surface_returns_explicit_skeleton(qa, tmp_path):
    """无源码时不做关键词扫描：输出固定的移动端风险骨架，并标明边界。"""
    result = qa.analyze_risks_data(tmp_path, surface="mobile")

    assert result["riskSkeleton"] == "mobile-surface"
    assert result["scope"]["surface"] == "mobile"
    assert result["summary"]["inspectedFiles"] == 0
    assert result["inspectedFiles"] == []
    assert len(result["risks"]) >= 5
    assert "无源码边界" in result["summary"]["note"]

    for risk in result["risks"]:
        assert risk["affectedFiles"] == [], "无源码时不能编造受影响文件"
        assert risk["requiresE2E"] is True, "移动端风险必须走 e2e 层"
        assert risk["surface"] == "mobile"
    ids = [risk["id"] for risk in result["risks"]]
    assert len(ids) == len(set(ids))
    assert result["coverageGaps"], "骨架风险默认都缺证据，必须显式列出 gap"


def test_analyze_risks_code_surface_is_unchanged(qa, tmp_path):
    """默认 surface=code 不触发骨架逻辑，避免影响有源码项目。"""
    result = qa.analyze_risks_data(tmp_path)
    assert result.get("riskSkeleton") is None


# --- coverage-balance --profile mobile ---


def _all_e2e_tasks() -> dict:
    return {
        "targetRatio": {"unit": 0, "integration": 0, "api": 0, "e2e": 1},
        "minSpecsByPriority": {"P0": 1, "P1": 1, "P2": 1, "P3": 1},
        "tasks": [
            {"id": "SPEC-C1-E2E-001", "layer": "e2e", "sourceCaseId": "C1", "priority": "P0"},
            {"id": "SPEC-C2-E2E-001", "layer": "e2e", "sourceCaseId": "C2", "priority": "P1"},
        ],
    }


def test_coverage_balance_mobile_profile_accepts_e2e_only(qa):
    result = qa.coverage_balance_data(_all_e2e_tasks(), profile="mobile", tolerance=0)

    assert result["status"] == "passed", result["findings"]
    types = {finding["type"] for finding in result["findings"]}
    assert "e2e-too-heavy" not in types, "移动端没有其他层可写，e2e 100% 不是缺陷"
    assert result["summary"]["targetRatio"]["e2e"] == 1.0


def test_coverage_balance_development_still_flags_e2e_heavy(qa):
    """同一份 e2e-only 任务在开发 profile 下仍应告警——判别力验证。"""
    result = qa.coverage_balance_data(_all_e2e_tasks(), profile="development", tolerance=0)
    types = {finding["type"] for finding in result["findings"]}
    assert "e2e-too-heavy" in types


# --- doctor --ignore 留痕 ---


def test_environment_checks_record_ignored_flag(qa):
    checks = [{
        "name": "service:web:reachable",
        "ok": False,
        "detail": "down",
        "required": True,
        "ignored": True,
        "ignoreReason": "按 --ignore 显式豁免：问题依旧存在，不计入阻塞",
    }]
    [entry] = qa.checks_to_environment_checks(checks)

    assert entry["ignored"] is True
    assert entry["ignoreReason"]
    # 豁免不改变事实：仍然是 blocked
    assert entry["status"] == "blocked"
    assert entry["required"] is True


def test_doctor_ignore_marks_check_in_environment_checks(qa, tmp_path, monkeypatch):
    """doctor 端到端：被 --ignore 的必须项仍出现，但带 ignored 标记。"""
    real_which = qa.shutil.which
    monkeypatch.setattr(qa.shutil, "which", lambda name: None if name == "rg" else real_which(name))
    out = tmp_path / "doctor.json"
    args = SimpleNamespace(
        repo=str(tmp_path / "missing-repo"),
        strict=False,
        json=str(out),
        ignore=["ripgrep"],
    )
    qa.doctor(args)

    data = json.loads(out.read_text(encoding="utf-8"))
    env = {item["name"]: item for item in data["environmentChecks"]}
    assert env["ripgrep"]["ignored"] is True
    assert env["ripgrep"]["ignoreReason"]
    assert env["ripgrep"]["status"] == "blocked"
    # 原始 checks 也保留 ignored，便于报告层直接读取
    raw = {item["name"]: item for item in data["checks"]}
    assert raw["ripgrep"]["ignored"] is True


# --- install-mobile-e2e：mobilecli 与真机发现 ---


def test_mobile_cli_missing_is_reported_not_available(qa, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda name: None)
    info = qa._mobile_cli_info()
    assert info["available"] is False
    assert info["path"] == ""


def test_install_mobile_e2e_verifies_mobilecli_and_devices(qa, tmp_path, monkeypatch):
    monkeypatch.setattr(
        qa, "_register_mobile_mcp",
        lambda repo: {"status": "passed", "path": str(repo / ".mcp.json")},
    )
    monkeypatch.setattr(
        qa, "_detect_adb_devices",
        lambda repo, timeout=15: {
            "available": True, "adbPath": "adb", "devices": ["SER"],
            "authorized": ["SER"], "unauthorized": [],
        },
    )
    monkeypatch.setattr(
        qa, "_mobile_cli_info",
        lambda timeout=15, repo=None: {"available": False, "path": "", "daemon": "", "bundled": False},
    )
    monkeypatch.setattr(qa, "_find_adb", lambda: "adb")
    monkeypatch.setattr(qa, "_adb_manual_steps", lambda repo: [])
    monkeypatch.setattr(qa.shutil, "which", lambda name: "adb" if name == "adb" else None)

    result = qa.install_mobile_e2e_in_repo(tmp_path)
    steps = {step["step"]: step for step in result["steps"]}

    # mobilecli 缺失不阻断脚本驱动，但必须显式告知
    assert steps["mobilecli"]["status"] == "manual"
    assert "mobilecli" in steps["mobilecli"]["reason"]
    assert steps["android-devices"]["status"] == "passed"
    assert result["status"] == "passed"


# --- init-project --profile mobile ---


def test_init_project_mobile_profile_skips_playwright_scaffold(qa, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    summary_path = tmp_path / "summary.json"
    qa.init_project(SimpleNamespace(
        repo=str(repo),
        force=False,
        root_gitignore=False,
        agent="claude",
        verify_mysql_mcp=False,
        mysql_mcp_timeout=1,
        skip_playwright_runtime=False,
        profile="mobile",
        json=str(summary_path),
    ))

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["projectProfile"] == "mobile"
    assert summary["testSuites"] == []
    assert not (repo / "playwright.config.ts").exists()
    assert not (repo / "playwright.config.js").exists()

    services = json.loads(
        (repo / ".qa-agent" / "config" / "services.json").read_text(encoding="utf-8")
    )
    assert services["services"] == [], "无源码移动端没有本地服务栈"

    profile = json.loads(
        (repo / ".qa-agent" / "profiles" / "project-test-profile.json").read_text(encoding="utf-8")
    )
    assert profile["profileMode"] == "mobile"
