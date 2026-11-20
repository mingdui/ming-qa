"""门禁一致性：结论、证据、元数据对不上时必须红，而不是盖章。

这批用例对应 review 里最容易「全绿但没验证」的几处：

- run 说 FAIL、task 写 passed，证据门禁不比对两者；
- 移动端证据来自历史 run，manifest 不绑定本轮执行；
- evidenceRefs 只要求「断言字符串出现在某个 kind 的列表里」，
  把同一串复制到 viewTree 与 screenshot 就能凑满；
- spec-tasks 自述的 summary/perCase 与 tasks 漂移；
- 生成端按 mobile 档写 1 条/用例，校验端却按 P0=8/P1=5 判红；
- 子进程超时杀掉时不留执行记录。

每条都写成「没有这个修复就会失败」的形态。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


# --- 构造工具 ---------------------------------------------------------------


def _cases(*case_ids: str) -> dict:
    return {"version": "1.0", "status": "confirmed", "cases": [
        {"id": cid, "priority": cid.split("-")[1] if cid.count("-") >= 2 else "P0",
         "title": f"{cid} 用例", "status": "confirmed", "layer": "e2e",
         "module": "order", "type": "business",
         "businessActor": "普通用户", "operationPath": "打开 App",
         "preconditions": ["已登录"], "steps": ["进入结算"],
         "expected": ["下单成功"], "businessAssertions": ["下单成功"]}
        for cid in (case_ids or ("TC-P0-001",))
    ]}


def _run(final_outcome: str = "PASS", log_file: str = "run-TC-P0-001-tc-p0-001-1700000000.log") -> dict:
    failed = final_outcome.upper() != "PASS"
    return {
        "version": "1.0",
        "summary": {"totalCases": 1, "casesPassed": 0 if failed else 1},
        "unmatchedLogs": [],
        "cases": [{
            "caseId": "TC-P0-001",
            "totalAttempts": 1,
            "finalOutcome": final_outcome,
            "attempts": [{
                "n": 1,
                "logFile": log_file,
                "exitCode": 1 if failed else 0,
                "durationMs": 1234,
                "outcome": final_outcome,
            }],
        }],
    }


def _mobile_task(case_id="TC-P0-001", task_id="SPEC-TC-P0-001-E2E-001",
                 status="passed", refs=None, oracle=None) -> dict:
    return {
        "id": task_id,
        "sourceCaseId": case_id,
        "priority": "P0",
        "layer": "e2e",
        "platform": "android-real",
        "implementationStatus": "implemented",
        "executionStatus": status,
        "targetFile": "tests/e2e-mobile/app/tc-p0-001.spec.ts",
        "oracle": oracle if oracle is not None else {"ui": [{"assertion": "下单成功"}]},
        "evidenceRefs": refs if refs is not None else {"viewTree": ["下单成功"]},
    }


def _mobile_spec(*tasks) -> dict:
    return {"version": "1.0", "tasks": list(tasks)}


def _write_mobile_manifest(repo: Path, *, run_id="1700000000", case_id="TC-P0-001",
                           task_id=None, run_log=None, kinds=None) -> Path:
    root = repo / ".qa-agent" / "runs" / run_id / "mobile" / "app"
    root.mkdir(parents=True, exist_ok=True)
    evidence = {}
    for kind, name in (("viewTree", "view-tree.xml"), ("screenshot", "screen.png"),
                       ("logcat", "logcat.txt")):
        if kinds is not None and kind not in kinds:
            continue
        evidence[kind] = name
        payload = b"\x89PNG\r\n\x1a\n" if kind == "screenshot" else b"<x/>" if kind == "viewTree" else b"ok\n"
        (root / name).write_bytes(payload)
    manifest = {
        "caseId": case_id, "device": "R58M12345", "platform": "android-real",
        "locator": "tree", "durationMs": 1200, "evidence": evidence,
    }
    if task_id:
        manifest["taskId"] = task_id
    if run_log:
        manifest["runLog"] = run_log
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


def _types(result: dict) -> list[str]:
    return [f["type"] for f in result.get("findings", [])]


# --- ① run 说失败、task 说通过 ------------------------------------------------


def test_run_failed_but_task_passed_is_caught(qa):
    """跑失败了却把 task 标 passed：证据门禁必须红。

    修复前：check_evidence_integrity 只看「task 有没有对应 run」，不看 run 结论，
    这条用例会直接通过。
    """
    result = qa.check_evidence_integrity(_cases(), _mobile_spec(_mobile_task()), _run("FAIL"))
    assert result["status"] == "failed"
    assert "run-task-outcome-mismatch" in _types(result)


def test_run_passed_and_task_passed_stays_green(qa):
    result = qa.check_evidence_integrity(_cases(), _mobile_spec(_mobile_task()), _run("PASS"))
    assert result["status"] == "passed", result["findings"]


def test_task_failed_with_failed_run_is_not_flagged(qa):
    """task 标 failed、run 也 FAIL——两者一致，不该报 mismatch（防误报）。"""
    task = _mobile_task(status="failed")
    result = qa.check_evidence_integrity(_cases(), _mobile_spec(task), _run("FAIL"))
    assert "run-task-outcome-mismatch" not in _types(result)


# --- ② 证据必须绑定本轮 run ---------------------------------------------------


def test_manifest_bound_to_current_run_passes(qa, tmp_path):
    log = "run-TC-P0-001-tc-p0-001-1700000000.log"
    _write_mobile_manifest(tmp_path, run_log=log)
    result = qa.assert_mobile_evidence_data(
        _mobile_spec(_mobile_task()), tmp_path, latest_run_data=_run("PASS", log),
    )
    assert result["status"] == "passed", result["findings"]
    assert result["summary"]["runBoundManifests"] == 1


def test_manifest_from_stale_run_is_caught(qa, tmp_path):
    """目录里躺着一份上个月的证据，本轮根本没跑——不能算数。"""
    _write_mobile_manifest(tmp_path, run_log="run-TC-P0-001-old-1.log")
    result = qa.assert_mobile_evidence_data(
        _mobile_spec(_mobile_task()), tmp_path, latest_run_data=_run("PASS"),
    )
    assert result["status"] == "failed"
    assert "mobile-evidence-run-unbound" in _types(result)


def test_manifest_without_run_log_is_caught_when_run_known(qa, tmp_path):
    """历史 manifest 没有 runLog 字段：给了 latest-run 就必须要求绑定。"""
    _write_mobile_manifest(tmp_path)
    result = qa.assert_mobile_evidence_data(
        _mobile_spec(_mobile_task()), tmp_path, latest_run_data=_run("PASS"),
    )
    assert result["status"] == "failed"
    assert "mobile-evidence-run-unbound" in _types(result)


def test_binding_is_skipped_without_run_data(qa, tmp_path):
    """不传 latest-run 时保持旧行为（单测与手工检查仍可用）。"""
    _write_mobile_manifest(tmp_path)
    result = qa.assert_mobile_evidence_data(_mobile_spec(_mobile_task()), tmp_path)
    assert result["status"] == "passed", result["findings"]


# --- ③ evidenceRefs 不能靠「并集包含」过关 ------------------------------------


def test_ref_kind_never_captured_is_caught(qa, tmp_path):
    """refs 声明了网络抓包，但 manifest 只采了树/图/日志——断言等于没验证。"""
    _write_mobile_manifest(tmp_path)
    task = _mobile_task(refs={"viewTree": ["下单成功"], "network": ["下单成功"]})
    result = qa.assert_mobile_evidence_data(
        _mobile_spec(task), tmp_path, latest_run_data=_run("PASS"),
    )
    assert result["status"] == "failed"
    assert "mobile-evidence-ref-kind-not-captured" in _types(result)


def test_duplicated_ref_lists_are_reported_as_warning(qa, tmp_path):
    """两个 kind 的断言清单逐字相同：多半是复制粘贴，但截图与树可能真的都支撑同一句。

    所以这条是 warn：要出现在报告里，不单独阻断。
    """
    log = "run-TC-P0-001-tc-p0-001-1700000000.log"
    _write_mobile_manifest(tmp_path, run_log=log)
    same = ["下单成功"]
    task = _mobile_task(refs={"viewTree": same, "screenshot": same},
                        oracle={"ui": [{"assertion": "下单成功"}]})
    result = qa.assert_mobile_evidence_data(
        _mobile_spec(task), tmp_path, latest_run_data=_run("PASS", log),
    )
    findings = [f for f in result["findings"] if f["type"] == "mobile-evidence-refs-duplicated"]
    assert findings, result["findings"]
    assert all(f["severity"] == "warn" for f in findings)
    assert result["status"] == "passed"


# --- ④ spec-tasks 元数据漂移 --------------------------------------------------


def test_metadata_drift_is_caught(qa):
    """summary.total 说 8 条、tasks 实际 9 条：按 summary 算覆盖率就会算错。"""
    task = _mobile_task()
    spec = _mobile_spec(task)
    spec["summary"] = {"total": 8, "byLayer": {"e2e": 8},
                       "perCase": {"TC-P0-001": {"taskCount": 1}}}
    result = qa.assert_completion_data(_cases(), spec)
    assert result["status"] == "failed"
    assert "spec-tasks-metadata-mismatch" in _types(result)


def test_per_case_missing_registration_is_caught(qa):
    spec = _mobile_spec(_mobile_task())
    spec["summary"] = {"total": 1, "byLayer": {"e2e": 1}}
    spec["perCase"] = {}
    result = qa.assert_completion_data(_cases(), spec)
    assert "spec-tasks-metadata-mismatch" in _types(result)


# --- ⑤ 生成端与校验端的最小 task 数必须一致 ------------------------------------


def test_generated_mobile_tasks_pass_completion_without_override(qa, tmp_path):
    """生成器按 mobile 档写 1 条/用例，校验端在不传 override 时必须认它。

    修复前：CLI 默认 P0=8/P1=5，同一份产物换个人重跑就判红。
    """
    cases = _cases()
    spec = qa.generate_spec_tasks_data(
        cases, repo=tmp_path,
        ratio={"unit": 0, "integration": 0, "api": 0, "e2e": 1},
        min_specs_override="P0=1,P1=1,P2=1,P3=1",
        generation_profile="mobile-e2e",
    )
    for task in spec["tasks"]:
        task["executionStatus"] = "passed"
    result = qa.assert_completion_data(cases, spec)
    assert "case-under-min-spec-tasks" not in _types(result), result["findings"]


def test_desktop_spec_tasks_still_require_eight(qa):
    """没有 minSpecsByPriority、也不是 mobile 档：默认 8/5 继续生效。"""
    spec = _mobile_spec(_mobile_task())
    spec["tasks"][0]["platform"] = "web"
    spec["tasks"][0]["layer"] = "api"
    result = qa.assert_completion_data(_cases(), spec)
    assert "case-under-min-spec-tasks" in _types(result)


def test_explicit_override_wins_over_artifact(qa):
    spec = _mobile_spec(_mobile_task())
    spec["minSpecsByPriority"] = {"P0": 1, "P1": 1, "P2": 1, "P3": 1}
    result = qa.assert_completion_data(_cases(), spec, min_specs_override="P0=4")
    assert "case-under-min-spec-tasks" in _types(result)


# --- ⑥ 超时预算与超时记录 ------------------------------------------------------


def test_timeout_budget_accounts_for_retries(qa):
    """test-timeout 240s × (retries 1 + 1) = 480s，默认 420s 会砍在第二次重试中途。"""
    budget, notes = qa._mobile_e2e_timeout_budget(
        SimpleNamespace(timeout=None, retries=1), 240_000,
    )
    assert budget >= 480
    assert notes == []


def test_explicit_timeout_too_small_is_widened_with_a_note(qa):
    budget, notes = qa._mobile_e2e_timeout_budget(
        SimpleNamespace(timeout=420, retries=1), 240_000,
    )
    assert budget > 420
    assert notes and "放宽" in notes[0]


def test_timeout_leaves_a_run_record(qa, tmp_path, monkeypatch):
    """子进程超时被杀：必须留 sidecar（退出码 124），否则「跑挂了」等于「没跑过」。"""
    spec = tmp_path / "tests" / "e2e-mobile" / "app" / "tc-p0-001.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// test", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(qa, "find_npx", lambda: "npx")
    monkeypatch.setattr(qa.subprocess, "run", fake_run)

    args = SimpleNamespace(
        repo=str(tmp_path), spec=str(spec), project=None, case_id="TC-P0-001",
        task_id="SPEC-TC-P0-001-E2E-001", extra=[], timeout=5, device="",
        app_package="", evidence=False, retries=None, test_timeout=1000,
    )
    with pytest.raises(SystemExit) as exc:
        qa.run_mobile_e2e(args)
    assert exc.value.code == qa.MOBILE_E2E_TIMEOUT_EXIT_CODE

    metas = list((tmp_path / ".qa-agent" / "runs").glob("*.meta.json"))
    assert metas, "超时也必须写 sidecar"
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["exitCode"] == qa.MOBILE_E2E_TIMEOUT_EXIT_CODE


def test_evidence_failure_is_recorded_not_swallowed(qa, tmp_path, monkeypatch):
    """证据采集崩了：测试结论照走，但必须落一份错误记录给门禁和报告。"""
    spec = tmp_path / "tests" / "e2e-mobile" / "app" / "tc-p0-001.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// test", encoding="utf-8")

    class BoomDevice:
        def __init__(self, **kwargs):
            pass

        def save_evidence(self, *args, **kwargs):
            raise RuntimeError("adb 拒绝连接")

    fake_module = SimpleNamespace(MobileAdb=BoomDevice)

    monkeypatch.setattr(qa, "find_npx", lambda: "npx")
    monkeypatch.setattr(qa.subprocess, "run",
                        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="PASS", stderr=""))
    monkeypatch.setattr(qa, "_find_adb", lambda *a, **k: "adb")
    monkeypatch.setitem(sys.modules, "mobile_adb", fake_module)

    args = SimpleNamespace(
        repo=str(tmp_path), spec=str(spec), project=None, case_id="TC-P0-001",
        task_id="SPEC-TC-P0-001-E2E-001", extra=[], timeout=30, device="R58M12345",
        app_package="com.example.app", evidence=True, retries=None, test_timeout=1000,
    )
    qa.run_mobile_e2e(args)

    errors = list((tmp_path / ".qa-agent" / "runs").glob("*.evidence-error.json"))
    assert errors, "证据失败必须留痕"
    payload = json.loads(errors[0].read_text(encoding="utf-8"))
    assert "adb 拒绝连接" in payload["error"]
    assert payload["runLog"], "错误记录要指回本次 run 日志"
