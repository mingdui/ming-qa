# CLI 命令参考（唯一真相来源，子 skill 依赖这里）

本节是完整的命令语法参考。子 skill 不再各自维护命令章节，而是通过引用"主 skill CLI 命令参考"获取语法。

## 通用工具（所有阶段可用）

```powershell
# 查看当前 QA 流程 manifest（产物路径和阶段完成状态）
python "$QA_AGENT_DIR/scripts/qa_agent.py"manifest --repo .

# 加载 .qa-agent/local/.env 并执行测试脚本（自动处理 CRLF/变量传递/日志）
python "$QA_AGENT_DIR/scripts/qa_agent.py"run-with-env --repo . --script <脚本相对路径> [--extra KEY=VAL]...

# 在 playwrightDir（默认 tests/）执行 Playwright spec，并写 run 证据（log+sidecar，供 aggregate-runs 自动聚合）
python "$QA_AGENT_DIR/scripts/qa_agent.py"run-e2e --repo . --spec tests/e2e/<module>/<case-id>.spec.ts [--extra KEY=VAL]...

# 执行 Mobilewright spec（Android 回归资产），写同构的 run 证据
python "$QA_AGENT_DIR/scripts/qa_agent.py"run-mobile-e2e --repo . --spec tests/e2e-mobile/<module>/<case-id>.spec.ts --case-id TC-P0-001 [--project android]

# 单独校验移动端三类证据（元素树 / 截图 / logcat）；assert-evidence-integrity 会自动合并
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-mobile-evidence --repo . --spec-tasks .qa-agent/current/test-spec-tasks.json

# 安全写入 JSON 文件（规避 Windows Write 工具的中文 U+FFFD 编码损坏）
python3 -c "..." | python "$QA_AGENT_DIR/scripts/qa_agent.py"safe-write-json <目标路径> --from-stdin

# 编码完整性检查
python "$QA_AGENT_DIR/scripts/qa_agent.py"check-mojibake <文件路径...> --strict
```

## 前置准备：布局与环境

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"init-project --repo .
python "$QA_AGENT_DIR/scripts/qa_agent.py"doctor --repo . --strict --check-services --json .qa-agent/current/environment-checks.json
```

`init-project --profile mobile` 跳过 Playwright 模板/运行时与本地服务栈骨架，只建 QA 目录、配置和空 services；适用于「被测对象是已安装的第三方 APK/IPA，仓库里没有源码」的场景。

`doctor --ignore <检查名>` 的豁免会写进 `environment-checks.json`：该项 `status` 仍是 `blocked`，另带 `ignored: true` 与 `ignoreReason`——报告层据此区分「已修复」和「用户知情放行」。

> Playwright 运行时（`@playwright/test` + `playwright.config.js` + 浏览器）统一落盘到
> `qa-agent.config.yaml` 的 `playwright.dir`（默认 `tests/`），避免根目录 lockfile 干扰前端
> Next.js workspace root 推断（导致动态路由 404）。`install-playwright-runtime` 自动装到该目录，
> `run-e2e` 在该目录执行 spec。`playwright.config.js` 的 `testDir` 为 `./e2e`（相对 playwrightDir）。

> **移动端 E2E**：scope 涉及 `platform: android-real` / `android-emulator` 的 task 时，执行
> `install-mobile-e2e --repo . --with-mobilewright --bundle-id <包名>`（注册 mobile-mcp、
> 安装 Android Platform Tools、安装 `mobilewright` + `@mobilewright/test` 并生成
> `mobilewright.config.ts`）。回归执行用 `run-mobile-e2e --app-package <包名>`，
> 会同时写 run sidecar 与元素树/截图/按 PID 过滤的 logcat。
> adb 优先用平台包管理器（winget / brew / apt），都没有时回退到 Google 官方 CDN 下载到
> `~/.ming-qa/platform-tools`。**下载回退后必须把该目录加入 PATH**——mobile-mcp 等第三方工具
> 只认 PATH，不会去 ming-qa 自管目录找 adb（用 `--add-to-path` 自动写入，需重开会话生效）。
> 装完仍需人工完成手机上的 USB 授权，命令会作为 `manualSteps` 报出来。
>
> **iOS**：Xcode 无法自动安装。声明了 `ios-sim` / `ios-real` 时，安装命令会输出人工步骤
> （`--with-ios` 可显式要求）；`doctor` 在 macOS 上检查 Xcode / `simctl` / 真机，
> 非 macOS 直接失败。iOS 相关逻辑未在 macOS 实测。
> 完整说明见 `references/mobile-e2e-integration.md`。

## 阶段 0：上下文

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"collect-context --repo . --scope uncommitted --output .qa-agent/current/context.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"index-existing-cases --repo . --output .qa-agent/current/existing-case-index.json
```

工作区没有 `.git`（纯移动端/黑盒验收）时加 `--allow-non-git`：产出的 `context.json` 带 `gitAvailable: false`、`scope.mode: "non-git"`，git diff 字段留空，不编造变更文件。不带该 flag 仍会报错——静默产出空上下文会让下游误以为收集过。

## 阶段 1：风险分析

```powershell
# 验收场景（推荐）：限定到具体模块文件，避免全仓库关键词扫描污染 affectedFiles
python "$QA_AGENT_DIR/scripts/qa_agent.py"analyze-risks --repo . \
  --context .qa-agent/current/context.json \
  --existing-index .qa-agent/current/existing-case-index.json \
  --module "src/main/java/.../OpenBoxServiceImpl.java,src/main/java/.../OpenBoxController.java" \
  --output .qa-agent/current/risk-analysis.json

# 开发场景（无 git diff 时回退到全仓库扫描）
python "$QA_AGENT_DIR/scripts/qa_agent.py"analyze-risks --repo . --context ... --existing-index ... --output ...
```

无源码移动端（已安装的 APK/IPA）用 `--surface mobile`：不做关键词扫描，输出固定的移动端风险骨架（状态残留、金额一致、越权、负向路径、跨页数据同步），每条 `requiresE2E: true`、`affectedFiles: []`，并在 `summary.note` 标注无源码边界。骨架不替代业务风险分析，仍需结合真机证据逐条落地。

`--module` 接受逗号分隔的文件路径、目录路径或 glob 表达式。验收场景中不带此参数将导致 `affectedFiles` 混杂全仓库无关文件。

## 阶段 2：用例设计

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"merge-existing-cases --repo . --generated .qa-agent/current/test-cases.generated.json --existing-index .qa-agent/current/existing-case-index.json --output .qa-agent/current/test-cases.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"validate-cases --cases .qa-agent/current/test-cases.json --summary --check-mojibake --strict-language
python "$QA_AGENT_DIR/scripts/qa_agent.py"render-cases --cases .qa-agent/current/test-cases.json --output .qa-agent/current/test-cases.html
python "$QA_AGENT_DIR/scripts/qa_agent.py"review-cases --cases .qa-agent/current/test-cases.json --context .qa-agent/current/context.json --output .qa-agent/current/model-review.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"check-mojibake .qa-agent/current/test-cases.json .qa-agent/current/test-cases.html .qa-agent/current/model-review.json --strict
# 用户确认后：
python "$QA_AGENT_DIR/scripts/qa_agent.py"promote-cases --cases .qa-agent/current/test-cases.json --repo . --module <模块名>
```

## 阶段 3：脚本生成

```powershell
# 完整测试金字塔（默认）：每条用例按 unit/integration/api/e2e 逐级递减拆解
python "$QA_AGENT_DIR/scripts/qa_agent.py"generate-spec-tasks --cases .qa-agent/current/test-cases.json --repo . --output .qa-agent/current/test-spec-tasks.json

python "$QA_AGENT_DIR/scripts/qa_agent.py"coverage-balance --spec-tasks .qa-agent/current/test-spec-tasks.json --output .qa-agent/current/coverage-balance.json --strict

# 无源码移动端：只生成 e2e，脚本路径/命令留给 mobile_adb 实现阶段填写
python "$QA_AGENT_DIR/scripts/qa_agent.py"generate-spec-tasks --cases .qa-agent/current/test-cases.json --repo . --profile mobile-e2e --output .qa-agent/current/test-spec-tasks.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"coverage-balance --spec-tasks .qa-agent/current/test-spec-tasks.json --profile mobile --output .qa-agent/current/coverage-balance.json --strict

python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-oracle-mapping --risk-analysis .qa-agent/current/risk-analysis.json --spec-tasks .qa-agent/current/test-spec-tasks.json
```

默认最小 task 数 P0=8/P1=5/P2=3/P3=1、比例 unit 60%/integration 20%/api 15%/e2e 5%。若要收窄范围，用 `--min-specs-by-priority` 降低每优先级最小任务数、或用 `--ratio` 调整层级配比，但不要把某一层砍成 0——验收同样需要单元测试。

`coverage-balance` 的 `--ratio` / `--min-specs-by-priority` 默认读 spec-tasks 根级的 `targetRatio` / `minSpecsByPriority`，无需手动传等价参数。

**非 Maven/npm 项目（Python、Go、Gradle 等）**：识别为 `generic`，生成的 task 带 `needsManualCommand: true`、`command`/`targetFile` 为空——实现阶段由 AI 按项目实际工具链填入可执行的测试命令。执行、证据链、假通过检测照常生效：`assert-completion` 会拦截「声称通过但 command 为空」的假通过（`generic-command-missing`）。

`assert-oracle-mapping` 校验每条 P0/P1 风险的 requiredAssertions 都映射到某个 spec-task 的 oracle/assertions。

## 阶段 4：执行与修复

```powershell
# 移动端回归（Mobilewright）：执行单条 spec，并写证据
python "$QA_AGENT_DIR/scripts/qa_agent.py" run-mobile-e2e --repo .   --spec tests/e2e-mobile/<module>/<case-id>.spec.ts   --case-id TC-P0-001 --task-id SPEC-TC-P0-001-E2E-001   --app-package <bundle-id> --project android   --test-timeout 240000
```

`--test-timeout` 是单条测试毫秒数（透传 mobilewright），`--retries` 透传重试次数。
`--timeout` 是包裹整个子进程的秒数，**默认不写**——不写时按 `test-timeout × (retries+1) + 120s` 自动算；
显式写了但小于这个最坏耗时，会打印一行提示并放宽到最坏耗时，而不是让测试跑到一半被自家超时砍掉。
子进程超时记为退出码 `124`，照样写 run sidecar 与证据（失败也要留痕）。

证据采集：

- `--app-package` 提供后，执行结束抓元素树 / 截图 / logcat 并写 `manifest.json`；manifest 带 `runLog`
  指向本次 sidecar 的日志，`captureWindow: "post-run"` 说明这是**测试结束后的终态快照**
- `--evidence-locator tree|screenshot` 声明本次取证方式：`tree` 表示按元素树定位，`screenshot` 表示按截图兜底；
  按截图兜底时必须同时给 `--fallback-reason`，否则证据门禁会问「为什么没用元素树」
- IME 默认**按次**禁用/恢复（见 `mobile_adb.py`），不再整轮禁用——整轮禁用会在 subprocess 被强杀时
  把设备留成没有输入法的状态。确实需要整轮禁用时显式加 `--disable-ime`，命令会注册 atexit 兜底恢复
- 证据落盘失败不吞掉：写 `.qa-agent/runs/<runId>.evidence-error.json`，`assert-evidence-integrity` 会据此出 finding

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"run-loop --repo . --config .qa-agent/config/qa-agent.config.yaml --output .qa-agent/runs/latest-run.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"update-results --cases .qa-agent/current/test-cases.json --run .qa-agent/runs/latest-run.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-completion --cases .qa-agent/current/test-cases.json --spec-tasks .qa-agent/current/test-spec-tasks.json --priorities P0,P1,P2 --output .qa-agent/current/completion-check.json
```

`--min-specs-by-priority` **默认不传**。不传时按三级回落取最小 task 数：显式参数 > spec-tasks 根级的
`minSpecsByPriority` > `generationProfile == "mobile-e2e"` 时每级 1 条 > 桌面默认 P0=8/P1=5/P2=3/P3=1。
也就是「生成器写进产物的口径」就是校验器默认采用的口径——同一条流水线不会因为少传一个参数就翻结论。
同一份 spec-tasks 还会被检查元数据自洽：`summary.total` / `summary.byLayer` / `perCase` 与 `tasks` 数组
对不上时报 `spec-tasks-metadata-mismatch`，因为那基本等于「产物生成后被改过」。

## 阶段 5：代码审查

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-code-review --code-review .qa-agent/current/code-review.json --output .qa-agent/current/code-review-check.json
```

## 阶段 6：报告生成

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"aggregate-runs --repo . --output .qa-agent/current/latest-run.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-evidence-integrity --cases .qa-agent/current/test-cases.json --spec-tasks .qa-agent/current/test-spec-tasks.json --run .qa-agent/current/latest-run.json --code-review .qa-agent/current/code-review.json --output .qa-agent/current/evidence-integrity-check.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"render-report --cases .qa-agent/current/test-cases.json --run .qa-agent/current/latest-run.json --spec-tasks .qa-agent/current/test-spec-tasks.json --completion-check .qa-agent/current/completion-check.json --risk-analysis .qa-agent/current/risk-analysis.json --code-review .qa-agent/current/code-review.json --output .qa-agent/reports/latest-report.html
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-report-freshness --report .qa-agent/reports/latest-report.html --cases .qa-agent/current/test-cases.json --spec-tasks .qa-agent/current/test-spec-tasks.json --completion-check .qa-agent/current/completion-check.json --risk-analysis .qa-agent/current/risk-analysis.json --code-review .qa-agent/current/code-review.json --output .qa-agent/current/report-freshness-check.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-readiness --completion-check .qa-agent/current/completion-check.json --code-review .qa-agent/current/code-review.json --evidence-integrity-check .qa-agent/current/evidence-integrity-check.json --report .qa-agent/reports/latest-report.html --report-freshness-check .qa-agent/current/report-freshness-check.json --output .qa-agent/current/readiness-check.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"render-report --cases .qa-agent/current/test-cases.json --run .qa-agent/current/latest-run.json --spec-tasks .qa-agent/current/test-spec-tasks.json --completion-check .qa-agent/current/completion-check.json --risk-analysis .qa-agent/current/risk-analysis.json --code-review .qa-agent/current/code-review.json --readiness-check .qa-agent/current/readiness-check.json --output .qa-agent/reports/latest-report.html
python "$QA_AGENT_DIR/scripts/qa_agent.py"check-mojibake .qa-agent/reports/latest-report.html .qa-agent/current/completion-check.json .qa-agent/current/readiness-check.json --strict
# 归档报告：渲染时顺带出一份时间戳副本（<module>-<runType>-<时间戳>.html）
python "$QA_AGENT_DIR/scripts/qa_agent.py" render-report --cases .qa-agent/current/test-cases.json --run .qa-agent/current/latest-run.json --spec-tasks .qa-agent/current/test-spec-tasks.json --completion-check .qa-agent/current/completion-check.json --risk-analysis .qa-agent/current/risk-analysis.json --code-review .qa-agent/current/code-review.json --readiness-check .qa-agent/current/readiness-check.json --module <module> --run-type acceptance --output .qa-agent/reports/latest-report.html
```

`assert-completion` / `assert-code-review` / `assert-readiness` 通过后，用 `render-report` 渲染
`latest-report.html`；带 `--module` 与 `--run-type` 时同目录多写一份带时间戳的归档副本
（`latest-report.html` 始终指向最近一次，归档副本留给历史对比）。

`assert-evidence-integrity --run` 不只是一份统计：给了 run 数据之后，移动端 `manifest.runLog` 必须能在
本轮 `latest-run.json` 的 `attempts[].logFile` 里找到，找不到就判 `mobile-evidence-run-unbound`——历史那次的
截图再漂亮也不能替这一次作证。没给 `--run` 时这条检查跳过（老产物仍可单跑）。同理 `run-task-outcome-mismatch`
比对的是 run 的 `finalOutcome` 与 task 的 `executionStatus`：跑挂了却写成 passed，两条都会亮红。

报告里的证据是可点的：`render-report` 会给每条 case 的执行日志、以及移动端证据墙上的元素树 / 截图 /
设备日志 / manifest 生成相对链接（报告放在 `.qa-agent/reports/` 时形如 `../runs/<runId>/mobile/<module>/screen.png`），
证据墙直接嵌截图缩略图。报告搬走或用浏览器以外的方式打开时，链接会失效——它是给人当场核对用的，不是分发包。
