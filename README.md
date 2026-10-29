# Football TC

一个可独立安装的 Codex 足球市场分析 skill。使用官方 Sporttery、8BO/Okooo 和
Okooo Betfair 的本轮采集证据，生成完整场次报告。全部结论为影子研究，不执行投注。

## 安装与使用

仓库地址：**https://github.com/Xiaroy-Liu/skill-TC**

在支持本仓库插件格式的 Codex 客户端中，将仓库作为插件安装。也可克隆仓库或通过
**Code → Download ZIP** 下载，将完整的 `skills/football-tc/` 文件夹复制到
`~/.codex/skills/football-tc/`。如设置了 `CODEX_HOME`，使用其下的 `skills/football-tc/`。
更新前请保留需要的本地运行产物；安装后重新加载 skills 或重启客户端。

然后输入：

```text
使用 $football-tc 分析今天的比赛
```

代理检查依赖并完成本轮官方赛程发现、8BO/Okooo 身份匹配、市场采集和分析。
无需安装其他足球 skill，也无需提供个人历史数据、球队映射、采集回执或数据库。
采集连接器、盘口分析组件和 TJ 审计组件均包含在本仓库。

## 运行环境

- macOS/Linux 的 bash 环境，Python **3.10+**，可创建虚拟环境。
- 首次安装需要访问 Python 依赖源；采集需要访问 Sporttery、8BO 和 Okooo。
- Scrapling 固定为 `scrapling[fetchers]==0.4.11`，安装在 skill 自己的 `.venv/`。
- 已在同一台 Mac 的独立副本中新建环境并实际采集、分析；其他机器及 Windows 未验证。

手动初始化依赖，在仓库根目录运行：

```bash
bash skills/football-tc/scripts/bootstrap_runtime.sh
```

若默认 Python 版本过低，可指定：

```bash
PYTHON_BIN=python3.12 bash skills/football-tc/scripts/bootstrap_runtime.sh
```

接着可直接运行：

```bash
skills/football-tc/.venv/bin/python skills/football-tc/scripts/analyze_today.py
```

入口从当前工作目录创建 `work/` 采集批次和 `outputs/` 报告目录。
可用 `--analysis-date YYYY-MM-DD` 指定业务日，`--run-root` 和 `--output` 指定新目录。
每次创建新批次和空身份状态，不复用历史映射或旧回执；无需先配置调度模板。
北京时间业务窗口为 `[当天 12:00, 次日 12:00)`，跨午夜场次纳入同一报告。

## 报告与缺失说明

- 保留窗口内全部官方场次，并分别展示在售状态、采集状态和缺失原因。
- WDL 盘口基线、官方有符号 HHAD 基准和外部亚盘对照。
- **比分 Top3、精确总进球 Top2、半全场 Top2**，逐玩法报告可用场数及缺失场次。
- 每场唯一 WDL/HHAD 影子单选；TJ 评分与门禁作为独立审计附件。
- 本轮采集批次、采集时间、handoff 及来源证据 SHA-256。

8BO HTML 壳页面支持已登记的后台赛程 API，按完整北京日期时间、主客顺序和唯一候选
匹配。验证页停止该来源；冲突、多候选和没有报价均保留明确状态。身份匹配成功不代表
每个市场报价齐全，也不能保证源站始终可访问。

比分使用原始隐含概率；半全场须有完整九项报价才去水。区间总球不能冒充精确总进球。
已采得的小玩法面板直接接入分析；真正缺失的字段不能用历史数据补齐。
修正分析时保留原报告，在新目录生成绑定同轮 handoff 的修正版并披露单选变化。

## 仓库结构

```text
.codex-plugin/plugin.json    插件入口
skills/football-tc/
  SKILL.md                  代理执行规则
  agents/                   skill 元数据
  scripts/                  采集、交接与一键入口
  references/               来源、身份和报告契约
  engine/                   随包盘口与 TJ 分析组件
  requirements.txt          独立运行依赖
```

详细规则见 [SKILL.md](skills/football-tc/SKILL.md) 和
[小玩法衔接契约](skills/football-tc/references/small-market-analysis-contract.md)。
连续调度与赛后结算为高级用法，按 skill 内相应契约配置；日常分析使用上述一键入口。

仓库仅分享代码、静态规则和说明。虚拟环境、个人配置、身份状态、原始页面、历史盘口、
采集回执和生成报告均不随仓库分享。

## 许可证

MIT，见 [LICENSE](LICENSE)。
