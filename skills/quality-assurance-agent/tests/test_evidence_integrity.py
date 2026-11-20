"""测试证据完整性门禁 check_evidence_integrity"""
import pytest


def _cases():
    return {"cases": [{"id": "TC-P0-001", "priority": "P0", "status": "passed"}]}


def _spec_tasks():
    return {"tasks": [{"id": "SPEC-TC-P0-001-API-001", "sourceCaseId": "TC-P0-001"}]}


def _run(exit_summary=None):
    return {
        "summary": exit_summary or {"totalCases": 1, "casesPassed": 1},
        "cases": [{"caseId": "TC-P0-001", "finalOutcome": "PASS"}],
        "unmatchedLogs": [],
    }


def test_passes_when_evidence_consistent(qa):
    result = qa.check_evidence_integrity(_cases(), _spec_tasks(), _run())
    assert result["status"] == "passed", result["findings"]


def test_fails_when_run_summary_empty(qa):
    run = _run(exit_summary={"totalCases": 0, "casesPassed": 0})
    result = qa.check_evidence_integrity(_cases(), _spec_tasks(), run)
    assert result["status"] == "failed"
    types = [f["type"] for f in result["findings"]]
    assert "run-summary-empty" in types


def test_fails_when_unmatched_logs_present(qa):
    run = _run()
    run["unmatchedLogs"] = ["run-tc-p0-001-1786635844.log"]
    result = qa.check_evidence_integrity(_cases(), _spec_tasks(), run)
    assert result["status"] == "failed"
    types = [f["type"] for f in result["findings"]]
    assert "unmatched-run-logs" in types


def test_fails_when_task_has_no_run_evidence(qa):
    # spec-task 引用 TC-P0-002，但 run 里只有 TC-P0-001
    spec_tasks = {"tasks": [{"id": "SPEC-TC-P0-002-API-001", "sourceCaseId": "TC-P0-002"}]}
    result = qa.check_evidence_integrity(_cases(), spec_tasks, _run())
    assert result["status"] == "failed"
    types = [f["type"] for f in result["findings"]]
    assert "task-without-run-evidence" in types


def test_blocked_task_does_not_require_run_evidence(qa):
    """blocked 任务永远不会产生 run；completion 允许它时，证据门禁不能二次判死。"""
    spec_tasks = {"tasks": [
        {"id": "SPEC-TC-P0-001-API-001", "sourceCaseId": "TC-P0-001"},
        {"id": "SPEC-TC-P1-008-E2E-008", "sourceCaseId": "TC-P1-008",
         "executionStatus": "blocked", "implementationStatus": "blocked"},
    ]}
    result = qa.check_evidence_integrity(_cases(), spec_tasks, _run())
    assert result["status"] == "passed", result["findings"]


def test_passed_task_still_requires_run_evidence(qa):
    """豁免只针对 blocked/skipped/deferred；passed 缺 run 仍要失败。"""
    spec_tasks = {"tasks": [
        {"id": "SPEC-TC-P0-001-API-001", "sourceCaseId": "TC-P0-001"},
        {"id": "SPEC-TC-P0-002-E2E-001", "sourceCaseId": "TC-P0-002",
         "executionStatus": "passed"},
    ]}
    result = qa.check_evidence_integrity(_cases(), spec_tasks, _run())
    assert result["status"] == "failed"
    assert "task-without-run-evidence" in [f["type"] for f in result["findings"]]


def test_fails_when_code_review_scope_is_string(qa):
    code_review = {"scope": "demo-backend/src"}
    result = qa.check_evidence_integrity(_cases(), _spec_tasks(), _run(), code_review)
    assert result["status"] == "failed"
    types = [f["type"] for f in result["findings"]]
    assert "invalid-code-review-scope" in types


def test_accepts_object_scope(qa):
    code_review = {"scope": {"files": ["a.java"], "description": "x"}}
    result = qa.check_evidence_integrity(_cases(), _spec_tasks(), _run(), code_review)
    assert result["status"] == "passed"


def test_readiness_incomplete_when_evidence_empty(qa):
    completion = {"status": "passed", "decision": "complete"}
    code_review = {"status": "passed", "findings": []}
    evidence = {
        "status": "failed",
        "summary": {"runCasesTotal": 0},
        "findings": [{"type": "run-summary-empty", "severity": "fail", "message": "空"}],
    }
    result = qa.assert_readiness_data(
        completion, code_review,
        report_freshness_data={"status": "passed", "decision": "Ready"},
        evidence_integrity_data=evidence,
    )
    assert result["decision"] == "Incomplete", result["decision"]
