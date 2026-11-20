#!/usr/bin/env python3
"""回归测试：代码审查渲染必须与 references/code-review.md 的产物契约一致。

历史缺陷：`_render_code_review` 是从别的项目整段搬来的（代码里那句
`# === 项目增强函数 (from project) ===`），字段假设与产物契约系统性不符：

  1. 契约里 `summary` 是一句话字符串，渲染函数却按 `{"blocking": N}` 字典读，
     传字符串直接 AttributeError 打挂整份报告渲染；
  2. finding 标题字段契约叫 `title`，渲染函数读 `summary`，合规产物渲染出空标题；
  3. 失败场景字段契约里是 `evidence`，渲染函数读 `failureScenario`。

这些都不会让 assert-code-review 失败（那个门禁对字段名做了多路回退），
所以只能在渲染这一步炸出来。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from qa_agent import _finding_title, _render_code_review


def _review(summary, findings):
    return {"status": "failed", "summary": summary, "findings": findings}


def test_render_accepts_string_summary_per_contract():
    """契约里 summary 是字符串——按契约产出时必须能正常渲染，不能崩。"""
    data = _review(
        "示例模块审查完成，发现 1 个 P1 问题。",
        [
            {
                "id": "CR-001",
                "severity": "P1",
                "file": "src/main/java/OpenBoxServiceImpl.java",
                "line": 698,
                "title": "概率表区间校验被禁用",
                "evidence": "第 698-712 行校验全部被注释",
                "recommendation": "恢复校验",
            }
        ],
    )

    html = _render_code_review(data, ["src/main/java/OpenBoxServiceImpl.java"])

    assert "代码审查" in html
    # 字符串 summary 时阻塞数从 findings 现算：1 条 P1
    assert "含 1 条阻塞" in html


def test_render_uses_title_field_for_finding_heading():
    """finding 标题字段是 title——渲染出来的标题不能是空的。"""
    data = _review(
        "结论",
        [
            {
                "id": "CR-007",
                "severity": "P2",
                "file": "a/b.java",
                "line": 3,
                "title": "入参未做容错",
                "evidence": "无 try/catch",
            }
        ],
    )

    html = _render_code_review(data, ["a/b.java"])

    assert "入参未做容错" in html


def test_render_falls_back_to_evidence_when_no_failure_scenario():
    """契约里没有 failureScenario 字段，应回退到 evidence 展示证据。"""
    data = _review(
        "结论",
        [
            {
                "id": "CR-008",
                "severity": "P2",
                "file": "a/b.java",
                "title": "问题",
                "evidence": "这是证据原文",
            }
        ],
    )

    html = _render_code_review(data, ["a/b.java"])

    assert "这是证据原文" in html


def test_render_still_accepts_legacy_dict_summary():
    """历史产物把 summary 写成 {"blocking": N} 字典，仍需兼容。"""
    data = _review({"blocking": 0}, [])
    data["findings"] = [
        {"id": "CR-009", "severity": "P3", "file": "a/b.java", "title": "小问题"}
    ]

    html = _render_code_review(data, ["a/b.java"])

    assert "均非阻塞" in html


def test_finding_title_prefers_title_then_falls_back():
    assert _finding_title({"title": "T", "summary": "S", "message": "M"}) == "T"
    assert _finding_title({"summary": "S", "message": "M"}) == "S"
    assert _finding_title({"message": "M"}) == "M"
    assert _finding_title({}) == ""


def test_render_hides_category_column_when_findings_have_none():
    """INFO 类 finding 常没有 category；空列比没列更让人以为缺数据。"""
    html = _render_code_review(_review("黑盒验收", [
        {"id": "CR-1", "severity": "INFO", "title": "无源码", "evidence": "APK"},
    ]))
    assert ">类型</th>" not in html
    assert "<td></td>" not in html


def test_render_keeps_category_column_when_present():
    html = _render_code_review(_review("示例", [
        {"id": "CR-1", "severity": "P1", "title": "问题", "category": "data-consistency"},
    ]))
    assert ">类型</th>" in html
