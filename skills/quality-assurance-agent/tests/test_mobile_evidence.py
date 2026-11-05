#!/usr/bin/env python3
"""移动端证据门禁：元素树 + 截图 + logcat，少一样就不能通过。

这条门禁对应文档里的硬约定，也对应口播稿里最容易说大的一句：
「测试证据沉淀：元素树快照、截图、logcat。少一样，测试报告不通过。」

门禁只约束 executionStatus == passed 的移动端 task。blocked / failed /
not-run 各有自己的语义（blocker 证据、失败日志），在这里重复判只会制造噪音。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _spec(*tasks):
    return {"tasks": list(tasks)}


def _task(case_id="TC-P0-001", task_id="SPEC-TC-P0-001-E2E-001",
          platform="android-real", status="passed", oracle=None, refs=None):
    return {
        "id": task_id,
        "sourceCaseId": case_id,
        "platform": platform,
        "executionStatus": status,
        "implementationStatus": "implemented",
        "targetFile": "@agent:mobile-mcp",
        "layer": "e2e",
        "oracle": oracle if oracle is not None else {
            "ui": [{"assertion": "按钮状态符合预期"}],
        },
        "evidenceRefs": refs if refs is not None else {
            "viewTree": ["按钮状态符合预期"],
        },
    }


def _write_manifest(
    repo: Path,
    *,
    run_id: str = "1700000000",
    scope: str = "app",
    case_id: str = "TC-P0-001",
    task_id: str | None = None,
    device: str | None = "R58M12345",
    files: bool = True,
    screenshot: str = "screen.png",
):
    root = repo / ".qa-agent" / "runs" / run_id / "mobile" / scope
    root.mkdir(parents=True, exist_ok=True)
    evidence = {
        "viewTree": "view-tree.json",
        "screenshot": screenshot,
        "logcat": "logcat.txt",
    }
    if files:
        (root / "view-tree.json").write_text('{"nodes": []}', encoding="utf-8")
        (root / screenshot).write_bytes(b"\x89PNG\r\n\x1a\n")
        (root / "logcat.txt").write_text("11-05 12:00:00 I/app: ok\n", encoding="utf-8")
    manifest = {"caseId": case_id, "device": device, "platform": "android-real",
                "locator": "tree", "durationMs": 1200,
                "evidence": evidence}
    if task_id:
        manifest["taskId"] = task_id
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


def _types(result):
    return [finding["type"] for finding in result["findings"]]


# --- ① 三类证据齐全才通过 ---


def test_complete_evidence_passes(qa, tmp_path):
    _write_manifest(tmp_path)
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "passed", result["findings"]
    assert result["summary"]["passedMobileTasks"] == 1
    assert result["summary"]["manifests"] == 1


def test_missing_logcat_fails(qa, tmp_path):
    root = _write_manifest(tmp_path)
    (root / "logcat.txt").unlink()
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    missing = [f for f in result["findings"] if f["type"] == "mobile-evidence-missing"]
    assert any(f["kind"] == "logcat" for f in missing), result["findings"]


def test_missing_view_tree_fails(qa, tmp_path):
    root = _write_manifest(tmp_path)
    (root / "view-tree.json").unlink()
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert any(f.get("kind") == "viewTree" and f["type"] == "mobile-evidence-missing"
               for f in result["findings"])


def test_missing_screenshot_fails(qa, tmp_path):
    root = _write_manifest(tmp_path)
    (root / "screen.png").unlink()
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert any(f.get("kind") == "screenshot" and f["type"] == "mobile-evidence-missing"
               for f in result["findings"])


def test_empty_evidence_file_fails(qa, tmp_path):
    """文件在但内容为空，等于没有证据——`touch` 一个空文件不能算数。"""
    root = _write_manifest(tmp_path)
    (root / "logcat.txt").write_text("", encoding="utf-8")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-empty" in _types(result)


def test_screenshot_must_be_an_image(qa, tmp_path):
    """把过程日志改名成 .png 不能冒充截图。"""
    root = _write_manifest(tmp_path)
    (root / "screen.png").unlink()
    (root / "screen.txt").write_text("not an image", encoding="utf-8")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["evidence"]["screenshot"] = "screen.txt"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-screenshot-not-image" in _types(result)


# --- ② 哪些 task 需要证据 ---


def test_passed_task_without_manifest_fails(qa, tmp_path):
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-manifest-missing" in _types(result)


def test_blocked_task_does_not_require_manifest(qa, tmp_path):
    """blocked 由 blocker 证据约束，不能在这里再判一次缺证据。"""
    result = qa.assert_mobile_evidence_data(_spec(_task(status="blocked")), tmp_path)
    assert result["status"] == "passed", result["findings"]


def test_not_run_task_does_not_require_manifest(qa, tmp_path):
    result = qa.assert_mobile_evidence_data(_spec(_task(status="not-run")), tmp_path)
    assert result["status"] == "passed", result["findings"]


def test_web_task_is_ignored(qa, tmp_path):
    task = _task(platform="web", status="passed")
    result = qa.assert_mobile_evidence_data(_spec(task), tmp_path)
    assert result["status"] == "passed"
    assert result["summary"]["mobileTasks"] == 0


def test_manifest_lookup_is_case_insensitive(qa, tmp_path):
    _write_manifest(tmp_path, case_id="tc-p0-001")
    result = qa.assert_mobile_evidence_data(_spec(_task(case_id="TC-P0-001")), tmp_path)
    assert result["status"] == "passed", result["findings"]


def test_manifest_can_match_by_task_id(qa, tmp_path):
    """同一用例拆成多个 task 时，按 taskId 精确对应，避免一份清单蒙混多条 task。"""
    _write_manifest(tmp_path, case_id="OTHER-CASE", task_id="SPEC-TC-P0-001-E2E-001")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "passed", result["findings"]


def test_two_tasks_one_without_evidence(qa, tmp_path):
    _write_manifest(tmp_path, case_id="TC-P0-001", task_id="SPEC-A")
    tasks = [
        _task(case_id="TC-P0-001", task_id="SPEC-A"),
        _task(case_id="TC-P0-002", task_id="SPEC-B"),
    ]
    result = qa.assert_mobile_evidence_data(_spec(*tasks), tmp_path)
    assert result["status"] == "failed"
    missing = [f for f in result["findings"] if f["type"] == "mobile-evidence-manifest-missing"]
    assert [f["taskId"] for f in missing] == ["SPEC-B"]


# --- ③ 清单本身要合格 ---


def test_malformed_manifest_fails(qa, tmp_path):
    root = tmp_path / ".qa-agent" / "runs" / "1" / "mobile" / "app"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text("{ broken", encoding="utf-8")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-manifest-invalid" in _types(result)


def test_manifest_without_case_id_fails(qa, tmp_path):
    _write_manifest(tmp_path)
    path = next(tmp_path.rglob("manifest.json"))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop("caseId")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert "mobile-evidence-manifest-invalid" in _types(result)


def test_manifest_without_device_fails(qa, tmp_path):
    """多设备环境下，没写 device 的结果不可复现。"""
    _write_manifest(tmp_path, device="")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-device-missing" in _types(result)


def test_evidence_paths_are_relative_to_manifest(qa, tmp_path):
    """清单里的相对路径必须以清单所在目录为基准，不能按 cwd 解析。"""
    _write_manifest(tmp_path)
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "passed", result["findings"]


def _rename_log_key(root: Path, new_key: str):
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence"][new_key] = manifest["evidence"].pop("logcat")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_ios_device_log_alias_is_accepted(qa, tmp_path):
    """iOS 用 deviceLog（syslog），不能强迫它把键名改成 Android 的 logcat。"""
    root = _write_manifest(tmp_path)
    _rename_log_key(root, "deviceLog")
    result = qa.assert_mobile_evidence_data(
        _spec(_task(platform="ios-sim")), tmp_path,
    )
    assert result["status"] == "passed", result["findings"]


def test_syslog_alias_is_accepted(qa, tmp_path):
    root = _write_manifest(tmp_path)
    _rename_log_key(root, "syslog")
    result = qa.assert_mobile_evidence_data(
        _spec(_task(platform="ios-real")), tmp_path,
    )
    assert result["status"] == "passed", result["findings"]


# --- ④ 定位方式与耗时 ---


def _update_manifest(root: Path, **changes):
    path = root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_missing_locator_fails(qa, tmp_path):
    """没声明定位方式，就无法证明「先树后截图」。"""
    root = _write_manifest(tmp_path)
    _update_manifest(root, locator=None)
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-locator-invalid" in _types(result)


def test_screenshot_locator_requires_fallback_reason(qa, tmp_path):
    """用截图坐标兜底必须写原因——空树是客观限制，不是想跳过就跳过。"""
    root = _write_manifest(tmp_path)
    _update_manifest(root, locator="screenshot")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-fallback-reason-missing" in _types(result)


def test_screenshot_locator_with_reason_passes(qa, tmp_path):
    root = _write_manifest(tmp_path)
    _update_manifest(root, locator="screenshot",
                     fallbackReason="整屏只有 3 个无障碍节点，自绘棋盘无法定位")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "passed", result["findings"]


def test_missing_duration_fails(qa, tmp_path):
    root = _write_manifest(tmp_path)
    _update_manifest(root, durationMs=None)
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-duration-missing" in _types(result)


def test_summary_reports_duration(qa, tmp_path):
    _write_manifest(tmp_path)
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["summary"]["totalDurationMs"] == 1200
    assert result["summary"]["maxDurationMs"] == 1200
    assert result["summary"]["locatorTree"] == 1


# --- ⑤ 设备日志崩溃是 passed 的负向门禁 ---


def test_crash_in_log_blocks_passed_task(qa, tmp_path):
    """证据齐了但日志里有崩溃——结论被自己的证据否定。"""
    root = _write_manifest(tmp_path)
    (root / "logcat.txt").write_text(
        "11-05 12:00:00 E/AndroidRuntime: FATAL EXCEPTION: main\n", encoding="utf-8",
    )
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-logcat-crash" in _types(result)


def test_anr_in_log_blocks_passed_task(qa, tmp_path):
    root = _write_manifest(tmp_path)
    (root / "logcat.txt").write_text("11-05 12:00:00 ANR in com.example.app\n", encoding="utf-8")
    result = qa.assert_mobile_evidence_data(_spec(_task()), tmp_path)
    assert "mobile-logcat-crash" in _types(result)


def test_ios_crash_signature_blocks_passed_task(qa, tmp_path):
    root = _write_manifest(tmp_path)
    (root / "logcat.txt").write_text(
        "Terminating app due to uncaught exception 'NSInvalidArgumentException'\n",
        encoding="utf-8",
    )
    result = qa.assert_mobile_evidence_data(
        _spec(_task(platform="ios-sim")), tmp_path,
    )
    assert "mobile-logcat-crash" in _types(result)


# --- ⑥ oracle 断言必须有证据映射 ---


def test_missing_evidence_refs_fails(qa, tmp_path):
    root = _write_manifest(tmp_path)
    task = _task(refs={})
    result = qa.assert_mobile_evidence_data(_spec(task), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-evidence-refs-missing" in _types(result)


def test_assertion_without_evidence_mapping_fails(qa, tmp_path):
    _write_manifest(tmp_path)
    task = _task(
        oracle={"ui": [{"assertion": "按钮状态符合预期"}]},
        refs={"viewTree": ["另一条断言"]},
    )
    result = qa.assert_mobile_evidence_data(_spec(task), tmp_path)
    assert result["status"] == "failed"
    assert "mobile-assertion-without-evidence" in _types(result)


def test_all_assertions_mapped_passes(qa, tmp_path):
    _write_manifest(tmp_path)
    task = _task(
        oracle={
            "ui": [{"assertion": "按钮状态符合预期"}],
            "api": [{"assertion": "接口返回 200"}],
            "negativeAssertions": [{"assertion": "无重复扣款"}],
        },
        refs={
            "viewTree": ["按钮状态符合预期"],
            "screenshot": ["无重复扣款"],
            "logcat": ["接口返回 200"],
        },
    )
    result = qa.assert_mobile_evidence_data(_spec(task), tmp_path)
    assert result["status"] == "passed", result["findings"]


def test_task_without_oracle_does_not_require_refs(qa, tmp_path):
    """没有 oracle 断言的 task 没有可映射的内容——不制造空转的门禁。"""
    _write_manifest(tmp_path)
    task = _task(oracle={}, refs={})
    result = qa.assert_mobile_evidence_data(_spec(task), tmp_path)
    assert result["status"] == "passed", result["findings"]


# --- ⑦ 与 evidence-integrity 的合流 ---


def test_merge_marks_failure(qa):
    base = {"status": "passed", "summary": {"testCasesTotal": 1}, "findings": []}
    extra = {"status": "failed", "summary": {"mobileTasks": 1},
             "findings": [{"type": "mobile-evidence-missing", "severity": "fail"}]}
    merged = qa.merge_gate_results(base, extra)
    assert merged["status"] == "failed"
    assert merged["summary"]["mobileEvidence"]["mobileTasks"] == 1


def test_merge_preserves_base_findings(qa):
    base = {"status": "passed", "summary": {}, "findings": [{"type": "x", "severity": "warn"}]}
    extra = {"status": "passed", "summary": {}, "findings": []}
    merged = qa.merge_gate_results(base, extra)
    assert [f["type"] for f in merged["findings"]] == ["x"]


def test_report_section_shows_mobile_duration(qa):
    """报告要能回答「跑了多久」——否则「跑得慢」永远只是感觉。"""
    html = qa._render_mobile_evidence({"summary": {"mobileEvidence": {
        "mobileTasks": 2, "passedMobileTasks": 2, "manifests": 2,
        "locatorTree": 1, "locatorScreenshot": 1,
        "totalDurationMs": 3400, "maxDurationMs": 2000,
    }}})
    assert "移动端 E2E" in html
    assert "3.4s" in html
    assert "2.0s" in html
    assert "树 1 / 截图 1" in html


def test_report_section_absent_without_mobile_tasks(qa):
    assert qa._render_mobile_evidence({"summary": {"mobileEvidence": {"mobileTasks": 0}}}) == ""
    assert qa._render_mobile_evidence({}) == ""


def test_readiness_carries_mobile_evidence(qa):
    result = qa.assert_readiness_data(
        {"status": "passed", "decision": "complete"},
        {"status": "passed", "findings": []},
        report_freshness_data={"status": "passed"},
        evidence_integrity_data={
            "status": "passed",
            "summary": {"mobileEvidence": {"mobileTasks": 1}},
        },
    )
    assert result["summary"]["mobileEvidence"]["mobileTasks"] == 1


def test_assert_evidence_integrity_blocks_on_missing_mobile_evidence(qa, tmp_path, monkeypatch):
    """readiness 只读 evidence-integrity-check.json；移动端 finding 必须并进去。"""
    current = tmp_path / ".qa-agent" / "current"
    current.mkdir(parents=True)
    spec_path = current / "test-spec-tasks.json"
    spec_path.write_text(json.dumps(_spec(_task()), ensure_ascii=False), encoding="utf-8")
    run_path = current / "latest-run.json"
    run_path.write_text(json.dumps({
        "summary": {"totalCases": 1},
        "cases": [{"caseId": "TC-P0-001", "finalOutcome": "PASS"}],
        "unmatchedLogs": [],
    }), encoding="utf-8")
    output = current / "evidence-integrity-check.json"

    args = SimpleNamespace(
        cases=None, spec_tasks=str(spec_path), run=str(run_path),
        code_review=None, repo=str(tmp_path), output=str(output),
    )
    with pytest.raises(SystemExit):
        qa.assert_evidence_integrity(args)

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert "mobile-evidence-manifest-missing" in [f["type"] for f in result["findings"]]
