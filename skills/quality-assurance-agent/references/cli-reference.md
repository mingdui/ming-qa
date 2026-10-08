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

> Playwright 运行时（`@playwright/test` + `playwright.config.js` + 浏览器）统一落盘到
> `qa-agent.config.yaml` 的 `playwright.dir`（默认 `tests/`），避免根目录 lockfile 干扰前端
> Next.js workspace root 推断（导致动态路由 404）。`install-playwright-runtime` 自动装到该目录，
> `run-e2e` 在该目录执行 spec。`playwright.config.js` 的 `testDir` 为 `./e2e`（相对 playwrightDir）。

> **Android 真机 E2E**：scope 涉及 `platform: android-real` 的 task 时，执行
> `install-mobile-e2e --repo .`（注册 mobile-mcp 到 `.mcp.json` + 安装 Android Platform Tools）。
> adb 优先用平台包管理器（winget / brew / apt），都没有时回退到 Google 官方 CDN 下载到
> `~/.ming-qa/platform-tools`。**下载回退后必须把该目录加入 PATH**——mobile-mcp 等第三方工具
> 只认 PATH，不会去 ming-qa 自管目录找 adb（用 `--add-to-path` 自动写入，需重开会话生效）。
> 装完仍需人工完成手机上的 USB 授权，命令会作为 `manualSteps` 报出来。
> 完整说明见 `references/mobile-e2e-integration.md`。

## 阶段 0：上下文

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"collect-context --repo . --scope uncommitted --output .qa-agent/current/context.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"index-existing-cases --repo . --output .qa-agent/current/existing-case-index.json
```

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

python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-oracle-mapping --risk-analysis .qa-agent/current/risk-analysis.json --spec-tasks .qa-agent/current/test-spec-tasks.json
```

默认最小 task 数 P0=8/P1=5/P2=3/P3=1、比例 unit 60%/integration 20%/api 15%/e2e 5%。若要收窄范围，用 `--min-specs-by-priority` 降低每优先级最小任务数、或用 `--ratio` 调整层级配比，但不要把某一层砍成 0——验收同样需要单元测试。

`coverage-balance` 的 `--ratio` / `--min-specs-by-priority` 默认读 spec-tasks 根级的 `targetRatio` / `minSpecsByPriority`，无需手动传等价参数。

**非 Maven/npm 项目（Python、Go、Gradle 等）**：识别为 `generic`，生成的 task 带 `needsManualCommand: true`、`command`/`targetFile` 为空——实现阶段由 AI 按项目实际工具链填入可执行的测试命令。执行、证据链、假通过检测照常生效：`assert-completion` 会拦截「声称通过但 command 为空」的假通过（`generic-command-missing`）。

`assert-oracle-mapping` 校验每条 P0/P1 风险的 requiredAssertions 都映射到某个 spec-task 的 oracle/assertions。

## 阶段 4：执行与修复

```powershell
python "$QA_AGENT_DIR/scripts/qa_agent.py"run-loop --repo . --config .qa-agent/config/qa-agent.config.yaml --output .qa-agent/runs/latest-run.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"update-results --cases .qa-agent/current/test-cases.json --run .qa-agent/runs/latest-run.json
python "$QA_AGENT_DIR/scripts/qa_agent.py"assert-completion --cases .qa-agent/current/test-cases.json --spec-tasks .qa-agent/current/test-spec-tasks.json --priorities P0,P1,P2 --min-specs-by-priority P0=1,P1=1,P2=1 --output .qa-agent/current/completion-check.json
```

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
# 归档报告（手动生成时间戳副本）：
python -c "import shutil,datetime;ts=datetime.datetime.now().strftime('%Y%m%d-%H%M%S');shutil.copy2('.qa-agent/reports/latest-report.html',f'.qa-agent/reports/{module}-{runType}-{ts}.html')"
```

`assert-completion` / `assert-code-review` / `assert-readiness` 通过后，用 `render-report` 渲染
`latest-report.html`，再用 `python shutil.copy2` 生成带时间戳的归档副本。
