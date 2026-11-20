# Changelog

本项目所有值得注意的变更都会记录在此文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- Android 真机 E2E 支持：`doctor` 新增条件化检测组（`mobile_adb` / `mobile_devices` / `mobile_mcp`）
  与 `--require-mobile-e2e` 开关；spec-task 新增 `platform` 字段；
  新增 `references/mobile-e2e-integration.md`
- 新增 `install-mobile-e2e` 命令：注册 mobile-mcp 到 `.mcp.json`，并按平台安装
  Android Platform Tools（包管理器不可用时回退到 Google 官方 CDN 下载）
- `install-mobile-e2e` 安装输出会打印 adb 落盘路径，并在 adb 不在 PATH 时明确提示——
  mobile-mcp 等第三方工具只认 PATH；新增 `--add-to-path` 写入用户 PATH（Windows，幂等）
- 移动端证据门禁：标记 `passed` 的移动端 task 必须有元素树快照、截图、logcat 三类证据的
  `manifest.json`，缺任何一类都会让 `assert-evidence-integrity` 失败（readiness 随之
  Incomplete / Not Ready）。新增 `assert-mobile-evidence` 单独校验
- 新增 `run-mobile-e2e`：执行 `npx mobilewright test` 并写与 `run-e2e` 同构的 run 证据
- `platform` 支持 `android-emulator`；`ios-sim` / `ios-real` 会被识别并进入 iOS 检测分支，
  不再有静默当成 web 的可能
- iOS 接入：`doctor` 在 macOS 上检查 Xcode / `simctl` / `idevice_id` / `xctrace`；
  非 macOS 明确失败。`install-mobile-e2e --with-ios` 输出 Xcode 人工准备步骤；
  移动端证据契约接受 iOS 的 `deviceLog` / `syslog` 日志键。
  **macOS 相关逻辑未实测**，见 `docs/TODO.md`
- 移动端证据清单新增 `locator` / `fallbackReason` / `durationMs`：截图兜底必须写原因；
  缺耗时直接失败。报告新增「移动端 E2E」区块，展示 task 数、证据清单数、累计/最长耗时、
  树定位与截图兜底数量
- 移动端 oracle 断言新增 `evidenceRefs` 映射：每条断言必须标明由元素树 / 截图 / 设备日志
  哪一类支撑；缺映射或断言未映射都会让 `assert-evidence-integrity` 失败
- 设备日志崩溃负向门禁：`FATAL EXCEPTION` / `ANR in` / `E/AndroidRuntime` / iOS
  uncaught exception 等命中时，即使三类证据齐全也不能判 `passed`
- run sidecar 新增 `durationMs`，`aggregate-runs` 同步聚合耗时
- `doctor` 的 `mobile_adb` / `mobile_devices` / `mobile_mcp` 在项目存在 Android task 时
  统一为阻断项，修正此前「设备项阻断、adb 项不阻断」的不一致
- 无源码移动端（黑盒 APK/IPA）工作流：`init-project --profile mobile`（跳过 Playwright/服务栈）、
  `collect-context --allow-non-git`（无 `.git` 时产出最小上下文并标注 non-git）、
  `analyze-risks --surface mobile`（输出移动端风险骨架而非关键词噪音）、
  `generate-spec-tasks --profile mobile-e2e`（只出 e2e，路径/命令留给实现阶段）、
  `coverage-balance --profile mobile`（e2e 100%、每级最少 1 条，不判 e2e 过重）
- 新增 `scripts/mobile_adb.py`：零依赖 ADB 驱动，封装中文输入法拦截、不可点击文本节点、
  `pm clear` 被拒后的降级链、多设备歧义；`save_evidence` 直接产出证据契约要求的四件套
- `install-mobile-e2e` 新增 `mobilecli` 与已授权设备发现步骤；`doctor` 新增 `mobile_cli`
  检查项——仅在 task 由 agent 直驱（`targetFile: "@agent:mobile-mcp"`）时阻断，脚本驱动不要求
- `doctor` 的 `mobile_mcp` 同样改为条件阻断：脚本驱动（`mobile_adb.py`）不再被迫注册
  mobile-mcp；只有 `@agent:mobile-mcp` 直驱时才要求注册并重启会话
- `doctor --ignore` 的豁免写入 `environment-checks.json`（`ignored: true` + `ignoreReason`，
  `status` 仍为 `blocked`），控制台标 `[WAIVED]`，报告不再把「知情放行」写成「已修复」
- `run-with-env` 新增 `--mobile` 与 `--timeout`：移动端 e2e 默认 300s，避免完整结算流程
  被 120s 默认超时截断
- **Mobilewright 成为移动端回归主引擎**：`generate-spec-tasks --profile mobile-e2e` 直接生成
  `tests/e2e-mobile/<module>/<case>.spec.ts` 与 `run-mobile-e2e` 命令；`install-mobile-e2e
  --with-mobilewright --bundle-id <包名>` 安装 `mobilewright` + `@mobilewright/test` 并生成
  `mobilewright.config.ts`；`doctor` 新增 `mobilewright_runtime`，`mobile_cli` 认项目内自带二进制
- `run-mobile-e2e` 补齐真机执行细节：修正 `--project=` 参数顺序、相对 POSIX spec 路径、
  测试级 `--test-timeout`（完整下单流程 240s）、执行前后禁用/恢复中文输入法、
  `--app-package` 后自动写元素树/截图/按 PID 过滤的 logcat 证据
- 新增 `references/mobilewright-regression.md`：记录 fill() 焦点竞争、IME 拦截、
  `clearData` 在 ColorOS 被拒、整机 logcat 误报等真机坑与处理方式
- `assert-script-implementation` 跳过已标记 blocked 的 task：产品缺口（如没有订单历史入口）
  不应被当成「脚本未实现」

### Changed

- **移动端证据必须绑定本轮执行**：`manifest.json` 新增 `runLog`（指向本次 sidecar 日志）、
  `captureWindow`（当前固定 `post-run`，说明是测试结束后的终态快照）、`capturedAt`，以及可选的
  `startedAt` / `finishedAt` / `package`。`assert-evidence-integrity --run <latest-run.json>` 会要求
  `runLog` 出现在本轮 `cases[].attempts[].logFile` 里，否则判 `mobile-evidence-run-unbound`。
  **升级后旧产物会变红**：证据要用新版 `run-mobile-e2e` 重跑一次才会带上 `runLog`；
  暂时不想重跑时别传 `--run`，或单独跑 `assert-mobile-evidence`（不传 `--run` 时不做绑定检查）
- `assert-completion` 的 `--min-specs-by-priority` 默认改为「不传」：不传时依次读 spec-tasks 根级的
  `minSpecsByPriority`、`generationProfile == "mobile-e2e"` 的每级 1 条、再回落到桌面默认 8/5/3/1。
  此前生成端写 1 条、校验端按 8 条判，同一份产物换个人重跑结论会翻转
- `run-mobile-e2e --timeout` 默认改为自动：按 `--test-timeout × (retries+1) + 120s` 计算；
  显式给的秒数小于这个最坏耗时时打一行提示并放宽，不再让测试跑一半被自家超时砍掉
- `run-mobile-e2e` 不再整轮禁用输入法：默认沿用 `mobile_adb.py` 的按次禁用/恢复，
  整轮禁用降级为显式开关 `--disable-ime`（配 atexit 兜底）。整轮禁用曾在 subprocess 被强杀时
  把设备留成没键盘的状态
- 报告的证据变成可点的：case 矩阵的执行日志、移动端证据墙的元素树 / 截图 / 设备日志 / manifest
  都是相对链接，证据墙直接嵌截图缩略图；未绑定 run 的清单会标注出来
- `README` 的依赖表述区分「工具自身零依赖」与「移动端可选外部工具（npx 调起）」

### Fixed

- 修复「跑挂了却写成通过」不拦：`assert-evidence-integrity` 现在比对 run 的 `finalOutcome` 与
  task 的 `executionStatus`，不一致报 `run-task-outcome-mismatch`
- 修复移动端断言映射形同盖章：`evidenceRefs` 里声明的证据类必须真的在 manifest 里采到
  （`mobile-evidence-ref-kind-not-captured`），两类证据的断言清单逐字相同时给出
  `mobile-evidence-refs-duplicated` 警告；未知 kind（如 `network`）不再被静默忽略
- 修复 `spec-tasks` 元数据漂移不报：`summary.total` / `summary.byLayer` / `perCase` 与 `tasks`
  对不上时报 `spec-tasks-metadata-mismatch`
- 修复 `run-mobile-e2e` 的 subprocess 超时异常未捕获（不写 sidecar、看不出发生过什么）：
  现在记为退出码 124 并照常写 sidecar 与证据
- 修复证据落盘失败被 `print` 吞掉：写 `.qa-agent/runs/<runId>.evidence-error.json`
- 修复 `--evidence-locator` 缺失导致 `locator` 恒为 `tree`：按截图兜底时可以正确声明

## [2.0.0] - 2026-09-16

首次公开发布。与内部使用的 1.1.x 相比，主要是分发方式与内部耦合的移除。

### Added

- 三条标准安装渠道：`npx skills add`、Claude Code 插件市场、从源码运行安装脚本
- `.claude-plugin/marketplace.json` 与 `plugin.json`，支持 Claude Code 插件市场
- GitHub Actions CI：3 平台 × 2 Python 版本跑测试，外加 shellcheck、
  插件清单校验、内部标识扫描
- `docs/` 下的安装、配置、架构三篇文档
- 中英双语 README
- MIT 许可证

### Changed

- **仓库结构**：8 个 skill 目录从仓库根迁移到 `skills/` 子目录，
  使其能被 `npx skills` 的容器目录扫描规则发现
- **通知机制**：webhook 从企业微信专用改为通用 markdown POST，
  兼容企业微信与钉钉；配置项 `notify.webhook` 默认留空
- **多模型交叉审查**：不再内置任何默认网关地址，必须通过
  `QA_AGENT_LLM_BASE_URL` 或 `--base-url` 显式配置
- `install.sh` / `install.ps1` 不再修改用户的 shell 配置文件或用户 PATH

### Removed

- **破坏性变更**：删除 `upgrade` 子命令。请改用各安装渠道自带的更新能力：
  `npx skills update` 或 `/plugin update ming-qa@ming-qa`
- 删除 `DEFAULT_WEBHOOK_URL` 常量。此前 `init-project` 生成的配置默认会向一个
  硬编码地址推送告警，现在不会向任何地址发送数据，除非用户显式配置
- 删除 `DEFAULT_BASE_URL` 的内网网关地址
- 删除 `latest.json` 与基于私有 CDN 的自更新机制
