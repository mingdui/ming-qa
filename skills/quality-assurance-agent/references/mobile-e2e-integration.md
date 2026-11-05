# Mobile E2E Integration

当 scope 涉及 iOS / Android 客户端时，使用本参考了解移动端 E2E 的宿主前提、执行路径与证据要求。

移动端 spec-task 用 `platform` 标识（缺省为 `web`）：

| `platform` | 含义 | 当前状态 |
|---|---|---|
| `web` | 浏览器 E2E（Playwright） | 支持 |
| `android-real` | Android 真机 | **已实现** |
| `android-emulator` | Android 模拟器 | **已实现**（与真机共用 adb 通路） |
| `ios-sim` | iOS 模拟器 | 已接入检测；需 macOS + Xcode，**未实测** |
| `ios-real` | iOS 真机 | 已接入检测；需 macOS + Xcode，**未实测** |

## 平台限制

移动端各目标对宿主环境的要求不同，**这是硬约束，换任何框架都绕不开**：

| 目标 | 依赖 | Windows | Linux | macOS |
|---|---|---|---|---|
| Android 真机 | `adb` + USB 授权 | ✅ | ✅ | ✅ |
| Android 仿真器 | Android SDK | ✅ | ✅ | ✅ |
| iOS 模拟器 | macOS + Xcode + `xcrun simctl` | ❌ | ❌ | ✅ |
| iOS 真机 | macOS + Xcode + WebDriverAgent + 设备隧道 | ❌ | ❌ | ✅ |

iOS 的驱动同样走 `mobile-mcp` / `Mobilewright`，ming-qa 负责宿主检测、平台接入和证据契约。
**一切 macOS 相关逻辑都未在 macOS 实测**——代码路径已就绪，真机/模拟器验证需要在 Mac 上做。

声明了 iOS 平台不会被静默当成 web 放行，也不会被要求安装 Android 的 adb。`doctor` 的检查项：

| 检查项 | 通过条件 | 不通过时的下一步 |
|---|---|---|
| `mobile_ios_host` | 宿主是 macOS 且能找到 `xcrun` | 在 macOS 上执行，或改用 Android；安装 Xcode 并同意许可协议 |
| `mobile_ios_simulators` | `xcrun simctl list devices available` 至少有一台模拟器 | Xcode → Settings → Platforms 安装 iOS Simulator Runtime |
| `mobile_ios_devices` | `idevice_id -l` 或 `xcrun xctrace list devices` 至少有一台 iPhone/iPad | 连接设备、在手机上点「信任」 |

Windows / Linux 上只会出现失败的 `mobile_ios_host`，不会去执行任何 iOS 命令。
`--require-mobile-e2e` 且项目没有 task 时按 Android 检查；只声明 iOS 的项目不会被要求装 adb，
`install-mobile-e2e` 也会跳过 Android Platform Tools。

真机列表优先用 `idevice_id -l`（libimobiledevice，输出最稳）；没有它才解析
`xcrun xctrace list devices`。xctrace 的输出把 Mac 和 iPhone 混在同一段，解析时靠机型名
排除 Mac——**如果 Mac 被重命名成不含机型关键词的名字，可能被误判成 iOS 设备**。
在 macOS 上实测时先用 `idevice_id` 或 Xcode 的设备窗口交叉确认。

## 环境准备

**优先用命令自动完成**：

```bash
python "$QA_AGENT_DIR/scripts/qa_agent.py" install-mobile-e2e --repo .
```

它做三件事（幂等）：注册 mobile-mcp 到 `.mcp.json`、安装 Android Platform Tools、（加
`--with-mobilewright` 时）安装 `@mobilewright/test`。

adb 的装法：优先用平台包管理器（winget / brew / apt）；**都没有时回退到 Google 官方 CDN
下载 zip 到 `~/.ming-qa/platform-tools/`**——Windows 10 LTSC 默认没有 winget，走的就是这条回退。

> ⚠️ **下载回退后，该目录必须加入 PATH。**
>
> 原因：mobile-mcp 这类第三方工具内部直接调用 `adb`，**只认 PATH**，不知道 ming-qa 自管目录
> 的存在。而 ming-qa 自己的 `doctor` 会回退到该目录查找（`_find_adb`）——所以会出现
> 「**本地检测全绿，agent 里却列不出设备**」这种分裂现象，排查时极容易误判为「没有设备」。
>
> 用 `install-mobile-e2e --add-to-path` 写入用户 PATH（Windows 写注册表，幂等），或手动添加。
> **改完必须重开会话**——环境变量对已运行的进程不生效。

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

**方式 ② 没有脚本文件**：对应 spec-task 的 `targetFile` 写 `@agent:mobile-mcp`——`@` 开头是约定
标识，表示「这不是文件路径」。**不要写成 `.spec.ts` 路径**，那会让产物自相矛盾（看路径以为有脚本，
实际拉不到文件），门禁也会正确地报 `target-file-missing`。想沉淀可回归资产请走方式 ③。

**优先用 accessibility tree 的元素树定位；元素树不可用时，退回截图 + 坐标。**

元素树是确定性的、token 高效的；截图找坐标既昂贵又脆弱。但**有一类应用根本没有元素树**——
游戏引擎（Godot / Unity）等自绘 UI 可能整屏只暴露少数几个容器节点（实测某 Godot 游戏只有
3 个 FrameLayout）。此时截图 + 坐标是**唯一**可行手段，不属于违规降级，但必须在 evidence
中标注原因，并说明棋盘/控件区域是如何标定的。

## 多设备处理

`adb devices` 可能列出多台设备（真机 + 仿真器并存很常见）。

**必须显式指定目标设备**，不允许静默选取第一台。静默选取会让测试结果不可复现，且出问题时极难排查。

## 方式 ③：@mobilewright/test（沉淀资产）

用于沉淀可回归的测试蓝图：

- 生成 spec 到 `tests/e2e-mobile/<module>/`
- 运行：

  ```bash
  python "$QA_AGENT_DIR/scripts/qa_agent.py" run-mobile-e2e \
    --repo . --spec tests/e2e-mobile/<module>/<case-id>.spec.ts \
    --case-id TC-P0-001 --task-id SPEC-TC-P0-001-E2E-001
  ```

  它执行 `npx mobilewright test --project=android <spec>`，并把 stdout / stderr /
  exit code 写成与 `run-e2e` 同构的 run sidecar——回归执行和首次执行落在同一条证据链上，
  `aggregate-runs` 不需要区分驱动。iOS 用 `--project ios`（具体名称以
  `mobilewright.config.ts` 为准）。

- 配置：`mobilewright.config.ts`

> Mobilewright 目前处于 0.0.x，API 可能变动。**它的细节不进 CLI，只在本参考里维护**——这样它 breaking 时只需要改这份文档。`run-mobile-e2e` 只固定「怎么调起来、怎么记证据」，不封装它的 API。

## 证据契约

移动端 task 的 evidence 必须包含三类：

| 证据 | 来源 | 用途 |
|---|---|---|
| view tree 快照 | `mobile_list_elements_on_screen` | 断言的实际依据，可离线复核 |
| 截图 | `mobile_save_screenshot` | 人可读的现场（用原始分辨率那份） |
| device log | `mobile_get_device_logs`（Android: logcat；iOS: syslog） | 崩溃与异常的唯一真相 |

落盘路径：`.qa-agent/runs/<run-id>/mobile/<scope>/`，并写一份 `manifest.json`：

```json
{
  "caseId": "TC-P0-001",
  "taskId": "SPEC-TC-P0-001-E2E-001",
  "device": "R58M12345",
  "platform": "android-real",
  "locator": "tree",
  "fallbackReason": "",
  "durationMs": 12345,
  "evidence": {
    "viewTree": "view-tree.json",
    "screenshot": "screen.png",
    "logcat": "logcat.txt"
  }
}
```

- `caseId` 必填；`taskId` 可选，同一用例拆成多条 task 时用它精确对应。
- `device` 必填——多设备环境下不写设备，这次执行不可复现。
- `locator` 必填，取值 `tree`（无障碍树定位）或 `screenshot`（截图坐标兜底）。
- `locator: screenshot` 时 `fallbackReason` 必填，写清为什么元素树不可用。
  用截图不违规，**不写原因才违规**。
- `durationMs` 必填，非负整数——本次执行的实际耗时（毫秒）。报告会汇总累计耗时和
  单条最长耗时，让「跑得慢」变成可量化的数字，而不是感觉。
- 三个证据路径相对 `manifest.json` 所在目录；文件必须真实存在且非空。
- 截图必须是图片文件（`.png` / `.jpg` / `.jpeg` / `.webp`）。
- 设备日志键：Android 写 `logcat`；iOS 写 `deviceLog`（或 `syslog`）。门禁三者等价，
  不改契约、不强迫 iOS 用 Android 的键名。
- **设备日志含崩溃特征是 `passed` 的负向门禁**：`FATAL EXCEPTION`、`ANR in`、
  `E/AndroidRuntime`、`Terminating app due to uncaught exception` 等一旦命中，
  即使三类证据齐全也判失败。证据不能只证明「跑过」，还要证明「没崩」。

### 断言 -> 证据映射

移动端 task 的每条 oracle 断言都必须标明由哪类证据支撑：

```json
"evidenceRefs": {
  "viewTree": ["按钮状态符合预期", "列表项数量为 3"],
  "screenshot": ["自绘棋盘的落子位置正确"],
  "logcat": ["本次执行没有崩溃或 ANR"]
}
```

- 键取 `viewTree` / `screenshot` / `logcat`（iOS 可用 `deviceLog` / `syslog`）。
- 值是该证据支撑的断言原文，必须与 `oracle` 里的 `assertion` 完全一致。
- 门禁只校验「每条断言都有映射」，映射得对不对由代码审查判断——
  机械能查的部分不放过，需要人判断的部分不假装能查。

门禁：

```bash
python "$QA_AGENT_DIR/scripts/qa_agent.py" assert-mobile-evidence \
  --repo . --spec-tasks .qa-agent/current/test-spec-tasks.json
```

`assert-evidence-integrity` 会自动合并这项校验（它默认从 `--repo` 扫描
`.qa-agent/runs/**/mobile/**/manifest.json`）。readiness 读的是
`evidence-integrity-check.json`，所以**少一类证据，最终报告就是 Not Ready / Incomplete**，
不是只打一条警告。

只有 `executionStatus == "passed"` 的移动端 task 需要证据。`blocked` / `failed` /
`not-run` 由各自的语义约束（blocker 证据、失败日志），不在这里重复判。

## 失败分类补充

在 `qa-test-runner/SKILL.md` 的三分类之外，移动端另有两类高频失败：

| 失败类型 | 判断标准 | 行动 |
|---|---|---|
| **设备问题** | 设备离线、USB 授权失效、ADB 断连 | 归入环境问题，修环境后重跑，**不计入产品缺陷** |
| **设备歧义** | 多台设备在线且未显式指定 | 要求显式指定设备，不允许静默选取 |

## 故障排查

### 设备列表为空，但 `adb devices` 明明看得到设备

**最常见的坑，且症状极具误导性**：工具返回 `{"devices": []}` 却**不报任何错误**。

按顺序排查：

**1. `adb` 不在 PATH**

mobile-mcp / mobilecli 内部调用 `adb` 时**只认 PATH**，不会去 ming-qa 自管目录找。
用 `install-mobile-e2e --add-to-path` 修，然后**重开会话**（环境变量对已运行的进程不生效）。

**2. mobilecli 的 daemon 缓存了旧状态**

daemon 在 adb 尚不可见时启动，就会一直记着「零设备」，**光修 PATH 不会让它重新扫描**：

```bash
mobilecli daemon stop     # 下次列设备时会自动重启并重扫
```

验证：`mobilecli daemon status` 看 `uptime` 是否归零。

**3. 手机上没点「允许 USB 调试」**

`adb devices` 会显示 `unauthorized` 而不是 `device`。

> **为什么这个坑难排查**：mobile-mcp 1.0.8 起，设备发现已经不走 adb，而是走 mobilecli 的
> daemon（源码里写着 `we must have mobilecli working`）。所以
> **Android SDK / `ANDROID_HOME` 都不是必需项**——别因为「列不出设备」就去装 Android Studio
> 或申请云真机，本地真机一直都在，问题在这两层。

### 点击 / 滑动 / 按键全部被拒，但截图和元素树正常

Android 报 `SecurityException: Injecting to another application requires INJECT_EVENTS permission`，
或 mobile-mcp 报 `agent error -32603: device refused MotionEvent`。

**这是 MIUI（小米 / Redmi）的系统级加固，不是工具故障。** 用这组对照 5 秒内定位：

| 能力 | 正常 | 被拦 | 走的路径 |
|---|---|---|---|
| 截图 / 读元素树 | ✅ | ✅ | 只读——说明 ADB 通路本身是好的 |
| 点击 / 滑动 | ✅ | ❌ | MotionEvent 注入 |
| `mobile_press_button` | ✅ | ❌ | keyevent 注入（另一条路径） |

**处置**：手机端 **设置 → 更多设置 → 开发者选项 → 「USB调试(安全设置)」** 打开。

> 该开关部分 MIUI 版本要求「登录小米账号」，个别版本还要求插 SIM 卡。
> **先让使用者实际点一下看弹窗，不要凭「没有 SIM 卡」预先判定不可行**——实测某个 MIUI 12
> 只校验账号，无 SIM 卡也能开启。
>
> 确实开不了时的退路：① 临时插一张废 SIM（MIUI 只做存在性校验）；② 换非 MIUI 设备；
> ③ 改用基于 AccessibilityService 的工具（不走 `input` 注入，因而不受此限）。

**此限制 agent 无法绕过**——检测到就必须如实告知使用者，不能假装操作成功。

### 两个截图 API 的坐标系不同

| API | 输出 | 用途 |
|---|---|---|
| `mobile_take_screenshot` | **缩略图**（如 360×800），附 ×N 换算说明 | 人眼查看 |
| `mobile_save_screenshot` | **原始分辨率**（如 1080×2400） | 像素与坐标分析 |

做任何坐标计算必须用 `mobile_save_screenshot`。两套坐标混用会算出错误的点击位置，
而且错得很隐蔽——点得到屏幕上某处，只是不是你想要的那处。

### 截图前必须等动画结束

在转场 / 连锁动画播放途中截图，读到的是被特效覆盖的脏数据（实测：棋盘区域被过渡特效染成
单一色调，求解器据此输出了 112 个「合法走法」这种明显失真的结果）。

**识别信号**：结果数量突然暴增、或元素分布异常单一 = 截图时机不对，**重截，不要采信这份结果**。

### 所有 `mobile_*` 调用都必须显式传 `device`

MCP 没有「当前设备」的隐式概念。每次调用都要带 `device: "<serial>"`，
多设备在线时尤其如此——不允许静默选取第一台。
