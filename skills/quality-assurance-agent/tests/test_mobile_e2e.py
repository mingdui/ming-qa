#!/usr/bin/env python3
"""Android 真机 E2E 接入的检测逻辑回归。

设备无法在单测里真连，因此测的是解析与判定逻辑本身：

  ① `adb devices` 的四种现实输出——无设备 / unauthorized / 已授权 / 多设备。
     其中 unauthorized 最要紧：手机上的「允许 USB 调试」没点时 adb 就是这个状态，
     它是使用者首次运行最常卡住的地方。

  ② 项目是否存在 android-real task。这决定 doctor 要不要硬拦：
     一个纯 Web 项目不该因为「没插手机」而失败。

  ③ 多设备必须全部列出。静默选取第一台会让结果不可复现，且极难排查。
"""

from __future__ import annotations

import json


def _fake_adb(monkeypatch, qa, stdout: str, *, which: str | None = "/usr/bin/adb"):
    """把 adb 查找与执行都打桩，返回固定输出。"""
    monkeypatch.setattr(qa.shutil, "which", lambda name: which if name == "adb" else None)
    monkeypatch.setattr(
        qa, "run_cmd",
        lambda *a, **kw: {"exitCode": 0, "stdout": stdout, "stderr": ""},
    )


# --- ① adb devices 输出解析 ---


def test_no_device(qa, tmp_path, monkeypatch):
    _fake_adb(monkeypatch, qa, "List of devices attached\n\n")
    info = qa._detect_adb_devices(tmp_path)
    assert info["available"] is True
    assert info["devices"] == []
    assert info["authorized"] == []


def test_unauthorized_device_is_not_authorized(qa, tmp_path, monkeypatch):
    """手机上的「允许 USB 调试」没点时，adb 报 unauthorized——不得算作可用设备。"""
    _fake_adb(monkeypatch, qa, "List of devices attached\nR58M12345\tunauthorized\n")
    info = qa._detect_adb_devices(tmp_path)
    assert info["unauthorized"] == ["R58M12345"]
    assert info["authorized"] == []


def test_authorized_device_is_recognized(qa, tmp_path, monkeypatch):
    _fake_adb(monkeypatch, qa, "List of devices attached\nR58M12345\tdevice\n")
    info = qa._detect_adb_devices(tmp_path)
    assert info["authorized"] == ["R58M12345"]


def test_multiple_devices_are_all_listed(qa, tmp_path, monkeypatch):
    """多设备必须全部列出——静默选取第一台会让结果不可复现。"""
    _fake_adb(
        monkeypatch, qa,
        "List of devices attached\nR58M12345\tdevice\nemulator-5554\tdevice\nXYZ\tunauthorized\n",
    )
    info = qa._detect_adb_devices(tmp_path)
    assert info["authorized"] == ["R58M12345", "emulator-5554"]
    assert info["unauthorized"] == ["XYZ"]


def test_adb_missing_is_reported_not_raised(qa, tmp_path, monkeypatch):
    """缺 adb 时返回结构化结果，不抛异常——doctor 要能把它写成可读的检测项。"""
    _fake_adb(monkeypatch, qa, "", which=None)
    info = qa._detect_adb_devices(tmp_path)
    assert info["available"] is False
    assert info["adbPath"] == ""


# --- ② android-real task 识别 ---


def _write_spec_tasks(tmp_path, tasks):
    current = tmp_path / ".qa-agent" / "current"
    current.mkdir(parents=True, exist_ok=True)
    (current / "test-spec-tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )
    return tmp_path


def test_mobile_tasks_detected(qa, tmp_path):
    repo = _write_spec_tasks(tmp_path, [
        {"id": "TASK-001", "layer": "e2e", "platform": "web"},
        {"id": "TASK-002", "layer": "e2e", "platform": "android-real"},
    ])
    assert [t["id"] for t in qa._mobile_platform_tasks(repo)] == ["TASK-002"]


def test_missing_platform_defaults_to_web(qa, tmp_path):
    """存量 spec-task 没有 platform 字段，必须视为 web，不能被误判成移动端。"""
    repo = _write_spec_tasks(tmp_path, [{"id": "TASK-001", "layer": "e2e"}])
    assert qa._mobile_platform_tasks(repo) == []


def test_no_spec_tasks_file_is_empty(qa, tmp_path):
    assert qa._mobile_platform_tasks(tmp_path) == []


def test_malformed_spec_tasks_does_not_raise(qa, tmp_path):
    current = tmp_path / ".qa-agent" / "current"
    current.mkdir(parents=True, exist_ok=True)
    (current / "test-spec-tasks.json").write_text("{ not json", encoding="utf-8")
    assert qa._mobile_platform_tasks(tmp_path) == []


# --- ③ 条件化阻断判定 ---


def test_no_mobile_task_skips_check(qa, tmp_path):
    """纯 Web 项目不该因为「没插手机」而被拦住。"""
    _write_spec_tasks(tmp_path, [{"id": "TASK-001", "layer": "e2e", "platform": "web"}])
    assert qa.should_check_mobile(tmp_path) is False


def test_mobile_task_triggers_check(qa, tmp_path):
    _write_spec_tasks(tmp_path, [{"id": "TASK-002", "layer": "e2e", "platform": "android-real"}])
    assert qa.should_check_mobile(tmp_path) is True


def test_flag_forces_check_even_without_tasks(qa, tmp_path):
    """--require-mobile-e2e 显式要求时，即使没有 mobile task 也要检测。"""
    assert qa.should_check_mobile(tmp_path, require_flag=True) is True


def test_mobile_mcp_registration_detected(qa, tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mobile-mcp": {"command": "npx"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert qa._mobile_mcp_registered(tmp_path) is True


def test_mobile_mcp_absent_is_false(qa, tmp_path):
    assert qa._mobile_mcp_registered(tmp_path) is False
