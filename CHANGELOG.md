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
