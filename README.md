# QA Agent

中文文档 | [English](README.en.md)

端到端 QA 验收工具包，以 **Agent Skill** 形式分发，支持 Claude Code、Codex、Cursor
及任何遵循 `SKILL.md` 规范的 agent。把一次功能验收拆成固定阶段，
**只在用例确认时停下等你审核**。

```
上下文收集 → 风险分析 → 用例设计 → 【你来确认】 → 脚本生成 → 执行与修复 → 代码审查 → 报告
```

## 安装

```bash
# 跨 agent（推荐）：装到通用目录，链接进本机所有已检测的 agent
npx -y skills add mingdui/ming-qa -g -y

# Claude Code 插件市场
/plugin marketplace add mingdui/ming-qa
/plugin install ming-qa@ming-qa

# 从源码（离线 / 自定义安装位置）
git clone https://github.com/mingdui/ming-qa.git
cd ming-qa/skills/quality-assurance-agent && ./install.sh --target claude-code
```

`-g` 装到用户级目录对所有项目生效（省略则装到当前项目）；`-y` 一次装全 8 个技能。
开头那个 `npx -y` 是跳过 npx 自己的「是否安装 skills 包」询问——两个 `-y` 作用不同，
少了前者第一次装会停在交互提示上。更多说明见 [安装文档](docs/installation.md)。

## 快速上手

装完之后不需要敲任何命令。在 agent 里说：

```
使用 quality-assurance-agent，对 <你的模块> 进行验收
```

agent 会自动初始化项目、体检环境、按验收范围装好需要的工具链，
只在下面两处停下来等你：

| 停在哪 | 你要做什么 |
|---|---|
| **填配置** | 按它给的清单填 `.qa-agent/local/.env`（已 git-ignored）。必填的只有测试账号 |
| **确认用例** | 审阅生成的用例。这是全流程唯一的人工门禁 |

确认之后，脚本生成 → 执行修复 → 代码审查 → 报告，全部自动衔接。

另外两个场景：

| 场景 | 在 agent 里说 | 和首次验收的区别 |
|---|---|---|
| **回归**（也叫"重跑"） | `回归 <模块名>` | **快**——只执行已有脚本，AI 不参与分析设计，**不用你确认** |
| **加用例** | `对 <模块名> 增加 <场景> 的用例` | 已有用例和脚本都不动，只为新场景走一遍全流程，确认时**只看新增的那几条** |

## 产物在哪

所有产物在目标项目的 `.qa-agent/` 下。

| 目录 | 内容 | git clone 后 |
|---|---|---|
| `.qa-agent/cases/` | 已确认的长期用例 | ✅ |
| `.qa-agent/spec-tasks/` | 用例→脚本映射蓝图 | ✅ |
| `.qa-agent/config/` | 项目 QA 配置 | ✅ |
| `.qa-agent/fixtures/` | 账号/服务示例模板 | ✅ |
| `.qa-agent/knowledge/` | 项目经验库 | ✅ |
| `.qa-agent/reports/` | HTML 报告 | ✅ |
| `.qa-agent/current/` | 本轮运行产物 | ❌ 需重新生成 |
| `.qa-agent/runs/` | 执行日志与证据 | ❌ |
| `.qa-agent/local/` | 本地账号密码 | ❌ |
| `tests/api/<模块>/` | 测试脚本 | ✅ |

## 数据与外发

本工具不内置任何外部服务地址，只在你明确配置后才对外通信：

- **LLM 网关**：用于可选的多模型交叉审查，未配置则跳过该阶段
- **告警 webhook**：用于推送报告质量告警，未配置则不发送任何通知

两者的配置方式见 [配置文档](docs/configuration.md)。

## 系统要求

- Python 3.9+。**工具自身**只用标准库，没有需要 `pip install` 的运行时依赖
- Git
- 目标项目：Maven 或 Node.js/npm，其他类型（Python、Go、Gradle 等）AI 执行过程可自动兼容
- E2E 测试基于 Playwright 覆盖 Web，并支持 Android 真机 / 模拟器：脚本驱动用内置 `mobile_adb.py`（adb 直连，不依赖 MCP），agent 直驱走 mobile-mcp，沉淀资产走 Mobilewright；证据链为元素树 + 截图 + 设备日志。iOS 已接入检测与证据契约，但需要 macOS + Xcode，尚未实测
- 移动端这条链路会用到**外部工具**，它们不在 Python 依赖里，按需通过 `npx` 调起：`mobilewright`（跑 `.spec.ts` 回归）、`mobile-mcp`（agent 直接驱动设备）、`adb`（`mobile_adb.py` 与证据采集）。缺哪个，对应能力就不可用，其余流程照常
- 被测对象**没有源码**时（已安装的 APK/IPA）：用 `init-project --profile mobile` 走黑盒路径。该模式只能证明「真机上这条流程走得通」，没有代码审查，不能证明实现质量

## 遇到问题

| 情况 | 看这里 |
|---|---|
| 装不上、环境报错、服务起不来 | [安装文档](docs/installation.md) |
| 想知道某个配置项什么意思 | [配置文档](docs/configuration.md) |
| 想了解各阶段产物和门禁设计 | [架构文档](docs/architecture.md) |

**想手动调用 CLI**（通常在排查问题时）：命令在 skill 的 `scripts/qa_agent.py`。
先解析 skill 目录，再用它执行：

```bash
QA_AGENT_DIR="${QA_AGENT_CLI:-$(dirname "$(find ~/.claude/skills ~/.agents/skills ~/.codex/skills .claude/skills .agents/skills .codex/skills -maxdepth 2 -name SKILL.md -path '*quality-assurance-agent/*' 2>/dev/null | head -1)")}"
python "$QA_AGENT_DIR/scripts/qa_agent.py" doctor --repo . --strict --check-services
```

## 参与贡献

[贡献指南](CONTRIBUTING.md) · [变更日志](CHANGELOG.md)

## 许可证

[MIT](LICENSE)
