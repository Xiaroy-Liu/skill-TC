# Football Market

这是一个单一的 `football-market` skill，包含完整的“市场采集/冻结 -> 盘口分析 -> 赛后影子结算”流程。

- 采集器：`football-market-data-collector`，只采集官方 Sporttery 市场池/SP、8BO/Okooo 市场证据、Okooo Betfair 证据和官方赛果。
- 分析模式：盘口基线、TJ 评分路由、逐场唯一 WDL/HHAD 影子单选和赛后诊断。

所有输出仍是 `research_only` / `shadow_only`，不会执行投注、分配资金或改写冻结输入。

## 安装

在 Codex 中把 GitHub 仓库作为插件安装：

`https://github.com/Xiaroy-Liu/skill-TC`

安装一次后使用 `$football-market`。采集和分析是同一个 skill 的两个工作模式。

## 运行环境

- Python 3.11 或更高版本。
- 盘口分析只需要 Python 标准库。
- 采集器的 Scrapling 抓取需要 `requirements.txt` 中的 Scrapling fetchers 和 Playwright 依赖，安装脚本会从 [Scrapling GitHub](https://github.com/D4Vinci/Scrapling) 安装。
- 不需要 PostgreSQL 客户端，也不读取模型数据库。
- 运行状态、浏览器配置和凭据由安装者在本机运行时目录中提供；这些目录不会随插件上传，也不在 Git 历史中。

本仓库提供 `.python-version`、`pyproject.toml`、`requirements.txt`、运行时安装脚本和 GitHub Actions，用于固定版本、依赖和自动测试。

初始化采集运行环境：

```bash
bash skills/football-market/scripts/bootstrap_runtime.sh
```

然后按 `skills/football-market/references/collector-market-only.example.json` 复制一份自己的运行时模板，并将其中的运行根目录改为本机路径。需要凭据的来源通过采集脚本默认的私有运行时位置或环境变量提供；浏览器运行时可通过 `FOOTBALL_EIGHTBO_PYTHON` 指定。不要把密钥写进仓库。

## 输入边界

分析模式只接受明确的、不可变的 `handoff/football_data_handoff.json`。采集模式负责生成它，但分析不得读取 `latest` 或可变缓存。市场 handoff 契约已随仓库保存在 `skills/football-market/references/football-three-skill-handoff-contract.md`（文件名为历史兼容名）。

采集器模板校验示例：

```bash
PYTHONPATH=skills/football-market/scripts python3 skills/football-market/scripts/run_daily_collection.py \
  --template skills/football-market/references/collector-market-only.example.json --validate-template
```

市场流水线示例：

```bash
python3 skills/football-market/scripts/run_frozen_market_pipeline.py \
  --handoff /path/to/cut/handoff/football_data_handoff.json \
  --output /path/to/new-output/football-market
```

## 本地校验

```bash
PYTHONPATH=skills/football-market/scripts python3 -m unittest discover -s skills/football-market/tests -p 'test_*.py'
python3 -m py_compile skills/football-market/scripts/*.py
```

## 许可证

MIT，见 [LICENSE](LICENSE)。
