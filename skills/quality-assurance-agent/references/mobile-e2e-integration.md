# Mobile E2E Integration

当 scope 涉及 iOS / Android 客户端时，使用本参考了解移动端 E2E 的宿主前提、执行路径与证据要求。

移动端 spec-task 的标识是 `platform: android-real`（缺省 `platform` 为 `web`）。

## 平台限制

移动端各目标对宿主环境的要求不同，**这是硬约束，换任何框架都绕不开**：

| 目标 | 依赖 | Windows | Linux | macOS |
|---|---|---|---|---|
| Android 真机 | `adb` + USB 授权 | ✅ | ✅ | ✅ |
| Android 仿真器 | Android SDK | ✅ | ✅ | ✅ |
| iOS 模拟器 | macOS + Xcode + `xcrun simctl` | ❌ | ❌ | ✅ |
| iOS 真机 | macOS + Xcode + WebDriverAgent + 设备隧道 | ❌ | ❌ | ✅ |

**当前只实现 Android 真机。** iOS 需要 macOS，尚未支持。

宿主不支持时的行为：`doctor` 只列举当前平台可用的检测项——Windows / Linux 上 iOS 相关项**根本不在清单里**，不会产生无意义的报错。

## 环境准备

**优先用命令自动完成**：

```bash
python "$QA_AGENT_DIR/scripts/qa_agent.py" install-mobile-e2e --repo .
```

它做三件事（幂等）：注册 mobile-mcp 到 `.mcp.json`、安装 Android Platform Tools、（加
`--with-mobilewright` 时）安装 `@mobilewright/test`。

adb 的装法：优先用平台包管理器（winget / brew / apt）；**都没有时回退到 Google 官方 CDN
下载 zip 到 `~/.ming-qa/platform-tools/`**——Windows 10 LTSC 默认没有 winget，走的就是这条
回退。这条回退**不修改系统 PATH**：要在命令行直接敲 `adb`，需自行把该目录加进 PATH。

装完命令会把仍需人工完成的步骤（USB 授权）作为 `manualSteps` 报出来。**那不是失败，是物理操作**，见下方第 2 步。

下面的手工步骤供排查使用，或在包管理器与下载都不可用时兜底。

### 1. 安装 Android Platform Tools（含 adb）

```bash
# Windows
winget install Google.PlatformTools

# macOS
brew install --cask android-platform-tools

# Debian / Ubuntu
sudo apt install android-tools-adb
```

装完确认 `adb version` 可执行。

### 2. 在手机上授权 USB 调试

这一步**无法自动化**，是首次运行的必然卡点：

1. 手机 → 设置 → 关于手机 → 连点「版本号」7 次，开启开发者选项
2. 开发者选项 → 打开「USB 调试」
3. USB 连接电脑
4. **在手机屏幕上点「允许此计算机调试」**（建议勾选「一律允许」）

验证：

```bash
adb devices
```

期望输出（设备序列号与状态之间是制表符）：

```
List of devices attached
R58M12345	device
```

- 显示 `unauthorized` → 第 4 步的弹窗没点
- 完全没有设备行 → 检查 USB 线是否支持数据传输，或换一根
- 弹窗始终不出现 → `adb kill-server && adb devices` 重新触发授权

### 3. 注册 mobile-mcp

```bash
claude mcp add mobile-mcp -- npx -y @mobilenext/mobile-mcp@latest
```

重启 agent 会话后，`mobile_list_available_devices` 应可用。

## 方式 ②：mobile-mcp 直驱（默认）

日常执行与调试走这条路径。agent 通过 MCP 工具操作设备：

```
mobile_list_available_devices      # 确认目标设备
mobile_launch_app                  # 启动被测 app
mobile_list_elements_on_screen     # 读 accessibility tree → 定位依据
mobile_click_on_screen_at_coordinates / mobile_type_keys
mobile_get_device_logs             # 崩溃 / 异常取证
```

**定位必须基于 accessibility tree 返回的元素树，不基于截图找坐标。**

元素树是确定性的、token 高效的；截图找坐标既昂贵又脆弱。坐标点击仅作为元素树无法覆盖时的降级手段，且必须在 evidence 中标注降级原因。

## 多设备处理

`adb devices` 可能列出多台设备（真机 + 仿真器并存很常见）。

**必须显式指定目标设备**，不允许静默选取第一台。静默选取会让测试结果不可复现，且出问题时极难排查。

## 方式 ③：@mobilewright/test（沉淀资产）

用于沉淀可回归的测试蓝图：

- 生成 spec 到 `tests/e2e-mobile/<module>/`
- 运行：`npx mobilewright test --project=android`
- 配置：`mobilewright.config.ts`

> Mobilewright 目前处于 0.0.x，API 可能变动。**它的细节不进 CLI，只在本参考里维护**——这样它 breaking 时只需要改这份文档。

## 证据契约

移动端 task 的 evidence 必须包含三类：

| 证据 | 来源 | 用途 |
|---|---|---|
| view tree 快照 | `mobile_list_elements_on_screen` | 断言的实际依据，可离线复核 |
| 截图 | `mobile_take_screenshot` | 人可读的现场 |
| device log | `mobile_get_device_logs`（logcat） | 崩溃与异常的唯一真相 |

落盘路径：`.qa-agent/runs/<run-id>/mobile/<scope>/`

## 失败分类补充

在 `qa-test-runner/SKILL.md` 的三分类之外，移动端另有两类高频失败：

| 失败类型 | 判断标准 | 行动 |
|---|---|---|
| **设备问题** | 设备离线、USB 授权失效、ADB 断连 | 归入环境问题，修环境后重跑，**不计入产品缺陷** |
| **设备歧义** | 多台设备在线且未显式指定 | 要求显式指定设备，不允许静默选取 |
