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
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


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


# --- ⑨ 用户 PATH 处理 ---


def test_path_with_dir_added_appends(qa):
    assert qa._path_with_dir_added("C:\\a;C:\\b", "C:\\c") == "C:\\a;C:\\b;C:\\c"


def test_path_with_dir_added_is_idempotent(qa):
    """已在 PATH 里的目录不重复追加。"""
    current = "C:\\a;C:\\b"
    assert qa._path_with_dir_added(current, "C:\\b") == current


def test_path_with_dir_added_handles_empty(qa):
    assert qa._path_with_dir_added("", "C:\\a") == "C:\\a"


def test_path_with_dir_added_drops_empty_segments(qa):
    """PATH 里常见的空条目（尾部分号造成）不该被放大。"""
    assert qa._path_with_dir_added("C:\\a;;C:\\b;", "C:\\c") == "C:\\a;C:\\b;C:\\c"


def test_add_to_user_path_manual_on_non_windows(qa, tmp_path):
    """非 Windows 平台不去改 shell 启动文件——动别人的 dotfile 侵入性太大。"""
    assert qa._add_to_user_path(tmp_path, platform="linux")["status"] == "manual"


# --- ⑩ 输出必须提示 PATH（使用者实际踩过的坑）---


def _fake_managed_adb(monkeypatch, qa, tmp_path):
    """adb 只存在于 ming-qa 自管目录，不在系统 PATH。"""
    monkeypatch.setattr(qa.shutil, "which", lambda n: None)
    monkeypatch.setattr(qa, "platform_tools_dir", lambda: tmp_path / "platform-tools")
    (tmp_path / "platform-tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "platform-tools" / ("adb.exe" if qa.os.name == "nt" else "adb")).write_text("")
    monkeypatch.setattr(
        qa, "run_cmd",
        lambda *a, **kw: {"exitCode": 0, "stdout": "List of devices attached\n", "stderr": ""},
    )


def test_notes_warn_when_adb_not_on_path(qa, tmp_path, monkeypatch):
    """adb 在用户目录但不在 PATH 时必须提示——第三方工具（mobile-mcp）只认 PATH。"""
    _fake_managed_adb(monkeypatch, qa, tmp_path)
    result = qa.install_mobile_e2e_in_repo(tmp_path, with_adb=False)
    assert any("PATH" in n for n in result["notes"]), "没有提示 PATH，使用者会像之前那样卡住"


def test_no_path_note_when_adb_on_path(qa, tmp_path, monkeypatch):
    """adb 已在 PATH 时不该刷无意义的提示。"""
    monkeypatch.setattr(qa.shutil, "which", lambda n: "/usr/bin/adb" if n == "adb" else None)
    monkeypatch.setattr(qa, "run_cmd", lambda *a, **kw: {"exitCode": 0, "stdout": "", "stderr": ""})
    result = qa.install_mobile_e2e_in_repo(tmp_path, with_adb=False)
    assert not any("PATH" in n for n in result["notes"])


def test_notes_warn_about_stale_daemon_after_path_change(qa, tmp_path, monkeypatch):
    """配完 PATH 要提醒 daemon 缓存。

    mobilecli 的 daemon 在 adb 不可见时启动，就会一直记着「零设备」且不报错——
    光修 PATH 不重启 daemon 依然列不出设备。这个坑真实发生过。

    断言用 "daemon stop" 而非 "daemon"：tmp_path 的目录名取自测试函数名，
    用泛词会被自己的路径污染，测试就永远通过了（假阳性）。
    """
    _fake_managed_adb(monkeypatch, qa, tmp_path)
    monkeypatch.setattr(qa, "_add_to_user_path",
                        lambda *a, **kw: {"status": "passed", "path": "x"})
    result = qa.install_mobile_e2e_in_repo(tmp_path, with_adb=False, add_to_path=True)
    assert any("daemon stop" in n for n in result["notes"]), "没有提醒 daemon 缓存"


def test_no_daemon_note_without_add_to_path(qa, tmp_path, monkeypatch):
    """没改 PATH 就不必提 daemon——避免噪音。"""
    _fake_managed_adb(monkeypatch, qa, tmp_path)
    result = qa.install_mobile_e2e_in_repo(tmp_path, with_adb=False)
    assert not any("daemon stop" in n for n in result["notes"])


def test_adb_step_reports_path(qa, tmp_path, monkeypatch):
    """检测到 adb 时步骤要带上落盘路径——只报一句状态而不说它在哪，使用者找不到它。"""
    _fake_managed_adb(monkeypatch, qa, tmp_path)
    result = qa.install_mobile_e2e_in_repo(tmp_path, with_adb=True)
    adb_step = next(s for s in result["steps"] if s["step"] == "adb")
    assert adb_step.get("detail") or adb_step.get("path")


# --- ⑪ 平台枚举：Android 模拟器与 iOS 必须被识别，不能被当成 web ---


def test_android_emulator_is_a_mobile_platform(qa, tmp_path):
    repo = _write_spec_tasks(tmp_path, [
        {"id": "T1", "layer": "e2e", "platform": "android-emulator"},
    ])
    assert [t["id"] for t in qa._mobile_platform_tasks(repo)] == ["T1"]


def test_ios_platform_is_recognized_not_silently_web(qa, tmp_path):
    """声明了 iOS 就必须进入移动端分支，否则会被静默当成 web 放行。"""
    repo = _write_spec_tasks(tmp_path, [
        {"id": "T1", "layer": "e2e", "platform": "ios-sim"},
    ])
    assert [t["id"] for t in qa._mobile_platform_tasks(repo)] == ["T1"]


def test_doctor_requires_android_only_for_android_tasks(qa):
    req = qa._mobile_doctor_requirements([{"platform": "android-real"}], False)
    assert req["android"] is True
    assert req["ios"] is False


def test_doctor_does_not_demand_adb_for_ios_only_tasks(qa):
    """只声明 iOS 的项目不该被要求装 adb——那是另一条工具链。"""
    req = qa._mobile_doctor_requirements([{"platform": "ios-real"}], False)
    assert req["android"] is False
    assert req["ios"] is True


def test_doctor_flag_without_tasks_checks_android(qa):
    """--require-mobile-e2e 没有 task 时按当前唯一实现的 Android 检查。"""
    req = qa._mobile_doctor_requirements([], True)
    assert req["android"] is True
    assert req["ios"] is False


def test_ios_detection_off_macos_is_honest(qa):
    """非 macOS 直接判不可用，不做「也许能连」的猜测。"""
    info = qa._detect_ios_devices(platform="win32")
    assert info["available"] is False
    assert "macOS" in info["reason"]


def test_ios_detection_requires_xcrun(qa, monkeypatch):
    monkeypatch.setattr(qa.shutil, "which", lambda name: None)
    info = qa._detect_ios_devices(platform="darwin")
    assert info["available"] is False
    assert "xcrun" in info["reason"]


def test_ios_detection_parses_simulators_and_devices(qa, monkeypatch):
    monkeypatch.setattr(
        qa.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in {"xcrun", "idevice_id"} else None,
    )
    sim_json = json.dumps({"devices": {"iOS 17.0": [
        {"name": "iPhone 15", "udid": "SIM-1", "state": "Booted"},
    ]}})

    def fake_run(cmd, cwd, timeout=20, **kwargs):
        if cmd[1:3] == ["simctl", "list"]:
            return {"exitCode": 0, "stdout": sim_json, "stderr": ""}
        if Path(cmd[0]).name == "idevice_id":
            return {"exitCode": 0, "stdout": "00008110-000A1C2E0E91801E\n", "stderr": ""}
        return {"exitCode": 1, "stdout": "", "stderr": ""}

    monkeypatch.setattr(qa, "run_cmd", fake_run)
    info = qa._detect_ios_devices(platform="darwin")
    assert info["available"] is True
    assert info["simulators"][0]["udid"] == "SIM-1"
    assert info["devices"][0]["udid"] == "00008110-000A1C2E0E91801E"


def test_xctrace_parser_excludes_macs_and_simulators(qa):
    """Mac 和模拟器不能混进 iOS 真机列表。"""
    stdout = """
== Devices ==
MacBook Pro (17.0) (00000000-0000-0000-0000-000000000000)
My Phone (17.0) (00008110-000A1C2E0E91801E)
== Simulators ==
iPhone 15 Simulator (17.0) (ABCD-1234-EFGH-5678-IJKL-9012)
"""
    devices = qa._parse_xctrace_devices(stdout)
    assert [d["udid"] for d in devices] == ["00008110-000A1C2E0E91801E"]


def test_ios_check_results_report_missing_simulator(qa):
    info = {"macOS": True, "xcode": True, "simulators": [], "devices": [],
            "reason": ""}
    checks = {c[0]: c for c in qa._mobile_ios_check_results(info, True, False)}
    assert checks["mobile_ios_host"][1] is True
    assert checks["mobile_ios_simulators"][1] is False
    assert "Simulator Runtime" in checks["mobile_ios_simulators"][3]


def _run_doctor(qa, repo, json_path, monkeypatch):
    """跑 doctor 并返回 checks；adb / mcp 打桩，避免依赖本机状态。"""
    monkeypatch.setattr(qa, "_detect_adb_devices", lambda *a, **k: {
        "available": True, "adbPath": "/usr/bin/adb", "devices": ["R1"],
        "authorized": ["R1"], "unauthorized": [],
    })
    monkeypatch.setattr(qa, "_mobile_mcp_registered", lambda *a, **k: True)
    args = qa.build_parser().parse_args([
        "doctor", "--repo", str(repo), "--json", str(json_path),
    ])
    qa.doctor(args)
    return json.loads(json_path.read_text(encoding="utf-8"))["checks"]


def test_doctor_blocks_android_toolchain_when_task_exists(qa, tmp_path, monkeypatch):
    """项目有 Android task 时，adb / 设备 / mobile-mcp 三项都必须是阻断项。

    历史不一致：设备项会阻断，但 adb 项只有显式 flag 才阻断——adb 缺失时
    设备项被跳过，结果整组都不拦。
    """
    _write_spec_tasks(tmp_path, [{"id": "T1", "layer": "e2e", "platform": "android-real"}])
    checks = _run_doctor(qa, tmp_path, tmp_path / "doctor.json", monkeypatch)
    mobile = {c["name"]: c for c in checks if c["name"].startswith("mobile_")}
    assert set(mobile) == {"mobile_adb", "mobile_devices", "mobile_mcp"}
    assert all(c["required"] is True for c in mobile.values())


def test_doctor_blocks_ios_explicitly(qa, tmp_path, monkeypatch):
    """只声明 iOS 时不检查 adb，但必须给出一个失败的 iOS 阻断项。"""
    _write_spec_tasks(tmp_path, [{"id": "T1", "layer": "e2e", "platform": "ios-sim"}])
    checks = _run_doctor(qa, tmp_path, tmp_path / "doctor.json", monkeypatch)
    mobile = {c["name"]: c for c in checks if c["name"].startswith("mobile_")}
    assert "mobile_ios_host" in mobile
    assert mobile["mobile_ios_host"]["ok"] is False
    assert mobile["mobile_ios_host"]["required"] is True
    assert "mobile_adb" not in mobile, "只声明 iOS 不该要求装 Android 的 adb"


def test_doctor_accepts_ios_when_macos_toolchain_ready(qa, tmp_path, monkeypatch):
    """macOS + Xcode + 已启动模拟器时，iOS 检测项应通过。"""
    _write_spec_tasks(tmp_path, [{"id": "T1", "layer": "e2e", "platform": "ios-sim"}])
    monkeypatch.setattr(qa, "_detect_ios_devices", lambda *a, **k: {
        "available": True, "platform": "darwin", "macOS": True, "xcode": True,
        "simulators": [{"name": "iPhone 15", "udid": "SIM-1",
                        "state": "Booted", "runtime": "iOS 17.0"}],
        "devices": [], "reason": "",
    })
    checks = _run_doctor(qa, tmp_path, tmp_path / "doctor.json", monkeypatch)
    mobile = {c["name"]: c for c in checks if c["name"].startswith("mobile_")}
    assert mobile["mobile_ios_host"]["ok"] is True
    assert mobile["mobile_ios_simulators"]["ok"] is True


def test_doctor_requires_ios_real_device_when_task_declares_it(qa, tmp_path, monkeypatch):
    _write_spec_tasks(tmp_path, [{"id": "T1", "layer": "e2e", "platform": "ios-real"}])
    monkeypatch.setattr(qa, "_detect_ios_devices", lambda *a, **k: {
        "available": True, "platform": "darwin", "macOS": True, "xcode": True,
        "simulators": [{"name": "iPhone 15", "udid": "SIM-1",
                        "state": "Booted", "runtime": "iOS 17.0"}],
        "devices": [], "reason": "",
    })
    checks = _run_doctor(qa, tmp_path, tmp_path / "doctor.json", monkeypatch)
    mobile = {c["name"]: c for c in checks if c["name"].startswith("mobile_")}
    assert mobile["mobile_ios_devices"]["ok"] is False
    assert "信任" in mobile["mobile_ios_devices"]["nextAction"]


# --- ⑮ iOS 人工准备步骤 ---


def _isolate_adb(qa, monkeypatch):
    monkeypatch.setattr(qa, "_detect_adb_devices", lambda *a, **k: {
        "available": False, "adbPath": "", "devices": [],
        "authorized": [], "unauthorized": [],
    })
    monkeypatch.setattr(qa, "_adb_manual_steps", lambda *a, **k: [])


def test_with_ios_reports_manual_steps(qa, tmp_path, monkeypatch):
    _isolate_adb(qa, monkeypatch)
    result = qa.install_mobile_e2e_in_repo(
        tmp_path, dry_run=True, with_adb=False, with_ios=True,
    )
    step = next(s for s in result["steps"] if s["step"] == "ios")
    assert step["status"] == "manual"
    assert any("Xcode" in item for item in result["manualSteps"])


def test_ios_task_triggers_manual_ios_step_without_flag(qa, tmp_path, monkeypatch):
    """项目里声明了 iOS task 时，安装命令要主动报出 Xcode 的人工步骤。"""
    _isolate_adb(qa, monkeypatch)
    _write_spec_tasks(tmp_path, [{"id": "T1", "layer": "e2e", "platform": "ios-real"}])
    result = qa.install_mobile_e2e_in_repo(tmp_path, dry_run=True, with_adb=False)
    assert any(s["step"] == "ios" for s in result["steps"])
    assert any("Xcode" in item for item in result["manualSteps"])


# --- ⑫ Mobilewright 回归执行 ---


def _mobile_spec(tmp_path):
    spec = tmp_path / "tests" / "e2e-mobile" / "app" / "tc-p0-001.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// mobilewright spec", encoding="utf-8")
    return spec


def _mobile_args(tmp_path, spec, **overrides):
    values = dict(
        repo=str(tmp_path), spec=str(spec), project=None,
        case_id="TC-P0-001", task_id="SPEC-TC-P0-001-E2E-001",
        extra=[], timeout=5,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_run_mobile_e2e_builds_mobilewright_command(qa, tmp_path, monkeypatch):
    spec = _mobile_spec(tmp_path)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="PASS", stderr="")

    monkeypatch.setattr(qa, "find_npx", lambda: "/usr/bin/npx")
    monkeypatch.setattr(qa.subprocess, "run", fake_run)

    qa.run_mobile_e2e(_mobile_args(tmp_path, spec))

    assert seen["cmd"][:5] == ["/usr/bin/npx", "mobilewright", "test", "--project", "android"]
    assert seen["cmd"][5] == str(spec)
    assert seen["cwd"] == str(tmp_path)


def test_run_mobile_e2e_records_sidecar(qa, tmp_path, monkeypatch):
    spec = _mobile_spec(tmp_path)
    monkeypatch.setattr(qa, "find_npx", lambda: "/usr/bin/npx")
    monkeypatch.setattr(
        qa.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="PASS", stderr=""),
    )

    qa.run_mobile_e2e(_mobile_args(tmp_path, spec))

    sidecars = list((tmp_path / ".qa-agent" / "runs").glob("*.meta.json"))
    assert sidecars, "回归执行没有写 sidecar，aggregate-runs 会漏掉这次执行"
    data = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert data["caseId"] == "TC-P0-001"
    assert data["taskId"] == "SPEC-TC-P0-001-E2E-001"
    assert data["outcome"] == "PASS"
    assert isinstance(data.get("durationMs"), int), "sidecar 要记录耗时，否则「跑得慢」无法量化"
    assert data["durationMs"] >= 0


def test_run_mobile_e2e_failure_still_records_evidence(qa, tmp_path, monkeypatch):
    """失败也要留证据——只在成功时记录，失败就查不清了。"""
    spec = _mobile_spec(tmp_path)
    monkeypatch.setattr(qa, "find_npx", lambda: "/usr/bin/npx")
    monkeypatch.setattr(
        qa.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    with pytest.raises(SystemExit):
        qa.run_mobile_e2e(_mobile_args(tmp_path, spec))

    sidecars = list((tmp_path / ".qa-agent" / "runs").glob("*.meta.json"))
    assert sidecars
    assert json.loads(sidecars[0].read_text(encoding="utf-8"))["outcome"] == "FAIL"


def test_run_mobile_e2e_missing_spec_is_readable(qa, tmp_path):
    with pytest.raises(qa.QaAgentError) as exc:
        qa.run_mobile_e2e(_mobile_args(tmp_path, tmp_path / "nope.spec.ts"))
    assert "脚本不存在" in str(exc.value)


def test_run_mobile_e2e_requires_npx(qa, tmp_path, monkeypatch):
    spec = _mobile_spec(tmp_path)
    monkeypatch.setattr(qa, "find_npx", lambda: None)
    with pytest.raises(qa.QaAgentError) as exc:
        qa.run_mobile_e2e(_mobile_args(tmp_path, spec))
    assert "npx" in str(exc.value)


# --- ⑬ Mobilewright 安装步骤 ---


def test_with_mobilewright_dry_run_reports_step(qa, tmp_path):
    result = qa.install_mobile_e2e_in_repo(
        tmp_path, dry_run=True, with_adb=False, with_mobilewright=True,
    )
    step = next(s for s in result["steps"] if s["step"] == "mobilewright")
    assert step["status"] == "dry-run"
    assert "@mobilewright/test" in step["command"]


# --- ⑭ 写用户 PATH 的逻辑（用假 winreg，不碰真实注册表）---


def _fake_winreg(stored):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(*args, **kwargs):
        return Key()

    def query(key, name):
        return stored["Path"], 2

    def set_value(key, name, reserved, kind, value):
        stored["Path"] = value

    return SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_READ=1, KEY_SET_VALUE=2, REG_EXPAND_SZ=2,
        OpenKey=open_key, QueryValueEx=query, SetValueEx=set_value,
    )


def test_add_to_user_path_windows_appends(qa, tmp_path, monkeypatch):
    stored = {"Path": "C:\\existing"}
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(stored))
    result = qa._add_to_user_path(tmp_path, platform="windows")
    assert result["status"] == "passed"
    assert str(tmp_path) in stored["Path"]


def test_add_to_user_path_windows_is_idempotent(qa, tmp_path, monkeypatch):
    stored = {"Path": "C:\\existing"}
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(stored))
    qa._add_to_user_path(tmp_path, platform="windows")
    assert qa._add_to_user_path(tmp_path, platform="windows")["status"] == "skipped"
