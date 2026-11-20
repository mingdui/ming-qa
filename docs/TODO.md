# TODO

本文件记录**已知的待办项与未验证项**。

与 README 的「边界」不同，这里写的是「已经做了但还没验证」或「计划做但没做」的东西——
**不要把这里的项当成可用功能**。

最后更新：2026-11-20

## 未验证（代码与文档已就绪，但没有真实环境跑过）

| 项 | 现状 | 风险 |
|---|---|---|
| Mobilewright 回归（`tests/e2e-mobile/**/*.spec.ts`） | 已在 PLG110 / Android 17 真机跑通 7 条业务用例（含完整结算下单），并接入证据门禁 | 仍是 0.0.x；实测 0.0.70。升级需锁版本并先跑冒烟；iOS 真机需 macOS |
| Android 模拟器（`android-emulator`） | 与真机共用 adb 通路，**未单独实测** | 模拟器与真机的输入注入行为可能有差异 |
| iOS（`ios-sim` / `ios-real`） | 宿主检测、证据契约、人工步骤已实现；**未在 macOS 实测** | `simctl` / `xctrace` 输出解析、mobile-mcp 的 iOS 驱动、Mobilewright `--project ios` 都未验证；`xctrace` 真机列表靠机型名排除 Mac，重命名过的 Mac 可能被误判 |
| `install-mobile-e2e --add-to-path` | 逻辑有单元测试（假注册表），真实写注册表**未验证** | 只改用户级 PATH，需重开会话才生效 |
| `install-mobile-e2e --with-mobilewright` | 只有 dry-run 与单元测试 | npm 安装路径未验证 |
| `@agent:` targetFile 约定 | 有单元测试 | 未在真实 spec-task 上验证 |
| 移动端证据门禁 | 三类证据、`locator` / `fallbackReason` / `durationMs`、`evidenceRefs` 映射、日志崩溃扫描、`runLog` 绑定本轮执行均有单元测试 | 真实 mobile-mcp 产出的元素树 / 截图 / 日志是否都能落盘并写进 manifest，未验证；崩溃特征扫描按行精确匹配，真实日志的误报率未知 |
| mobilecli daemon 缓存提示 | 由实际踩坑反推 | 未验证「新用户照提示能否解决」 |
| `mobile_adb.py` 内置驱动 | 单元测试覆盖点击祖先、输入法启停、checkbox 回退、重置降级、证据落盘 | 只在 PLG110 / Android 17 的一台真机上跑过完整流程；其他 ROM 的输入法与重置菜单可能不同 |
| `analyze-risks --surface mobile` 风险骨架 | 输出固定 5 类移动端风险，`requiresE2E: true` | 骨架是通用项，尚未按真实业务补充；直接使用会漏掉应用特有风险 |

## 已知局限

- **只测过 2 个应用**：一个自绘引擎游戏（整屏只有 3 个无障碍节点）、一个电商类 demo
  （完整无障碍树）。H5 套壳、Flutter、React Native、WebView 混合渲染均未验证。
- **iOS 需要 macOS + Xcode，且未实测**。代码路径已接入：非 macOS 上 `doctor` 会明确失败，
  不会静默降级或假装可用。真机/模拟器驱动由 `mobile-mcp` / `Mobilewright` 负责，
  ming-qa 只做宿主检测、平台接入与证据契约。
- **移动端证据必须由执行阶段写 manifest**：元素树 / 截图 / logcat 就算都在磁盘上，
  没写进 `.qa-agent/runs/<run-id>/mobile/<scope>/manifest.json` 也会被判缺失。
  这是刻意的——没有清单就无法把证据和 case 对应起来。
- **无源码项目**：已提供 `--profile mobile` / `--allow-non-git` / `--surface mobile` /
  `--profile mobile-e2e` 一组黑盒路径，上下文与风险阶段不再空转；但 `qa-code-reviewer`
  仍然没有代码可审，readiness 判定到不了「就绪」。这是**设计边界**，不是缺陷——
  无源码验收只能证明「真机上这条流程走得通」，不能证明实现质量。

## 计划中

- 云真机接入（BrowserStack / Mobile Next Cloud 等）
- iOS 验证（阻塞于 macOS 环境）：在 Mac 上跑通 `doctor`、mobile-mcp 直驱、`run-mobile-e2e --project ios`
- 把 `install/detect_e2e_*` 抽象成带 `platform` 参数的统一 driver 层
  （等第三个 driver 出现时再做，当前只有 web 与 Android 两个）

## 代码结构债（已知，明确未做）

这三项是 2026-11-20 那轮 review 里指出、但**故意没和门禁修复同一批做**的：它们改的是形状，
不是判定。改错了不会亮红，只会悄悄坏掉，所以要有测试覆盖之后单独动手。

| 项 | 现状 | 为什么先不动 |
|---|---|---|
| `scripts/qa_agent.py` 单文件约 1.3 万行、374 个顶层函数、53 个子命令（`doctor` 596 行、`build_parser` 536 行） | 只有一个 `qa_core/project_manifest.py` 被拆出去 | 拆分本身不改变行为，但会移动几百个符号；在门禁刚改完的同一个提交里做，出问题时无法判断是新逻辑错还是搬错了 |
| 报告渲染区有 23 处 `# === 项目增强函数 (from project) ===` 标记 | 从内部项目搬来的代码块，函数名与调用点都是项目特定措辞 | 要连同内容一起判断「这条规则还成立吗」，无法机械删除；应随拆分一起做 |
| 单设备假设：`_detect_single_device` 遇多台设备直接报歧义，`mobilewright.config.ts` 里 `workers: 1` | 只支持「一台设备跑一条流」 | 多设备要改执行编排（分片、并发、证据目录按设备分区），属于新特性而非修复 |

已解决的结构问题（留档，避免重复讨论）：

- 三个 driver 各写一套执行逻辑 → `run-e2e` 与 `run-mobile-e2e` 已共用
  `_execute_and_record`（子进程 + 超时 + sidecar + 「没匹配到测试不记账」），
  `mobile_adb.py` 保持独立底层（它是 adb 命令封装，不跑 runner）
- 静默吞异常：证据落盘失败改为写 `<runId>.evidence-error.json`，找不到 adb / 没有唯一设备
  各有一条明确提示，不再让「设备连着却没证据」变成谜


## 环境相关的已知坑（已修复，但值得记录）

- adb 装在 `~/.ming-qa/platform-tools` 时**不在 PATH**，而 mobile-mcp / mobilecli
  只认 PATH → 表现为「本地检测全绿、agent 里列不出设备」。用 `--add-to-path` 修。
- mobilecli 的 daemon 会**缓存「零设备」状态且不报错**（`status: ok`, `devices: []`）。
  adb 变得可见后必须 `mobilecli daemon stop` 才会重扫。
- MIUI 默认拦截 ADB 输入注入（`INJECT_EVENTS`），点击/滑动/按键全被拒但截图正常。
  需在开发者选项里开「USB调试(安全设置)」，此步**无法自动化**。
