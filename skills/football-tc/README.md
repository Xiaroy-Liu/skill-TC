# Football TC

一个可独立安装的 Codex 足球市场分析 skill。输入“使用 $football-tc 分析今天的比赛”，
自动完成官方赛程发现、8BO/Okooo 身份匹配、盘口与必发采集，以及逐场影子分析。

## 安装到 Codex

1. 从 [GitHub 仓库](https://github.com/Xiaroy-Liu/skill-TC) 克隆，或使用 **Code → Download ZIP** 下载并解压。也可按仓库根目录 README 使用插件安装。
2. 手动安装时，将仓库里的整个 `skills/football-tc` 目录放入 Codex 的 skills 目录。
   默认位置为 `~/.codex/skills/football-tc`；如已设置 `CODEX_HOME`，使用其下的
   `skills/football-tc`。更新已有版本前保留需要的本地运行产物。
3. 在 Codex 中重新加载 skills 或重启应用后，输入：

   ```text
   使用 $football-tc 分析今天的比赛
   ```

skill 会先检查依赖，缺失时执行随包安装入口，再开始本轮采集。无需安装其他足球 skill，
也不需要提供历史比赛数据、球队映射或旧采集回执。

## 环境与手动运行

- 当前安装入口适用于 macOS/Linux 的 bash 环境。
- 需要 Python 3.10+、可创建虚拟环境，以及可访问依赖源、Sporttery、8BO 和 Okooo 的网络。
- 首次需要下载依赖；源站限制、验证页或网络错误会留下明确缺口，不能保证每个市场都有报价。
- 已在同一台 Mac 的独立副本中新建环境并实际完成采集与分析；Windows 和其他机器未验证。

在安装后的 skill 目录运行：

```bash
bash scripts/bootstrap_runtime.sh
```

如默认 Python 版本过低：

```bash
PYTHON_BIN=python3.12 bash scripts/bootstrap_runtime.sh
```

从希望保存数据的工作目录运行以下命令（使用默认安装位置时）：

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/football-tc/.venv/bin/python" \
  "${CODEX_HOME:-$HOME/.codex}/skills/football-tc/scripts/analyze_today.py"
```

可加 `--analysis-date YYYY-MM-DD` 指定业务日。北京时间业务窗口是
`[当天 12:00, 次日 12:00)`；跨午夜场次纳入同一报告。

每次创建新批次和空身份状态，原始证据在当前工作目录的 `work/`，报告在 `outputs/`。
通过 `--run-root`、`--output` 指定路径时，两个目录都必须尚不存在。

## 报告包含什么

- 逐场 WDL、官方有符号 HHAD 基准与外部亚盘对照。
- 比分 Top3（原始隐含概率）、精确总进球 Top2、半全场 Top2（完整九项报价去水）。
- 每场唯一 WDL/HHAD 影子单选、独立 TJ 评分及阻断原因。
- 官方行覆盖、各玩法字段覆盖，以及采集批次、时间和证据 SHA-256。

缺失字段会明确标注原因。身份匹配成功不代表全部市场报价齐全；半全场不完整时不能
伪造去水概率。全部结论仅作影子研究，不执行投注。

## 采集与分析规则

8BO HTML 壳页面可使用已登记的后台赛程 API；按完整北京日期时间、主客顺序和
唯一候选匹配，保留实际原始响应与哈希。验证页停止该来源，冲突候选不能强行匹配。

比分和半全场采集面板直接接入随包分析组件。已采得可用报价时应进入报告；没有报价
时明确保留缺口。修正分析时沿用本轮绑定的 handoff，在新目录生成修正版并说明单选变化。

代理执行细则见 [SKILL.md](SKILL.md)，盘口小玩法口径见
[衔接契约](references/small-market-analysis-contract.md)。

## Skill 目录内容

```text
SKILL.md
README.md
.gitignore
requirements.txt
agents/
scripts/
references/
engine/
```

仓库仅发布代码、静态规则与文档。虚拟环境、个人配置、历史盘口、身份映射、采集回执、
原始页面和生成报告均不属于分享内容。`.gitignore` 只忽略未跟踪文件；更新已有仓库时
还需检查实际提交清单。
