"""ADB-based mobile driver for ming-qa.

Why this exists: mobile-mcp is an MCP server and is not always available in the
agent session. This module is the deterministic, zero-dependency fallback that
encodes the traps we hit on real devices:

  - Chinese IMEs intercept `adb shell input text`; we disable IMEs around each
    typing call and restore them immediately (so a killed script cannot leave
    the device with no keyboard).
  - Product titles are often non-clickable; tapping them does nothing. We walk
    up to the nearest clickable ancestor.
  - `uiautomator dump` writes a device path; doing it through a shell can be
    rewritten by Git Bash. We always use Python subprocess, never a shell.
  - `pm clear` is denied on Android 17 / ColorOS. We fall back to an
    application-provided reset menu, then `run-as`, and fail loudly.

The module is importable by test scripts:

    import os, sys
    sys.path.insert(0, os.path.join(os.environ["QA_AGENT_DIR"], "scripts"))
    from mobile_adb import MobileAdb

    device = MobileAdb()          # requires exactly one authorized device
    device.launch("com.example.app")
    device.tap(text="Add to cart")
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

DEFAULT_KEYCODES = {
    " ": 62,
    "@": 77,
    ".": 56,
    "/": 76,
    "-": 69,
    "_": 69,
    ",": 55,
    ":": 74,
    ";": 74,
}
for _index, _letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
    DEFAULT_KEYCODES[_letter] = 29 + _index
    DEFAULT_KEYCODES[_letter.upper()] = 29 + _index
for _digit in "0123456789":
    DEFAULT_KEYCODES[_digit] = 7 + int(_digit)


class MobileAdbError(RuntimeError):
    pass


def find_adb(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("ADB_PATH")
    if env and Path(env).exists():
        return env
    found = shutil.which("adb")
    if found:
        return found
    candidates = [
        Path.home() / ".ming-qa" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"),
        Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise MobileAdbError(
        "adb not found; install Android Platform Tools and put it on PATH, "
        "or set ADB_PATH"
    )


class MobileAdb:
    def __init__(self, serial: str | None = None, adb: str | None = None, timeout: int = 60):
        self.adb = find_adb(adb)
        self.timeout = timeout
        self.serial = serial or self._detect_single_device()

    # --- process / device -------------------------------------------------
    def _run(self, *args: object, timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, "-s", self.serial, *map(str, args)],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout or self.timeout,
        )

    def _detect_single_device(self) -> str:
        result = subprocess.run(
            [self.adb, "devices"], capture_output=True, encoding="utf-8",
            errors="replace", timeout=self.timeout,
        )
        authorized: list[str] = []
        unauthorized: list[str] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            if parts[1] == "device":
                authorized.append(parts[0])
            elif parts[1] == "unauthorized":
                unauthorized.append(parts[0])
        if not authorized:
            hint = f" unauthorized={unauthorized}" if unauthorized else " (no devices)"
            raise MobileAdbError(f"no authorized Android device;{hint}")
        if len(authorized) > 1:
            raise MobileAdbError(
                "multiple devices online; pass serial explicitly: " + ", ".join(authorized)
            )
        return authorized[0]

    def launch(self, package: str) -> None:
        self._run("shell", "monkey", "-p", package, "-c",
                  "android.intent.category.LAUNCHER", "1")

    # --- element tree -----------------------------------------------------
    def dump(self) -> ET.Element:
        with tempfile.TemporaryDirectory(prefix="mingqa-mobile-") as tmp:
            local = Path(tmp) / "window.xml"
            self._run("shell", "uiautomator", "dump", "/sdcard/window.xml")
            self._run("pull", "/sdcard/window.xml", str(local))
            return ET.parse(local).getroot()

    @staticmethod
    def _matches(node: ET.Element, rid: str | None, text: str | None,
                 desc: str | None) -> bool:
        node_id = node.get("resource-id") or ""
        if rid and (node_id == rid or node_id.split("/")[-1] == rid):
            return True
        if text and (node.get("text") or "").strip() == text:
            return True
        if desc and (node.get("content-desc") or "").strip() == desc:
            return True
        return False

    def find(self, rid: str | None = None, text: str | None = None,
             desc: str | None = None, root: ET.Element | None = None) -> ET.Element | None:
        root = root if root is not None else self.dump()
        for node in root.iter("node"):
            if self._matches(node, rid, text, desc):
                return node
        return None

    @staticmethod
    def bounds(node: ET.Element) -> tuple[int, int, int, int]:
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
        if not match:
            raise MobileAdbError(f"invalid bounds: {node.get('bounds')!r}")
        return tuple(map(int, match.groups()))

    @staticmethod
    def clickable_ancestor(root: ET.Element, target: ET.Element) -> ET.Element:
        parent_map = {child: parent for parent in root.iter("node") for child in parent}
        node: ET.Element | None = target
        while node is not None and node.get("clickable") != "true":
            node = parent_map.get(node)
        return node if node is not None else target

    def tap(self, rid: str | None = None, text: str | None = None,
            desc: str | None = None, timeout: int = 12) -> None:
        end = time.time() + timeout
        while time.time() < end:
            root = self.dump()
            node = self.find(rid=rid, text=text, desc=desc, root=root)
            if node is not None:
                target = self.clickable_ancestor(root, node)
                left, top, right, bottom = self.bounds(target)
                self._run("shell", "input", "tap",
                          (left + right) // 2, (top + bottom) // 2)
                time.sleep(0.6)
                return
            time.sleep(0.5)
        raise MobileAdbError(f"not found: {rid or text or desc}")

    # --- text input -------------------------------------------------------
    def _ime_list(self) -> list[str]:
        result = self._run("shell", "ime", "list", "-s")
        return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]

    def _type_keyevents(self, value: str) -> None:
        keycodes: list[str] = []
        for char in value:
            code = DEFAULT_KEYCODES.get(char)
            if code is None:
                raise MobileAdbError(f"no keycode for character: {char!r}")
            keycodes.append(str(code))
        if keycodes:
            self._run("shell", "input", "keyevent", *keycodes)

    def type_text(self, value: str, rid: str | None = None, clear: bool = False) -> None:
        """Type text with IMEs disabled for this call only.

        Disabling round the whole test script is what left the device without a
        keyboard when a run timed out. Per-call disable/restore makes timeouts
        harmless.
        """
        if rid:
            self.tap(rid=rid)
        saved = self._run("shell", "settings", "get", "secure",
                          "default_input_method").stdout.strip()
        imes = self._ime_list()
        try:
            for ime in imes:
                self._run("shell", "ime", "disable", ime)
            if clear:
                self._run("shell", "input", "keyevent", "123")
                self._run("shell", "input", "keyevent", *(["67"] * 40))
            result = self._run("shell", "input", "text", value.replace(" ", "%s"))
            if result.returncode != 0:
                self._type_keyevents(value)
        finally:
            for ime in imes:
                self._run("shell", "ime", "enable", ime)
            if saved:
                self._run("shell", "ime", "set", saved)

    def check(self, rid: str) -> None:
        root = self.dump()
        node = self.find(rid=rid, root=root)
        if node is None:
            raise MobileAdbError(f"not found: {rid}")
        left, top, right, bottom = self.bounds(node)
        self._run("shell", "input", "tap", left + 60, (top + bottom) // 2)
        time.sleep(0.4)
        root = self.dump()
        node = self.find(rid=rid, root=root)
        if node is not None and node.get("checked") == "true":
            return
        if node is not None:
            left, top, right, bottom = self.bounds(node)
            self._run("shell", "input", "tap",
                      (left + right) // 2, (top + bottom) // 2)
            time.sleep(0.4)
            root = self.dump()
            node = self.find(rid=rid, root=root)
        if node is None or node.get("checked") != "true":
            raise MobileAdbError(f"{rid} did not become checked")

    # --- state / evidence -------------------------------------------------
    def reset_state(self, package: str, *, reset_menu_item: str | None = None,
                    reset_confirm: str | None = None, reset_ok: str | None = None) -> str:
        """Try pm clear, then the app's reset menu, then run-as. Returns the method."""
        cleared = self._run("shell", "pm", "clear", package)
        if cleared.returncode == 0:
            return "pm-clear"
        if reset_menu_item and reset_confirm:
            try:
                self.launch(package)
                time.sleep(1)
                self.tap(text=reset_menu_item)
                self.tap(text=reset_confirm)
                if reset_ok:
                    self.tap(text=reset_ok)
                return "app-menu"
            except MobileAdbError:
                pass
        removed = self._run(
            "shell", "run-as", package, "sh", "-c",
            "rm -rf databases/* shared_prefs/* files/* cache/*",
        )
        if removed.returncode == 0:
            return "run-as"
        raise MobileAdbError(
            "cannot reset app state: pm clear denied, no app reset menu configured, "
            "and run-as failed"
        )

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        shot = subprocess.run(
            [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=self.timeout,
        )
        path.write_bytes(shot.stdout)

    def logcat(self, lines: int = 500, package: str = "") -> str:
        """抓设备日志。

        传 package 时只取该进程的日志：整机 logcat 会混入其他 App 的历史崩溃，
        证据门禁的崩溃扫描会因此误报——这不是本次测试的失败。
        """
        args = ["logcat", "-d", "-t", str(lines)]
        if package:
            pids = (self._run("shell", "pidof", package).stdout or "").split()
            if pids:
                args.extend(["--pid", pids[0]])
        return self._run(*args).stdout

    def save_evidence(self, repo: Path, case_id: str, task_id: str, *,
                      locator: str, duration_ms: int, fallback_reason: str = "",
                      note: str = "", run_id: str | None = None,
                      scope: str = "mydemo", package: str = "",
                      run_log: str = "", started_at: str = "",
                      finished_at: str = "") -> Path:
        """抓三类证据并写 manifest。

        `run_log` 是这次执行的 run 日志文件名。写进 manifest 后，门禁才能判断
        「这堆截图属于本轮」而不是「某个历史目录里正好有一份」——否则把旧目录
        复制进来就能冒充一次通过。

        `started_at` / `finished_at` 与 `captureWindow` 一起说明采集时机：这里的
        树、截图、日志都是**测试结束之后**补采的终态快照，不是断言发生那一刻的
        现场。这一步是为了给门禁留下可核对的证据（有证据 + 绑定执行），而不是
        假装它见证了断言；要看断言那一刻的画面，得靠 Mobilewright 的
        `viewTree: 'on-failure'` 产物或逐步截图。
        """
        run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        evidence_dir = Path(repo) / ".qa-agent" / "runs" / run_id / "mobile" / (scope or "mobile")
        evidence_dir.mkdir(parents=True, exist_ok=True)
        root = self.dump()
        (evidence_dir / "view-tree.xml").write_bytes(
            ET.tostring(root, encoding="utf-8", xml_declaration=True)
        )
        self.screenshot(evidence_dir / "screen.png")
        (evidence_dir / "logcat.txt").write_text(
            self.logcat(package=package), encoding="utf-8"
        )
        manifest = {
            "caseId": case_id,
            "taskId": task_id,
            "device": self.serial,
            "platform": "android-real",
            "locator": locator,
            "fallbackReason": fallback_reason,
            "durationMs": max(0, int(duration_ms)),
            "runLog": run_log,
            "captureWindow": "post-run",
            "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "evidence": {
                "viewTree": "view-tree.xml",
                "screenshot": "screen.png",
                "logcat": "logcat.txt",
            },
            "note": note,
        }
        if started_at:
            manifest["startedAt"] = started_at
        if finished_at:
            manifest["finishedAt"] = finished_at
        if package:
            manifest["package"] = package
        (evidence_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return evidence_dir
