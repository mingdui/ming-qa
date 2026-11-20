"""测试 code-review verdict 的行动语言呈现。"""
from __future__ import annotations


def test_zh_verdict_action_language(qa):
    assert qa._zh_verdict("CONFIRMED") == "待修复"
    assert qa._zh_verdict("PLAUSIBLE") == "待确认"
    assert qa._zh_verdict("REJECTED") == "已排除"
    assert qa._zh_verdict("UNKNOWN") == "UNKNOWN"  # 未知透传


def test_verdict_badge_color_by_action(qa):
    assert 'class="b danger"' in qa._verdict_badge("CONFIRMED")
    assert "待修复" in qa._verdict_badge("CONFIRMED")

    assert 'class="b warn"' in qa._verdict_badge("PLAUSIBLE")
    assert "待确认" in qa._verdict_badge("PLAUSIBLE")

    assert 'class="b mute"' in qa._verdict_badge("REJECTED")
    assert "已排除" in qa._verdict_badge("REJECTED")


def test_verdict_badge_empty_renders_dash_not_question_mark(qa):
    html = qa._verdict_badge("")
    assert 'class="b mute"' in html
    assert "—" in html
    assert "?" not in html


def test_verdict_badge_unknown_falls_back(qa):
    html = qa._verdict_badge("weird")
    assert 'class="b info"' in html
    assert "weird" in html
