"""Regression tests for the ADB driver's trap handling."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

import mobile_adb


TREE = """<hierarchy>
  <node resource-id="pkg:id/list" clickable="true" bounds="[0,0][1080,2000]">
    <node resource-id="pkg:id/title" clickable="false" text="Sauce Labs Backpack" bounds="[60,100][500,200]" />
  </node>
  <node resource-id="pkg:id/check" class="android.widget.CheckBox" checkable="true"
        checked="{checked}" clickable="true" bounds="[45,1850][1035,1994]" />
</hierarchy>"""


class FakeAdb(mobile_adb.MobileAdb):
    def __init__(self, root: ET.Element, run_results=None):
        self.adb = "adb"
        self.timeout = 1
        self.serial = "SERIAL"
        self.calls: list[tuple] = []
        self.root = root
        self.run_results = run_results or {}

    def _run(self, *args, timeout=None):
        self.calls.append(args)
        result = self.run_results.get(args)
        if result is not None:
            return result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def dump(self):
        return self.root


def _tree(checked="false"):
    return ET.fromstring(TREE.format(checked=checked))


def test_clickable_ancestor_walks_up():
    root = _tree()
    title = root.find(".//node[@resource-id='pkg:id/title']")
    target = mobile_adb.MobileAdb.clickable_ancestor(root, title)
    assert target.get("resource-id") == "pkg:id/list"


def test_tap_non_clickable_text_uses_parent_bounds():
    fake = FakeAdb(_tree())
    fake.tap(text="Sauce Labs Backpack")
    tap_calls = [c for c in fake.calls if c[:3] == ("shell", "input", "tap")]
    assert tap_calls, fake.calls
    # Parent list spans the screen; center is (540, 1000).
    assert tap_calls[-1][3:] == (540, 1000)


def test_type_text_disables_and_restores_imes():
    fake = FakeAdb(_tree())
    fake._ime_list = lambda: ["ime.cn/.One", "ime.cn/.Two"]
    fake.run_results = {
        ("shell", "settings", "get", "secure", "default_input_method"):
            SimpleNamespace(returncode=0, stdout="ime.cn/.One\n", stderr="")
    }
    fake.type_text("Rebecca Winter", rid="pkg:id/title")
    calls = fake.calls
    disabled = [c for c in calls if c[:3] == ("shell", "ime", "disable")]
    enabled = [c for c in calls if c[:3] == ("shell", "ime", "enable")]
    assert disabled and enabled
    assert len(disabled) == len(enabled) == 2
    text_calls = [c for c in calls if c[:3] == ("shell", "input", "text")]
    assert text_calls and text_calls[-1][3] == "Rebecca%sWinter"
    assert calls[-1] == ("shell", "ime", "set", "ime.cn/.One")


def test_check_retries_center_when_left_edge_misses(monkeypatch):
    fake = FakeAdb(_tree(checked="false"))
    checks = iter(["false", "false", "true"])

    def dump():
        try:
            value = next(checks)
        except StopIteration:
            value = "true"
        return _tree(checked=value)

    fake.dump = dump
    fake.check("pkg:id/check")
    taps = [c for c in fake.calls if c[:3] == ("shell", "input", "tap")]
    assert len(taps) >= 2, "should fall back to a center tap"


def test_multi_device_requires_serial(monkeypatch):
    output = "List of devices attached\nAAA\tdevice\nBBB\tdevice\n"
    monkeypatch.setattr(
        mobile_adb.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    with pytest.raises(mobile_adb.MobileAdbError) as exc:
        mobile_adb.MobileAdb(adb="adb")
    assert "multiple devices" in str(exc.value)


def test_unauthorized_device_is_rejected(monkeypatch):
    output = "List of devices attached\nAAA\tunauthorized\n"
    monkeypatch.setattr(
        mobile_adb.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    with pytest.raises(mobile_adb.MobileAdbError) as exc:
        mobile_adb.MobileAdb(adb="adb")
    assert "unauthorized" in str(exc.value)


def test_reset_state_falls_back_to_run_as_when_pm_clear_denied():
    fake = FakeAdb(_tree())
    fake.run_results = {
        ("shell", "pm", "clear", "pkg"): SimpleNamespace(
            returncode=1, stdout="", stderr="SecurityException",
        ),
        ("shell", "run-as", "pkg", "sh", "-c",
         "rm -rf databases/* shared_prefs/* files/* cache/*"):
            SimpleNamespace(returncode=0, stdout="", stderr=""),
    }
    assert fake.reset_state("pkg") == "run-as"


def test_save_evidence_writes_manifest_and_files(tmp_path):
    fake = FakeAdb(_tree())

    def screenshot(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")

    fake.screenshot = screenshot
    fake.logcat = lambda lines=500, package="": "I/app: ok\n"
    evidence_dir = fake.save_evidence(
        tmp_path, "TC-1", "SPEC-1", locator="tree", duration_ms=1234,
        note="PASS",
    )
    manifest = json.loads((evidence_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["caseId"] == "TC-1"
    assert manifest["durationMs"] == 1234
    assert manifest["evidence"]["screenshot"] == "screen.png"
    assert (evidence_dir / "view-tree.xml").exists()
    assert (evidence_dir / "logcat.txt").read_text(encoding="utf-8") == "I/app: ok\n"
