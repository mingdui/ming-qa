# TODO

本文件记录**已知的待办项与未验证项**。

与 README 的「边界」不同，这里写的是「已经做了但还没验证」或「计划做但没做」的东西——
**不要把这里的项当成可用功能**。

最后更新：2026-11-04

## 未验证（代码与文档已就绪，但没有真实环境跑过）

| 项 | 现状 | 风险 |
|---|---|---|
| 方式③ `@mobilewright/test` 脚本资产 | 文档已描述，**从未执行** | Mobilewright 处于 0.0.x，API 可能变动 |
| `install-mobile-e2e --add-to-path` | 只有 dry-run 与单元测试 | 真实写注册表未验证 |
| `install-mobile-e2e --with-mobilewright` | 从未执行 | npm 安装路径未验证 |
| `@agent:` targetFile 约定 | 仅有单元测试 | 未在真实 spec-task 上验证 |
| mobilecli daemon 缓存提示 | 由实际踩坑反推 | 未验证「新用户照提示能否解决」 |

## 已知局限

- **只测过 2 个应用**：一个自绘引擎游戏（整屏只有 3 个无障碍节点）、一个电商类 demo
  （完整无障碍树）。H5 套壳、Flutter、React Native、WebView 混合渲染均未验证。
- **iOS 完全未支持**（需要 macOS + Xcode）。
- **无源码项目**：`qa-context-profiler` 与 `qa-code-reviewer` 基本为空，
  readiness 判定到不了「就绪」。这是**设计边界**，不是缺陷——ming-qa 假设你有代码仓库。

## 计划中

- 云真机接入（BrowserStack / Mobile Next Cloud 等）
- iOS 模拟器支持（阻塞于 macOS 环境）
- 把 `install/detect_e2e_*` 抽象成带 `platform` 参数的统一 driver 层
  （等第三个 driver 出现时再做，当前只有 web 与 Android 两个）

## 环境相关的已知坑（已修复，但值得记录）

- adb 装在 `~/.ming-qa/platform-tools` 时**不在 PATH**，而 mobile-mcp / mobilecli
  只认 PATH → 表现为「本地检测全绿、agent 里列不出设备」。用 `--add-to-path` 修。
- mobilecli 的 daemon 会**缓存「零设备」状态且不报错**（`status: ok`, `devices: []`）。
  adb 变得可见后必须 `mobilecli daemon stop` 才会重扫。
- MIUI 默认拦截 ADB 输入注入（`INJECT_EVENTS`），点击/滑动/按键全被拒但截图正常。
  需在开发者选项里开「USB调试(安全设置)」，此步**无法自动化**。
