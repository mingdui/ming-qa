"""报告「本次交付」区：不能把不存在/未实现的东西写成已交付。"""

from __future__ import annotations

from types import SimpleNamespace


def test_report_source_paths_exclude_readiness_check(qa, tmp_path):
    """readiness 依赖 report-freshness，不能再反过来当成报告的源（会循环变陈旧）。"""
    args = SimpleNamespace(
        cases=str(tmp_path / "test-cases.json"),
        run=None, review=None, code_review=None, risk_analysis=None,
        completion_check=None, readiness_check=str(tmp_path / "readiness-check.json"),
        spec_tasks=None,
    )
    labels = [label for label, _ in qa.collect_report_source_paths(args)]
    assert "readiness-check" not in labels


def test_executive_summary_separates_missing_scripts(qa):
    html = qa._render_executive_summary(
        {"module": "demo", "changes": "审查 2 个文件", "inScopeCases": "1 条", "legacy": "0 条"},
        [],
        [],
        test_scripts=["tests/e2e-mobile/demo/tc-p0-001.spec.ts"],
        product_src=["tests/e2e-mobile/demo/tc-p0-001.spec.ts"],
        missing_scripts=["tests/e2e-mobile/demo/tc-p1-008.spec.ts"],
    )

    assert "已映射但缺失的测试脚本（1 个）" in html
    assert "tc-p1-008.spec.ts" in html
    # 审查范围不等于产品源码：黑盒项目里它就是测试脚本
    assert "代码审查范围（1 个文件）" in html
    assert "涉及产品源码" not in html


def test_mobile_evidence_section_uses_report_styles(qa):
    """移动端区块必须挂 block/cards 样式，否则会退化成没有排版的纯文本。"""
    html = qa._render_mobile_evidence({
        "summary": {"mobileEvidence": {
            "mobileTasks": 9, "passedMobileTasks": 8, "matchedManifests": 8,
            "locatorTree": 8, "locatorScreenshot": 0,
            "totalDurationMs": 570748, "maxDurationMs": 152281,
        }}
    })
    assert 'class="block"' in html
    assert 'class="cards"' in html
    assert 'class="card"' in html
    assert ".cards {" in qa._REPORT_CSS
    assert ".card .value" in qa._REPORT_CSS


def test_executive_summary_without_missing_hides_block(qa):
    html = qa._render_executive_summary(
        {"module": "demo", "changes": "审查 1 个文件", "inScopeCases": "1 条", "legacy": "0 条"},
        [], [], test_scripts=["tests/a.spec.ts"],
    )
    assert "已映射但缺失的测试脚本" not in html
