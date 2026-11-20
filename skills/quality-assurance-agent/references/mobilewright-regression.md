# Mobilewright 回归层实践

本文记录把 Mobilewright 作为**移动端回归主引擎**的落地方式，以及真机上踩过的坑。
探索层仍然用 agent + mobile-mcp；两者是并列通道，不是自动流水线。

## 定位

| 层 | 工具 | 用途 |
|---|---|---|
| 探索 | agent + mobile-mcp | 新 App / 新版本摸流程、找选择器、确认边界 |
| 回归 | **Mobilewright** | 每次构建/回归跑固定 spec，无 LLM 在环 |
| 兜底 | `mobile_adb.py` | 没有 Node/MCP 时的 adb 直连 |

Mobilewright 0.0.x，API 可能变动；升级后先跑 `npx mobilewright doctor` 和一条冒烟 spec。

## 安装与配置

```bash
npm install --save-dev mobilewright @mobilewright/test
npx mobilewright doctor --category android
npm exec -- mobilewright devices
```

`mobilecli` 由 `mobilewright` 包自带（`node_modules/@mobilenext/mobilecli-*`），
**不需要单独安装**。`doctor` 的 `mobile_cli` 检查认这个来源。

最小配置：

```ts
import { defineConfig } from 'mobilewright';

export default defineConfig({
  testDir: './tests/e2e-mobile',
  timeout: 240_000,          // 完整下单流程约 2 分钟；90s 会截断
  retries: 1,
  workers: 1,
  viewTree: 'on-failure',
  reporter: 'list',
  projects: [{
    name: 'android',
    use: {
      platform: 'android',
      bundleId: 'com.example.app',
      deviceId: process.env.ANDROID_SERIAL || '<serial>',
    },
  }],
});
```

## 执行

```bash
# 单条
npx mobilewright test tests/e2e-mobile/app/tc-p0-001.spec.ts --project=android

# 整目录
npx mobilewright test tests/e2e-mobile/app --project=android
```

- `--project` 必须用 `=` 形式，且 **spec 在前**：`--project android <spec>` 会被当成两个 project 名，报
  `Project(s) "<spec>" not found`。
- spec 参数要用**相对路径 + 正斜杠**；绝对 Windows 路径（反斜杠）会被当成文件过滤正则，报
  `No tests found`。
- CLI 的 `--timeout` 是**毫秒**，且会覆盖 config 的 `timeout`。

## 真机坑与处理

### 1. `fill()` 把文本打进上一个字段

Mobilewright 的 `fill()` 是「点击元素中心 → 给当前焦点控件输入文本」。焦点切换有延迟，
连续填多个输入框时，文本会落进**上一个**字段（实测城市的值写进了地址二）。

处理：先显式确认焦点，再 fill，并回读值。

```ts
async function fillField(screen, testId, value) {
  const field = screen.getByTestId(testId);
  await field.scrollIntoViewIfNeeded({ maxSwipes: 5 });
  for (let i = 0; i < 3; i += 1) {
    await field.tap({ timeout: 5_000 });
    try {
      await expect(field).toBeFocused({ timeout: 1_500 });
      break;
    } catch { /* retry */ }
  }
  await expect(field).toBeFocused({ timeout: 5_000 });
  await field.fill(value);
  await expect(field).toHaveValue(value);
}
```

### 2. 中文输入法拦截 `input text`

微信/搜狗输入法下，点击成功但文本不进去。两种处理方式，按顺序优先第一种：

1. **按次禁用**（默认）：`mobile_adb.py` 的 `type_text` 在每次输入前禁用当前 IME、输入后恢复。
   粒度小，进程被杀最多留一条没打进去的文本，不会把设备搞成没键盘。
2. **整轮禁用**（显式开启）：`run-mobile-e2e --disable-ime`，执行前禁用所有 IME、结束后恢复
   （`_mobile_ime_snapshot` / `_restore_mobile_imes`），并注册 atexit 兜底。

整轮禁用确实能覆盖「一批字段连续输入」的场景，但它曾把设备留在没有输入法的状态——subprocess 被
强杀时 `finally` 不执行。所以现在默认不这么做：**除非你确认整轮禁用的收益大于风险，否则不要加这个开关**。
直接跑 `npx mobilewright test` 时两套都不存在，得自己处理。

### 3. `app.clearData()` 在 Android 17 / ColorOS 被拒

日志：`SecurityException: ... does not have permission CLEAR_APP_USER_DATA`。
不要依赖 `clearData()`；用 App 自带的重置菜单（本例：Reset App State → RESET APP → OK），
再回 Catalog。`pm clear` 同样被拒。

### 4. 整机 logcat 触发崩溃误报

`adb logcat -d` 会带上其他 App 的历史崩溃。证据日志必须按被测进程过滤：

```bash
pid=$(adb -s <serial> shell pidof <package> | tr -d '\r')
adb -s <serial> logcat -d -t 500 --pid "$pid"
```

`mobile_adb.MobileAdb.logcat(package=...)` 已实现；`run-mobile-e2e --app-package` 会传进去。

### 5. 屏幕外输入框

不滚动直接 `fill()` 会点到屏幕外坐标。每个字段先 `scrollIntoViewIfNeeded()`；
键盘弹出会遮住下一行，收键盘后再滚动下一个。注意 `keyboard.isVisible()` 在部分国产
输入法上返回 false（明明键盘在），所以按次禁用 IME 比判断键盘状态更可靠。

## 与 ming-qa 的证据链

```bash
python "$QA_AGENT_DIR/scripts/qa_agent.py" run-mobile-e2e \
  --repo . --spec tests/e2e-mobile/app/tc-p0-001.spec.ts \
  --case-id TC-P0-001 --task-id SPEC-TC-P0-001-E2E-001 \
  --app-package com.example.app --project android
```

该命令：

1. 相对路径执行 `npx mobilewright test <spec> --project=android`；
2. 写 `runs/run-*.log` + `.meta.json`（与 `run-e2e` 同构）；
3. 传 `--app-package` 时再用 adb 抓元素树 / 截图 / 按 PID 过滤的 logcat，
   写 `.qa-agent/runs/<run-id>/mobile/<scope>/manifest.json`。manifest 里的 `runLog` 指向
   **本次** sidecar 日志，`captureWindow: "post-run"` 说明这是测试结束后的终态快照——
   断言那一刻的界面，只能靠 mobilewright 自己的 `viewTree: 'on-failure'` 产物。

之后的门禁与 adb 脚本完全一致：`aggregate-runs` → `update-results` →
`assert-evidence-integrity` → `assert-mobile-evidence` → `assert-completion`。

注意 `assert-evidence-integrity --run <latest-run.json>` 会要求移动端 manifest 绑定本轮执行：
`manifest.runLog` 必须出现在本轮 `cases[].attempts[].logFile` 里。所以**每次重跑都要重新生成证据**，
把上一轮的 manifest 挪过来会被判 `mobile-evidence-run-unbound`。这一条没有开关，是刻意的。

## 跨项目检查清单

换一个 App / 团队时，按这组顺序排查，能避开本次两轮执行里踩过的绝大多数坑：

1. **工具链**：Node ≥ 22.12；`mobilewright` 版本写进 lockfile；`mobilecli` 由包自带，
   不要另装；`doctor` 的 `ANDROID_HOME` 缺失**不等于跑不了**——只要 `adb` 在 PATH
   且 `mobilewright devices` 能列出设备，真机测试仍可执行。
2. **设备**：显式固定 serial（多设备时绝不静默取第一台）；中文输入法默认按次禁用
   （`--disable-ime` 才整轮禁用）。不要依赖 `keyboard.isVisible()`，部分国产输入法会返回 false。
3. **状态**：真机不会为每个 spec 隔离状态；每个 spec 自己 `resetApp`。优先用 App 自带
   重置菜单，`clearData` / `pm clear` 在 Android 17 / ColorOS 会被拒。
4. **spec**：相对 POSIX 路径；`--project=<name>` 放在 spec 之后；单条测试超时用毫秒，
   完整结算流程给 240000。
5. **表单**：`fill()` 前确认 `toBeFocused()`，填完回读 `toHaveValue()`；屏幕外字段先
   `scrollIntoViewIfNeeded()`。
6. **证据**：logcat 必须按被测进程 PID 过滤；失败/无测试时不要伪造 manifest；
   汇总时每个 task 只取最新 PASS 清单。
7. **门禁**：mobile profile 用 `coverage-balance --profile mobile`；blocked task 跳过
   实现门禁，但 completion 仍会判 `case-not-verified`——这是刻意的诚实判定。
8. **边界**：先记录被测包名 + 版本号；如果设备上是旧构建，报告必须写明
   「验收对象是已安装 APK，不是仓库 HEAD 构建产物」。

## 已知边界

- Mobilewright 仍是 0.0.x；升级前先锁版本并跑冒烟。
- iOS 真机需要 macOS + Xcode；Windows 上只能跑 Android。
- 复杂逻辑用 Playwright 风格 TS 表达；比 YAML 灵活，但维护成本高于纯声明式。
- 真机执行不隔离设备状态；每个 spec 必须自己保证前置状态（本仓库用 `resetApp`）。
