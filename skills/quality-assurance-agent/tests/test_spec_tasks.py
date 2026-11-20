"""Task 3: Spec-task generation and oracle mapping."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _minimal_cases(cases: list[dict]) -> dict:
    return {"status": "confirmed", "version": "1.0", "cases": cases}


def _minimal_case(case_id="TC-P0-001", priority="P0", **extra) -> dict:
    base = {
        "id": case_id,
        "priority": priority,
        "title": "下单 API",
        "module": "order",
        "type": "business",
        "layer": "api",
        "automation": "automated",
        "status": "confirmed",
        "businessActor": "普通用户",
        "operationPath": "POST /order/create",
        "preconditions": ["余额充足"],
        "steps": ["发起下单"],
        "expected": ["下单成功，返回奖品信息"],
        "businessAssertions": ["响应 code=200", "余额扣减正确"],
        "traceability": ["RISK-P0-001"],
        "data": {"productId": "225", "tableHint": "t_product_prize"},
    }
    base.update(extra)
    return base


def test_mobile_e2e_profile_generates_mobilewright_specs(qa, tmp_path):
    """无源码移动端：只生成 e2e，targetFile 指向 Mobilewright spec，命令走 run-mobile-e2e。"""
    cases = _minimal_cases([_minimal_case()])
    result = qa.generate_spec_tasks_data(
        cases, repo=tmp_path,
        ratio={"unit": 0, "integration": 0, "api": 0, "e2e": 1},
        min_specs_override="P0=1,P1=1,P2=1,P3=1",
        generation_profile="mobile-e2e",
    )
    assert result["tasks"], "mobile-e2e profile should still produce tasks"
    for task in result["tasks"]:
        assert task["layer"] == "e2e"
        assert task["platform"] == "android-real"
        assert task["targetFile"].startswith("tests/e2e-mobile/")
        assert task["targetFile"].endswith(".spec.ts")
        assert "run-mobile-e2e" in task["command"]
        assert task["id"] in task["command"]
        # 包名未知：命令里没有 --app-package，必须由实现阶段补
        assert "--app-package" not in task["command"]
        assert task["needsManualCommand"] is True


def test_mobile_e2e_profile_uses_app_package_when_known(qa, tmp_path):
    cases = _minimal_cases([_minimal_case(data={"appPackage": "com.example.app"})])
    result = qa.generate_spec_tasks_data(
        cases, repo=tmp_path,
        ratio={"unit": 0, "integration": 0, "api": 0, "e2e": 1},
        min_specs_override="P0=1,P1=1,P2=1,P3=1",
        generation_profile="mobile-e2e",
    )
    for task in result["tasks"]:
        assert task["appPackage"] == "com.example.app"
        assert "--app-package com.example.app" in task["command"]
        assert "needsManualCommand" not in task


def test_spec_task_oracle_is_structured_object_not_string(qa, tmp_path):
    write(
        tmp_path / "demo-backend" / "pom.xml",
        "<project><artifactId>demo-backend</artifactId></project>",
    )
    cases = _minimal_cases([_minimal_case()])
    result = qa.generate_spec_tasks_data(cases, repo=tmp_path)

    for task in result["tasks"]:
        for oracle_list in task["oracle"].values():
            for item in oracle_list:
                assert isinstance(item, dict), (
                    f"oracle item must be a dict, got {type(item).__name__}: {item!r}"
                )
                assert "assertion" in item, f"oracle item missing 'assertion': {item!r}"
                assert "type" in item, f"oracle item missing 'type': {item!r}"


def test_spec_task_assertions_sourced_from_business_assertions(qa, tmp_path):
    write(
        tmp_path / "demo-backend" / "pom.xml",
        "<project><artifactId>demo-backend</artifactId></project>",
    )
    case = _minimal_case(
        businessAssertions=["响应 code=200", "balance_change = totalDeducted"],
        expected=["下单成功"],
    )
    cases = _minimal_cases([case])
    result = qa.generate_spec_tasks_data(cases, repo=tmp_path)

    for task in result["tasks"]:
        flat = json.dumps(task["assertions"])
        assert "balance_change = totalDeducted" in flat, (
            f"businessAssertion not in task assertions: {task['assertions']}"
        )
        assert flat.count("balance_change = totalDeducted") == 1, (
            "businessAssertion duplicated in assertions"
        )


def test_spec_task_root_has_generation_metadata(qa, tmp_path):
    write(
        tmp_path / "demo-backend" / "pom.xml",
        "<project><artifactId>demo-backend</artifactId></project>",
    )
    cases = _minimal_cases([_minimal_case()])
    result = qa.generate_spec_tasks_data(cases, repo=tmp_path)

    assert "generationProfile" in result, "missing generationProfile"
    assert "minSpecsByPriority" in result, "missing minSpecsByPriority"
    assert "targetRatio" in result, "missing targetRatio"


def test_requires_e2e_case_forces_e2e_task_when_ratio_excludes_it(qa, tmp_path):
    """即使配比把 e2e 挤成 0，requiresE2E=True 的用例也必须强制补一个 e2e task。"""
    write(
        tmp_path / "demo-backend" / "pom.xml",
        "<project><artifactId>demo-backend</artifactId></project>",
    )
    case = _minimal_case(requiresE2E=True)
    cases = _minimal_cases([case])
    result = qa.generate_spec_tasks_data(
        cases,
        repo=tmp_path,
        min_specs_override="P0=1,P1=1,P2=1",
        ratio={"unit": 1, "integration": 0, "api": 0, "e2e": 0},
    )

    layers = [t["layer"] for t in result["tasks"]]
    assert "e2e" in layers, "requiresE2E=True 必须强制产出至少一个 e2e task，即使配比里 e2e=0"


def test_default_pyramid_generates_unit_tasks(qa, tmp_path):
    """默认配比（不再有 acceptance-mode 开关）必须生成 unit task，而不是只出 api。"""
    write(
        tmp_path / "demo-backend" / "pom.xml",
        "<project><artifactId>demo-backend</artifactId></project>",
    )
    cases = _minimal_cases([_minimal_case(priority="P0")])
    result = qa.generate_spec_tasks_data(cases, repo=tmp_path)

    layers = [t["layer"] for t in result["tasks"]]
    assert "unit" in layers, "默认金字塔必须包含 unit task"
    assert result["generationProfile"] == "development"


def test_spec_task_db_oracle_contains_table_hint_from_case_data(qa, tmp_path):
    write(
        tmp_path / "demo-backend" / "pom.xml",
        "<project><artifactId>demo-backend</artifactId></project>",
    )
    case = _minimal_case(data={"productId": "225", "tableHint": "t_product_prize"})
    cases = _minimal_cases([case])
    result = qa.generate_spec_tasks_data(cases, repo=tmp_path)

    db_assertions = []
    for task in result["tasks"]:
        db_assertions.extend(task["oracle"].get("db", []))

    flat = json.dumps(db_assertions)
    assert "t_product_prize" in flat or "225" in flat, (
        f"case data (tableHint/productId) not reflected in DB oracle: {db_assertions}"
    )


def test_oracle_mapping_gate_fails_when_p0_risk_not_covered(qa, tmp_path):
    risk_data = {
        "risks": [
            {
                "id": "RISK-P0-001",
                "priority": "P0",
                "category": "money-reward-settlement",
                "requiredAssertions": ["余额扣减正确"],
                "requiresE2E": False,
            }
        ]
    }
    spec_tasks = {
        "tasks": [
            {
                "id": "SPEC-TC-P0-001-API-001",
                "sourceCaseId": "TC-P0-001",
                "assertions": ["code=200"],
                "oracle": {"api": [{"type": "api", "assertion": "code=200"}], "db": [], "ui": [], "sideEffects": [], "negativeAssertions": []},
                "traceability": [],
            }
        ]
    }

    result = qa.assert_oracle_mapping_data(risk_data, spec_tasks)

    assert result["status"] == "failed", f"expected failed, got: {result['status']}"
    finding_ids = [f.get("riskId") for f in result.get("findings", [])]
    assert "RISK-P0-001" in finding_ids


def test_oracle_mapping_gate_passes_when_all_p0_p1_risks_mapped(qa, tmp_path):
    risk_data = {
        "risks": [
            {
                "id": "RISK-P0-001",
                "priority": "P0",
                "category": "money-reward-settlement",
                "requiredAssertions": ["余额扣减正确"],
                "requiresE2E": False,
            }
        ]
    }
    spec_tasks = {
        "tasks": [
            {
                "id": "SPEC-TC-P0-001-API-001",
                "sourceCaseId": "TC-P0-001",
                "assertions": ["余额扣减正确"],
                "oracle": {"api": [], "db": [{"type": "db", "assertion": "余额扣减正确", "sourceRiskId": "RISK-P0-001"}], "ui": [], "sideEffects": [], "negativeAssertions": []},
                "traceability": ["RISK-P0-001"],
            }
        ]
    }

    result = qa.assert_oracle_mapping_data(risk_data, spec_tasks)

    assert result["status"] == "passed", f"expected passed, got: {result['status']}\nfindings: {result.get('findings')}"
