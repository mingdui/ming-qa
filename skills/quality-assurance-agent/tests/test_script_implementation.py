"""测试脚本实现真实性：映射到文件 ≠ 实现了测试。"""
from __future__ import annotations

from pathlib import Path


def _spec_tasks(target_files):
    """构造 N 个 task 映射到同一个 targetFile。"""
    tasks = []
    for i, tf in enumerate(target_files):
        for _ in range(3):  # 每个文件映射 3 个 task
            tasks.append({
                "id": f"SPEC-{i}-{_}-API-001",
                "sourceCaseId": f"TC-P0-{i:03d}",
                "targetFile": tf,
                "layer": "api",
            })
    return {"tasks": tasks}


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_blocked_task_does_not_require_target_file(qa, tmp_path):
    """产品缺口导致的 blocked task 不该卡在实现门禁上。"""
    spec = {"tasks": [{
        "id": "SPEC-TC-P1-008-E2E-001",
        "sourceCaseId": "TC-P1-008",
        "targetFile": "tests/e2e-mobile/demo/tc-p1-008.spec.ts",
        "layer": "e2e",
        "implementationStatus": "blocked",
        "executionStatus": "blocked",
    }]}
    result = qa.assert_script_implementation_data(spec, tmp_path)
    assert result["status"] == "passed"
    assert result["summary"]["blockedTasks"] == 1


def test_target_file_missing_is_not_implemented(qa, tmp_path):
    spec = _spec_tasks(["src/test/java/qa/OpenBoxTest.java"])
    result = qa.assert_script_implementation_data(spec, tmp_path)
    assert result["status"] == "failed"
    types = [f["type"] for f in result["findings"]]
    assert "target-file-missing" in types


def test_placeholder_stub_is_not_implemented(qa, tmp_path):
    stub = tmp_path / "tests" / "api" / "tc.sh"
    _write(stub, '#!/bin/bash\necho "BLOCKED: ..."\nexit 0\n')
    spec = _spec_tasks(["tests/api/tc.sh"])
    result = qa.assert_script_implementation_data(spec, tmp_path)
    types = [f["type"] for f in result["findings"]]
    assert "placeholder-stub" in types


def test_test_method_count_less_than_task_count_fails(qa, tmp_path):
    """54 个 task 映射到只有 7 个 @Test 的文件 → 映射未实现。"""
    java = tmp_path / "src/test/java/qa/OpenBoxTest.java"
    _write(java, "class OpenBoxTest {\n  @Test void t1() {}\n  @Test void t2() {}\n}\n")  # 2 个 @Test
    spec = {"tasks": []}
    for i in range(5):  # 5 个 task 映射到这个文件
        spec["tasks"].append({"id": f"S-{i}", "sourceCaseId": f"C-{i}", "targetFile": "src/test/java/qa/OpenBoxTest.java", "layer": "unit"})
    result = qa.assert_script_implementation_data(spec, tmp_path)
    types = [f["type"] for f in result["findings"]]
    assert "mapping-not-implemented" in types


def test_test_method_count_sufficient_passes(qa, tmp_path):
    java = tmp_path / "src/test/java/qa/OpenBoxTest.java"
    _write(java, "class OpenBoxTest {\n  @Test void t1() {}\n  @Test void t2() {}\n  @Test void t3() {}\n}\n")  # 3 个 @Test
    spec = {"tasks": []}
    for i in range(3):  # 3 个 task，方法数 >= task 数
        spec["tasks"].append({"id": f"S-{i}", "sourceCaseId": f"C-{i}", "targetFile": "src/test/java/qa/OpenBoxTest.java", "layer": "unit"})
    result = qa.assert_script_implementation_data(spec, tmp_path)
    assert result["status"] == "passed"


# --- agent 直驱形态（无独立脚本）---
#
# web 侧的 Playwright MCP 直驱与移动端的 mobile-mcp 直驱都属于这类：没有脚本文件可校验。
# 若不豁免，这个门禁对它们永远无法通过——而 completion gate 的 evidence 要求
# 才是它们的正确约束点。


def test_agent_driven_mobile_target_file_is_skipped(qa, tmp_path):
    """targetFile 以 @ 开头 = agent 直驱，不判「文件不存在」。"""
    spec = {"tasks": [
        {"id": "T1", "sourceCaseId": "C1", "targetFile": "@agent:mobile-mcp", "layer": "e2e"},
    ]}
    result = qa.assert_script_implementation_data(spec, tmp_path)
    assert result["status"] == "passed", result["findings"]


def test_agent_driven_playwright_target_file_is_skipped(qa, tmp_path):
    spec = {"tasks": [
        {"id": "T1", "sourceCaseId": "C1", "targetFile": "@agent:playwright-mcp", "layer": "e2e"},
    ]}
    assert qa.assert_script_implementation_data(spec, tmp_path)["status"] == "passed"


def test_real_missing_file_still_fails(qa, tmp_path):
    """豁免只针对 @ 前缀——真实路径缺失仍要报错，不能把门禁放水。"""
    spec = {"tasks": [
        {"id": "T1", "sourceCaseId": "C1",
         "targetFile": "tests/e2e-mobile/app/tc-p0-001.spec.ts", "layer": "e2e"},
    ]}
    result = qa.assert_script_implementation_data(spec, tmp_path)
    assert result["status"] == "failed"
    assert result["findings"][0]["type"] == "target-file-missing"


def test_agent_driven_tasks_are_excluded_from_file_count(qa, tmp_path):
    """同一份 spec-tasks 里两种形态并存：只统计真实文件，直驱 task 不计入。"""
    _write(tmp_path / "tests" / "api" / "tc.sh", "#!/bin/bash\ncurl -s \"$URL\"\nexit 0\n")
    spec = {"tasks": [
        {"id": "T1", "sourceCaseId": "C1", "targetFile": "@agent:mobile-mcp", "layer": "e2e"},
        {"id": "T2", "sourceCaseId": "C2", "targetFile": "tests/api/tc.sh", "layer": "api"},
    ]}
    result = qa.assert_script_implementation_data(spec, tmp_path)
    assert result["status"] == "passed", result["findings"]
    assert result["summary"]["files"] == 1, "agent 直驱的 task 不该计入文件数"
