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
    """把 adb 查找与执行都打桩，返回固定输出。

    同时隔离真实环境：本机可能已把 adb 装到 ~/.ming-qa/platform-tools，
    不隔离的话「adb 缺失」类用例会随开发机状态时红时绿。
    """
    monkeypatch.setattr(qa.shutil, "which", lambda name: which if name == "adb" else None)
    monkeypatch.setattr(
        qa, "run_cmd",
        lambda *a, **kw: {"exitCode": 0, "stdout": stdout, "stderr": ""},
    )
    monkeypatch.setattr(
        qa, "platform_tools_dir", lambda: qa.Path("/nonexistent/ming-qa-platform-tools")
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


def test_adb_runs_outside_repo_to_avoid_log_pollution(qa, tmp_path, monkeypatch):
    """adb 会在 cwd 写 adb.log——不能在目标 repo 下跑，否则污染用户工作区。"""
    seen = {}

    def fake_run(cmd, cwd, timeout=15, **kw):
        seen["cwd"] = cwd
        return {"exitCode": 0, "stdout": "List of devices attached\n", "stderr": ""}

    monkeypatch.setattr(qa.shutil, "which", lambda n: "/usr/bin/adb" if n == "adb" else None)
    monkeypatch.setattr(qa, "run_cmd", fake_run)
    qa._detect_adb_devices(tmp_path)
    assert str(seen["cwd"]) != str(tmp_path), "adb 不该在目标 repo 下运行（会留下 adb.log）"


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


# --- ④ 安装流程：mobile-mcp 注册 ---


def test_register_mobile_mcp_creates_entry(qa, tmp_path):
    result = qa._register_mobile_mcp(tmp_path, platform="linux")
    assert result["status"] == "created"
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "mobile-mcp" in data["mcpServers"]


def test_register_mobile_mcp_preserves_existing_servers(qa, tmp_path):
    """注册 mobile-mcp 不能弄丢用户已有的 MCP server。"""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mysql_mcp": {"command": "npx"}}}), encoding="utf-8",
    )
    qa._register_mobile_mcp(tmp_path, platform="linux")
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "mysql_mcp" in data["mcpServers"], "原有 server 被覆盖了"
    assert "mobile-mcp" in data["mcpServers"]


def test_register_mobile_mcp_is_idempotent(qa, tmp_path):
    qa._register_mobile_mcp(tmp_path, platform="linux")
    assert qa._register_mobile_mcp(tmp_path, platform="linux")["status"] == "skipped"


def test_register_mobile_mcp_windows_uses_cmd_wrapper(qa, tmp_path):
    """Windows 上 MCP server 必须以 cmd /c 启动——对照 .mcp.json 里 playwright-test 的写法。"""
    qa._register_mobile_mcp(tmp_path, platform="windows")
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    entry = data["mcpServers"]["mobile-mcp"]
    assert entry["command"] == "cmd"
    assert entry["args"][:2] == ["/c", "npx"]


def test_register_mobile_mcp_survives_malformed_file(qa, tmp_path):
    """.mcp.json 损坏时要重建，不能抛异常把安装流程打断。"""
    (tmp_path / ".mcp.json").write_text("{ broken", encoding="utf-8")
    assert qa._register_mobile_mcp(tmp_path, platform="linux")["status"] == "created"
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "mobile-mcp" in data["mcpServers"]


# --- ⑤ adb 安装命令的平台选择 ---


def test_adb_install_command_windows_uses_winget(qa, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda n: "/x/winget" if n == "winget" else None)
    cmd = qa._adb_install_command("windows")
    assert cmd is not None and cmd[0] == "winget"


def test_adb_install_command_macos_uses_brew(qa, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda n: "/x/brew" if n == "brew" else None)
    cmd = qa._adb_install_command("darwin")
    assert cmd is not None and cmd[0] == "brew"


def test_adb_install_command_none_without_package_manager(qa, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda n: None)
    assert qa._adb_install_command("windows") is None


# --- ⑥ 人工步骤必须报出来 ---


def test_manual_steps_reported_when_device_unauthorized(qa, tmp_path, monkeypatch):
    """USB 授权无法自动化——adb 可用但设备未授权时，必须作为人工步骤报出来。"""
    _fake_adb(monkeypatch, qa, "List of devices attached\nR58M12345\tunauthorized\n")
    steps = qa._adb_manual_steps(tmp_path)
    assert steps, "未授权时不能返回空的人工步骤"
    assert any("允许" in s for s in steps)


def test_no_manual_steps_when_device_authorized(qa, tmp_path, monkeypatch):
    _fake_adb(monkeypatch, qa, "List of devices attached\nR58M12345\tdevice\n")
    assert qa._adb_manual_steps(tmp_path) == []


def test_no_manual_steps_when_adb_missing(qa, tmp_path, monkeypatch):
    """adb 没装时由安装步骤负责，人工步骤列表不重复报——避免噪音。"""
    _fake_adb(monkeypatch, qa, "", which=None)
    assert qa._adb_manual_steps(tmp_path) == []


# --- ⑦ dry-run 不得改动任何文件 ---


def test_dry_run_does_not_write_mcp(qa, tmp_path):
    """--dry-run 只报告不落盘——否则「先看看会做什么」就成了空话。"""
    result = qa.install_mobile_e2e_in_repo(tmp_path, dry_run=True, with_adb=False)
    assert not (tmp_path / ".mcp.json").exists()
    assert any(s["status"] == "dry-run" for s in result["steps"])


# --- ⑧ 无包管理器时的下载回退（Windows 10 LTSC 没有 winget 即此情形）---


def test_find_adb_falls_back_to_managed_dir(qa, tmp_path, monkeypatch):
    """下载到用户目录的 adb 必须能被认出来——否则装完 doctor 仍报缺失。"""
    monkeypatch.setattr(qa.shutil, "which", lambda n: None)
    monkeypatch.setattr(qa, "platform_tools_dir", lambda: tmp_path / "platform-tools")
    (tmp_path / "platform-tools").mkdir(parents=True)
    (tmp_path / "platform-tools" / ("adb.exe" if qa.os.name == "nt" else "adb")).write_text("")
    assert qa._find_adb() is not None


def test_find_adb_prefers_path_over_managed_dir(qa, tmp_path, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(qa, "platform_tools_dir", lambda: tmp_path / "platform-tools")
    assert qa._find_adb() == "/usr/bin/adb"


def test_find_adb_returns_none_when_nowhere(qa, tmp_path, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda n: None)
    monkeypatch.setattr(qa, "platform_tools_dir", lambda: tmp_path / "nope")
    assert qa._find_adb() is None


def test_download_platform_tools_dry_run_does_not_write(qa, tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "platform_tools_dir", lambda: tmp_path / "platform-tools")
    result = qa._download_platform_tools(tmp_path, platform="windows", dry_run=True)
    assert result["status"] == "dry-run"
    assert not (tmp_path / "platform-tools").exists()


def test_download_platform_tools_unsupported_platform(qa, tmp_path):
    """不支持的平台要落到 manual 而不是抛异常——安装流程不能断。"""
    result = qa._download_platform_tools(tmp_path, platform="plan9")
    assert result["status"] == "manual"


def test_platform_tools_urls_are_https_google(qa):
    """下载源必须是 Google 官方 HTTPS 地址，不接受任意 URL。"""
    for plat, url in qa.PLATFORM_TOOLS_URLS.items():
        assert url.startswith("https://dl.google.com/android/repository/"), f"{plat} 的下载源不是官方地址"
