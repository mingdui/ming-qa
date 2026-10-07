# 架构

## 组成

1 个顶层路由器 + 7 个阶段子 skill + 1 个 Python CLI。

| # | Skill | 职责 | 判断质量 |
|---|---|---|---|
| 0 | `quality-assurance-agent` | 路由器。按阶段调用子 skill，自己不写测试、不判质量 | — |
| 1 | `qa-context-profiler` | 收集仓库事实与环境证据 | 否 |
| 2 | `qa-risk-analyzer` | 识别高风险业务路径与必需验证点 | 否 |
| 3 | `qa-testcase-designer` | 生成中文业务用例，等待用户确认 | 否 |
| 4 | `qa-test-script-generator` | 把已确认用例转成 spec-task 与测试脚本 | 否 |
| 5 | `qa-test-runner` | 执行、分类失败、修最小根因、跑 completion 门禁 | 否 |
| 6 | `qa-code-reviewer` | 独立视角代码审查，产出 `code-review.json` | 是 |
| 7 | `qa-report-generator` | 汇总门禁产物，渲染报告，给出最终判定 | 是 |

前五个阶段只做收集、转换、执行，质量判断集中在阶段 6、7。

## 执行链路

```
阶段 0 上下文收集
   ↓
阶段 1 风险分析
   ↓
阶段 2 用例设计  ←── 唯一强制人工门禁：等你确认用例
   ↓
阶段 3 脚本生成
   ↓
阶段 4 执行与修复
   ↓
阶段 5 代码审查
   ↓
阶段 6 报告与最终判定
```

阶段 2 确认之后全部自动衔接，中间不需要人工介入。

## 门禁

每次阶段切换前校验上游产物，缺失不允许跳过。

| 门禁 | 产物 | 校验内容 |
|---|---|---|
| completion | `completion-check.json` | 所有 task 是否执行完并通过 |
| code-review | `code-review-check.json` | 是否存在 P0/P1 blocking 发现 |
| readiness | `readiness-check.json` | 汇总前两者，给出最终判定 |

最终判定分四档：**就绪 / 有条件就绪 / 未就绪 / 未完成**。

判定取 completion 与 code-review 的交集——两者都通过才是「就绪」。

## 产物目录

运行产物在目标项目的 `.qa-agent/` 下。

| 目录 | 内容 | git clone 后 |
|---|---|---|
| `cases/` | 已确认的长期用例 | ✅ |
| `spec-tasks/` | spec-task 蓝图（用例→脚本映射） | ✅ |
| `config/` | 项目 QA 配置 | ✅ |
| `profiles/` | 项目测试 profile | ✅ |
| `risk-rules/` | 风险规则 | ✅ |
| `fixtures/` | 账号/服务示例模板（脱敏后） | ✅ |
| `knowledge/` | 项目经验库 | ✅ |
| `reports/` | HTML 报告 | ✅ |
| `current/` | 本轮运行产物 | ❌ 需重新生成 |
| `runs/` | 执行日志与证据 | ❌ |
| `local/` | 本地账号密码 | ❌ |

**clone 后首次回归**：`current/` 已经不在了，先建目录再从 `cases/` 重新生成任务蓝图：

```bash
python "$QA_AGENT_DIR/scripts/qa_agent.py" init-project --repo .
python "$QA_AGENT_DIR/scripts/qa_agent.py" generate-spec-tasks \
  --cases .qa-agent/cases/<模块>.json --repo . \
  --output .qa-agent/current/test-spec-tasks.json
```

`tests/api/` 下的测试脚本不受影响。

## 数据流向

```
仓库代码 ─→ qa-context-profiler ─→ context.json
                                        ↓
用户需求 ─→ qa-risk-analyzer ────→ risk-analysis.json
                                        ↓
                              qa-testcase-designer
                                        ↓
                              test-cases.json  ←── 你在这里确认
                                        ↓
                           qa-test-script-generator
                                        ↓
                              test-spec-tasks.json
                                        ↓
                                qa-test-runner
                                        ↓
                    test-results + evidence + completion-check.json
                                        ↓
                               qa-code-reviewer
                                        ↓
                                 code-review.json
                                        ↓
                              qa-report-generator
                                        ↓
                    latest-report.html + readiness-check.json
```

## 外部依赖与降级

| 依赖 | 用途 | 不可用时 |
|---|---|---|
| Playwright 运行时 | 前端 E2E | 记录到 `environment-checks.json`；纯 API 验收不需要 |
| Android 真机 / mobile-mcp | 移动端 E2E | 受影响 task 标记 blocked，原因写入 blocker |
| MySQL MCP | 数据库资金/状态校验 | 降级为 API 替代验证，在 evidence 中标注信息损失 |
| LLM 网关 | 多模型交叉审查 | 跳过该阶段，其余流程不受影响 |
| 告警 webhook | 报告质量告警 | 不发送通知；发送失败只写 stderr，不阻断 |

所有外部地址都由用户显式配置，未配置时不发起网络请求。

## CLI 调用约定

`skills/quality-assurance-agent/scripts/qa_agent.py` 是命令语法的唯一真相来源。

先解析一次 skill 目录，之后所有命令写成 `python "$QA_AGENT_DIR/scripts/qa_agent.py" <cmd>`：

```bash
QA_AGENT_DIR="${QA_AGENT_CLI:-$(dirname "$(find ~/.claude/skills ~/.agents/skills ~/.codex/skills .claude/skills .agents/skills .codex/skills -maxdepth 2 -name SKILL.md -path '*quality-assurance-agent/*' 2>/dev/null | head -1)")}"
```

## 仓库布局

```
ming-qa/                            仓库根 = 插件根
├── skills/
│   ├── quality-assurance-agent/    主 skill + Python CLI
│   ├── qa-context-profiler/
│   ├── qa-risk-analyzer/
│   ├── qa-testcase-designer/
│   ├── qa-test-script-generator/
│   ├── qa-test-runner/
│   ├── qa-code-reviewer/
│   └── qa-report-generator/
└── docs/  .claude-plugin/  ...
```

仓库根同时是插件根。8 个 skill 平铺在 `skills/` 下——`npx skills` 扫描固定的容器目录名并向下查找，仓库根目录需要直接含 `SKILL.md` 才会被当作 skill。
