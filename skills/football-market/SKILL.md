---
name: football-market
description: "统一采集、冻结并分析足球市场数据：使用官方 Sporttery、8BO/Okooo、Okooo Betfair 和 Scrapling 生成可审计市场 handoff，再生成盘口基线、TJ 路由与唯一 WDL/HHAD 影子单选；不执行投注。"
---

# Football Market

这是本插件唯一的足球 skill，包含一个采集器和一个分析入口：

- **采集器**：`football-market-data-collector`，负责官方 Sporttery 市场池/SP、8BO/Okooo 市场证据、Okooo Betfair 证据、标准化、审计、调度、官方赛果和不可变市场 handoff；不采集 API-Football、球员、天气、模型上下文或历史基金数据。
- **分析入口**：本 skill 只消费采集器生成的具体冻结 cut，生成盘口基线、TJ 评分路由、最终二选一和赛后影子结算。采集完成前不得进入分析。

市场分析不反向修改采集 handoff；采集和分析使用同一个 handoff SHA-256、官方场次集合和覆盖计数。

采集器的详细规则按需读取：`references/market-collector-mode.md`、`references/automation-contract.md`、`references/handoff-contract.md`、`references/post-match-data-contract.md`、`references/football-three-skill-handoff-contract.md` 和 `references/collector-market-only.example.json`。

8BO/Okooo 浏览器采集由 `scripts/run_eightbo_scrapling.py` 和 `scripts/run_okooo_panels.py` 负责；其 `football_sources.eightbo` 连接器源码已随本 skill 放在 `scripts/football_sources/`，不依赖安装者电脑上的私有源码目录。

## 定位

这是 `$football-pankou` 与 `$football-tj` 的统一入口。它在同一不可变
`handoff/football_data_handoff.json` 上连续完成三层工作：

1. **盘口基线层**：逐源去水，保留 WDL、官方 HHAD 基准与统一有符号亚盘 overlay、比分 Top3、精确总进球 Top2、半全场 Top2，以及所有缺失和阻断原因。
2. **评分路由层**：消费上述冻结 `market_selection.json`，进行注册证据组计票、平局保护、冷门风险、低总球标签、逐场评分和正式门禁路由。评分与选择资格分离：核心方向字段完整时允许在可用证据上形成部分评分，但注册选择证据不完整时仍须 fail-closed。
3. **分析单选层**：在不改变前两层事实与门禁的前提下，比较独立的 WDL 和 HHAD 候选，给每场一个、且仅一个市场方向的影子分析推荐。

分析单选的候选与结论只消费冻结盘口基线；TJ 的评分、门禁、路由、OOS 状态和 `route_receipt`/ledger 当前都不是其输入，也不得阻断、降级或默认改写 WDL/HHAD 二选一。单选必须在盘口基线完成后独立生成，即使 TJ 验证失败或正式路由为 `观察`/`HHAD_RESEARCH_POOL`，也不能因此不生成单选。TJ 如需保留，只能作为独立审计附件，不能混入单选理由或选择逻辑。

盘口基线不是推荐，评分路由也不会改写盘口概率或方向。三层必须各自留存，并以同一个 handoff SHA-256、官方场次集合和模型文件哈希绑定。不得把展示层字段、官方 HHAD 基准或 TJ 路由反向覆盖盘口基线。

### 官方让球与亚盘统一口径

统一使用主队视角的有符号让球线：主让为负、主受让为正，半球和四分之一盘保留。官方 HHAD 提供有符号线、三项 SP 和去水基准；8BO/Okooo 亚盘先归一化后按可用来源等权合成，并通过 `asian_vs_official_gap` 同时记录两类事实：外部线比官方更深时，对官方让胜是方向性增强信号；线差造成的跨市场结算转换不确定性，则是独立的授权/结算风险。`official_baseline_status`、`direct_market_status`、`unified_signed_line_status`、`asian_gap_directional_support`、`asian_gap_conversion_risk` 必须分开记录。没有外部同线三项报价时，只能将 `direct_market_status` 标记为 `missing/no_exact_line_quote`，不得把官方 HHAD 写成 `HHAD missing`，也不得删除官方 HHAD 方向或概率。该统一比较不产生 HHAD 三项概率，不放宽正式 HHAD 的直接同线和 OOS 门禁。

## 每场分析单选

当用户要求“推荐”、"每场只在胜平负和让球胜平负中选一个"或等价表述时，必须交付这一层。它是冻结盘口的 `shadow_recommendation`，而不是投注、EV、资金或执行指令。

每个官方场次恰好一行，且必须在 `WDL` 与 `HHAD` 中选择一个方向。不得同场并列两项、先给两项再让用户自行选择，或用“研究池”“观察”“无推荐”替代预测。深盘、官方未售 WDL、直接同线 HHAD 缺失、OOS 未通过及其他 TJ 阻断属于独立授权/风险字段：应在 TJ 审计附件中原样保留，但不是分析单选的输入，不能阻断、降级或删除影子预测。输出必须在当前分析目录保存 `final-selection/final-single-selection.md` 与 `final-selection/final-single-selection.json`，写明：场次、比赛、唯一市场和方向、HHAD 有符号让球线（仅 HHAD）、冻结盘口证据比较、选择理由、亚盘方向性支持与线差转换风险、cut 与 handoff SHA-256；TJ 门禁若展示必须单列为审计信息。后续对话必须复用同一 cut 的该文件；除非生成新 cut，不得手工重选而产生不同结论。

这层与相邻字段严格区分：

- `market_direction` 是 WDL 盘口基线，不是自动推荐。
- `official_hhad_direction` 是官方 HHAD 三项 SP 的独立基准；它可作为影子分析的 HHAD 候选，但不等于外盘直接同线 HHAD 候选或正式 HHAD 授权。
- `HHAD_RESEARCH_POOL` 只是 TJ 的内部研究状态，绝无方向，不能作为选择 HHAD 的理由，更不能复制为推荐。
- `single_selection.csv` 只记录 TJ 全部门禁后的正式影子路由；分析单选不得覆盖它，也不得把它的“观察”行误写成无推荐。

### 候选与判断

WDL 和 HHAD 独立建候选、独立解释，绝不互相换算：

1. **WDL 候选**只能是同一 cut 中逐公司去水、注册聚合后的 WDL 第一方向。分析时必须一并检查 8BO 与必发是否同向、开盘至冻结是否持续同向、第一与第二差值、资金/热度/离散冲突、平局保护，以及比分、总进球、半全场结构是否支持普通胜负。
2. **官方在售资格是授权字段，但最终展示必须规避未开售玩法。** 官方 `HAD.spAvailable`、玩法 `Selling` 与 HHAD 在售状态必须逐场保留。内部分析仍须保留 WDL/HHAD 的唯一影子候选，不得因未售而删除场次、输出空选择或改成“观察”。但若最终候选为官方未开售的 HAD/WDL，且同场官方 HHAD 实际在售，则最终展示单选改用独立生成的官方 HHAD 候选，并明确标记为影子研究、不可执行；只有所选官方玩法确实在售时，才可标记为可执行授权。若两头均未开售，保留原唯一影子候选并标记“官方未开售/不可执行”。
3. **HHAD 候选**优先来自该场官方精确有符号让球线下的官方三项 HHAD 基准；若存在精确同线直接三项报价，可另列为外部 HHAD 市场头。分析必须保留官方线、8BO/Okooo 统一后的有符号亚盘线、线位差、让平边界信号，以及比分结构在让胜/让平/让负三个结算区间的覆盖。不得从 WDL、亚盘、资金、Kelly、大小球或比分反推或伪造 HHAD 三项概率。
4. **深盘保护**：当官方 HHAD 的绝对让球值 `>=2` 且官方 HAD 未售卖时，外盘 WDL 强势、亚盘更深或官方 HHAD 第一去水概率都不足以单独确认“让胜”。这些限制必须降为深盘风险/未授权标签；仍须在 WDL 与 HHAD 中作唯一影子预测，且不得将“主胜/客胜很强”伪装成已经通过直接同线 HHAD 验证。
5. 先形成唯一影子预测，再分别标记授权状态。去水概率只能形成候选，不能单独决定正式授权。优先选择同时获得跨源同向、路径稳定、比分结构支持且反证较少的一头；WDL 领跑稳定但比分以一球小胜为主时，应选 WDL，不把官方 `-1` 的让负误写为推荐；WDL 方向分散或平局风险显著、而官方 HHAD 的结算区间与比分结构更匹配时，可选择 HHAD。外部亚洲线比官方更深时，应将其作为官方让胜的方向性增强证据，但不能把线位差直接转换成官方 HHAD 概率或正式授权；必须同时保留官方线与外部线的结算区间差异。以官方 `-2` 为例，净胜 2 球是让平、净胜 3 球以上才是让胜；外部 `-2.5` 只能说明外部市场要求更高的净胜门槛，不能消除官方 `-2` 的让平边界。直接同线 HHAD、OOS 和在售要求继续严格控制 TJ 正式路由，但不得删除这项影子单选。
6. `asian_vs_official_gap` 必须拆分为方向性支持和转换风险，不能笼统写成单一风险字段：外部线更深通常支持主队让胜方向，但官方整数盘的让平边界、不同盘口的结算区间和来源时间差仍要单独保留。`focus_hhad_draw`、资金/热度拥挤、平局风险、反转和比分分散都是风险或反证，必须写入理由；这些因素可以降级分析信心，不能无证据翻转候选方向。直接同线 HHAD 和 OOS 未通过时，HHAD 选择必须标为 `official_baseline_shadow`，不得标为 `direct_market_candidate`、`authorized` 或正式主推；外部同线缺失只影响授权字段，不得删除二选一。
7. 当两个合格候选的正反证无法区分时，默认 WDL，因为其必须有跨源冻结报价；理由明确写为 `tie_break_to_wdl_due_to_no_direct_hhad_edge`。若同时存在比分平局集中、低总球或官方整数盘的实质线差，附加 `tie_break_requires_draw_and_hhad_boundary_review` 并降为谨慎影子结论；不得因 WDL 优势不足就自动切换 HHAD，也不得从这些风险反推 HHAD 方向。

### 复盘反馈与两种单选评估

赛后复盘必须把下列口径分开结算，不能只报告一个“命中率”：

1. **前台最终二选一**：结算 `final-selection/final-single-selection.json` 的唯一 `selected_market/selected_direction`；这是冻结展示口径。
2. **原始分析影子单选**：结算生成单选时、尚未因官方售卖资格改变展示玩法的 WDL/HHAD 候选；这是评估候选判断本身的口径。
3. **TJ 研究单选**：结算 `market-routing` ledger 中的 TJ 研究方向和状态；这是评分/门禁研究口径，不能回写前台单选。
4. 同时报告纯 WDL 第一方向和官方 HHAD 第一方向作为基线，但不得用任何基线命中率替代前台二选一成绩。官方主客顺序、外部事件方向或直接市场证据存在 `orientation_conflict` 时，整场标为 `invalid_for_scoring`，保留在覆盖统计中但剔除能力评价。

复盘至少按以下维度分层：TJ 主选择门通过/未通过、研究等级、`net_score`、平局保护、跨源冲突比例、官方让球绝对值、亚洲线与官方线差、比分 Top3 是否覆盖让球结算区间。门通过不是命中保证，门未通过也不是方向必错；复盘要同时记录“方向命中”和“玩法结算命中”。

当官方 HAD/WDL 未售而 HHAD 在售时，售卖修正必须产生独立的 `conversion_risk` 记录：WDL 强势只能说明普通胜负候选，不能自动升级为 HHAD 让胜。尤其官方绝对让球值 `>=2` 时，除非冻结比分结构明确覆盖相应净胜球区间，否则保留“WDL 方向判断”和“HHAD 穿盘判断”两条独立事实；不得把售卖修正后的 HHAD 结果误写成原始 WDL 判断失败。

整数盘复盘要先结算让平边界，再评价让胜/让负。例如主队 `-1` 且实际净胜 1 球，唯一结算为让平；比分平局集中、资金平局领先或亚洲线与官方线存在实质差异时，必须把边界风险写入单选理由和赛后根因，而不是笼统归为预测错误。

赛后输出应给出逐场根因分类：`identity/orientation`、`wdl_direction`、`hhad_margin_or_boundary`、`sale_conversion`、`low_separation_conflict`、`score_tail` 或 `result_missing`。复盘只追加事实和研究结论，不改写冻结盘口、TJ ledger、权重、阈值或正式执行状态；单场或单日结果不得触发自动调参。

平局风险展示必须分层：WDL 概率/资金触发门、比分 Top3 的平局集中、以及官方整数 HHAD 的让平结算边界是独立事实。概率门未触发不得写成“没有平局风险”；必须保留比分平局结构。官方整数线还须展示使 `主队净胜球 + 官方让球 = 0` 的让平边界及其 Top3 覆盖。亚洲线与官方线存在实质差异时，同时记录外部更深/更浅对让胜候选的方向性支持，以及不同结算区间带来的转换风险；不得据此伪造 HHAD 概率或绕过直接同线与 OOS 门禁。

分析单选的固定顺序是：冻结身份与哈希校验 -> 独立生成 WDL/HHAD 候选 -> 核对欧赔/必发同向性与完整走势 -> 核对资金热度、平局风险和反转反证 -> 核对亚盘与官方线差及让球结算区间 -> 核对比分、总进球、半全场结构 -> 深盘保护 -> 每场唯一选择 -> 冻结逐场理由 -> 另行附加 TJ 审计状态。缺失字段保持 `missing`，不补零；只有 WDL 与官方 HHAD 两个市场头都实际缺失时才允许终止并报告数据故障。TJ 的评分、门禁、OOS 和正式路由不在这条选择链上。

## 输入与边界

市场模式使用统一交接契约，见本 skill 附带的
`references/football-three-skill-handoff-contract.md`。
本 skill 只消费 collector 生成的具体冻结
`handoff/football_data_handoff.json`，并把同一 `handoff_sha256`、
`acquisition_id`、官方场次集合和覆盖计数写入全部市场产物；它不回写
collector handoff，也不把市场产物当作网站发布输入。

本 skill 的采集器只生成市场 handoff 和官方赛后 handoff；不得读取或写入其他模型 runtime、API-Football/球员/天气/上下文 handoff。分析入口不得读取任何 collector 的 `latest` 或跨 cut 替换文件。

- 只接受具体冻结 cut 的 `handoff/football_data_handoff.json`，拒绝 `latest`、可变目录和跨 cut 替换。
- 对每一个官方在售场次保留一行；缺失保持 `missing`/`blocked`，不得删行、补零或将其当作中性。
- 复合证据组按已注册且有效的成员做一次聚合；部分成员缺失时保留逐源覆盖并标记 `partial`，不得把整个方向评分清空。概率、优势差或路径等核心方向字段缺失时，方向评分才为 `missing`。
- `net_score = direction_quality_score - draw_protection_score`。冷门风险保留为可见标签和复盘维度，但不再扣减净分，也不单独阻断路由。
- TJ `market-routing` 的时序门独立作用于正式影子路由：仅 `prospective_pre_kickoff` 可进入 TJ 主选择门，`replay_only_after_kickoff` 必须保留在 TJ 账本中，但不得作为前瞻选择或校准样本。分析单选自行记录开球前/后时间状态；该状态只标注可前瞻性，不把 TJ 时序门变成分析选择阻断。
- WDL 与 HHAD 是独立市场头。普通 WDL、亚盘、资金、Kelly、大小球或比分均不能推导 HHAD 方向；正式 HHAD 路由仍须满足精确签名线、同线直接三项报价和独立 OOS 门。分析单选的官方 HHAD 基准候选仅适用“每场分析单选”一节的 `official_baseline_shadow`，绝不改变正式门禁。
- `O2.5`、BTTS 只作为后台交叉证据，不能成为前台官方玩法、单选或替代 WDL/HHAD/比分/总进球/半全场。
- 全流程固定为 `research_only / shadow_only`：`probability_impact=0`、`stake=0`、`parlay=false`、`formal_execution=false`。不得借由合并流程改变此状态。

## 运行

使用唯一编排入口：

```bash
python3 scripts/run_frozen_market_pipeline.py \
  --handoff /path/to/cut/handoff/football_data_handoff.json \
  --output /path/to/new-output/football-market
```

输出根目录不可预先存在。成功后包含：

- `market-baseline/`：纯盘口 `market_selection.*`、盘口 cut 回执和前台盘口分析表；
- `market-routing/`：逐注册证据组 ledger、逐场评分卡、盘口展示层和 `single_selection.csv`；
- `final-selection/final-single-selection.{md,json}`：本 cut 唯一权威的 WDL/HHAD 二选一清单，逐场保留冻结盘口候选比较、官方 HHAD 有符号线、亚盘方向性支持、线差转换风险和选择理由；TJ 门禁与阻断项只在 `market-routing/` 审计产物中保留，不得成为该清单的选择输入；
- `analysis-single-selection.{md,json}`：每场唯一的影子分析推荐及其冻结理由；
- `market_pipeline_receipt.json`：自动化基线/路由层 SHA-256、官方场次覆盖、路由覆盖和影子生命周期证明。

## 报告交付

每次完成冻结市场分析，除保留流水线原始产物外，必须在本次输出目录交付一份独立 Markdown 报告。报告以 `market-baseline/market_public_display_table.md` 的版式为前半部分，且不得省略任何官方场次。固定顺序如下：

1. **盘口分析表**：场次、比赛、胜平负、平局风险、官方 HHAD 基准、比分 Top3、精确总进球 Top2、半全场 Top2；比分必须标明为原始隐含概率，其余使用对应冻结去水概率。
2. **分析数据表（审计）**：逐场保留 8BO WDL、必发 WDL、综合 WDL、官方 HHAD 基准、8BO/Okooo/综合亚盘线、线位差、让平风险判别与平局风险。官方 HHAD 仅为独立基准，绝不能由 WDL 或亚盘推导为让球选择。
3. **每场最终二选一**：必须放在 TJ 评分路由之前，且报告中只能出现这一处作为前台最终单选章节；以 `final-selection/final-single-selection.json` 为唯一权威来源。纯盘口单选表只展示：场次、比赛、唯一单选、官方售卖核对、判断理由；不重复展示 WDL 基线或官方 HHAD 基线。每场必须有唯一的 `WDL` 或 `HHAD` 方向；报告与赛后命中率只能结算该文件，不得将 WDL/HHAD 两条基线各自命中率替代二选一成绩。逐场判断理由必须保留冻结盘口证据比较、亚盘方向性支持、线差转换风险与共享风险。若 HAD/WDL 未开售，须在“官方售卖核对”中明确标记，并按官方在售资格规则规避为可执行推荐。生成后必须核验章节顺序为“每场最终二选一”在前、“TJ 评分路由（独立审计）”在后；若顺序不符，先修正报告编排再交付。
4. **TJ 评分路由（独立审计）**：放在每场最终二选一之后。主报告必须逐场展示 `research_grade`（A/B/C/X）、`market_shadow_net_score`、评分状态、评分覆盖（核心方向与证据簇）、选择证据完整性、时序状态、最终路由和主要阻断项；不能只链接 `match_scorecards.md` 而省略等级。等级必须来自冻结 `market-routing/single_selection.csv`/ledger，且与附件逐场一致；缺失等级显示 `X/缺失`，不得猜测或留空。必须保留 `HHAD_RESEARCH_POOL`、`观察`、`partial`/`missing` 状态及其原因，不能以摘要掩盖未通过的门禁。冷门风险只作标签，不得写成净分扣分或单独阻断原因。报告渲染器须在输入缺少上述 TJ 列时 fail-closed，禁止生成看不到评分等级的报告。
5. **TJ 研究单选（独立）**：若生成 TJ 单选表，须置于 TJ 评分路由之后，逐场保留研究方向、研究状态和理由。TJ 单选仅用于研究与赛后影子结算，不改变纯盘口单选，不构成正式授权或执行。
6. **冻结与核验**：cut、handoff SHA-256、输入/输出/遗漏/重复覆盖统计、管线状态和主要产物哈希；明确 `probability_impact=0`、`stake=0`、`parlay=false`、`formal_execution=false`。

完整 `market-routing/match_scorecards.md`、`final-selection/final-single-selection.{md,json}` 与（如生成）`analysis-single-selection.{md,json}` 必须作为报告链接的审计附件保留。报告可概括评分项，但不得改变评分路由、补零缺失证据、把市场基线写成可执行推荐，或省略任何官方行。

可选 `--market-hhad-oos` 仅传入已经冻结、哈希绑定的 HHAD OOS 审计。它不会绕过 HHAD 的直接同线报价门。

赛后结算必须使用独立模式，禁止在完整构建命令上附加 `--post-match`。完整构建命令收到 `--post-match` 必须 fail-closed，防止赛后重建盘口基线、最终单选、TJ ledger 或报告而污染时序。结算模式必须消费完整冻结报告及其 `market_pipeline_receipt.json`，校验报告、预测 handoff、最终单选和 TJ ledger 的路径与 SHA-256、官方场次集合及路由 registry。报告是冻结展示来源；命中字段仍只从报告所绑定的结构化 `final-single-selection.json` 和 TJ ledger 读取，禁止从 Markdown 反解析预测。核验通过后才追加官方结果和诊断到一个新目录：

```bash
python3 scripts/run_frozen_market_pipeline.py \
  --settlement-only \
  --frozen-output /path/to/frozen-football-market-output \
  --post-match /path/to/football_post_match_data_handoff.json \
  --settlement-mode prospective \
  --output /path/to/new-market-settlement-output
```

`--settlement-mode` 在独立结算模式中必须显式指定。仅当冻结 ledger 生成时间早于全部开球时才允许 `prospective`；其他历史结算使用 `replay`。独立结算输出只有 `market-settlement/` 与 `market_settlement_only_receipt.json`，不含重新生成的基线、单选、路由或前台报告。

影子选择器支持 `--shadow-version v2.2` 与 `--shadow-version v2.3`。v2.2 增加整数官方让球线的让平边界保护和售卖转换可见性；v2.3 进一步要求 WDL 转 HHAD 同时具备同线直接证据、独立 HHAD 结算结构和低让平边界风险，否则保留原始 WDL 影子判断并阻断执行资格。两者都不改写既有冻结 cut、概率、TJ 净分或正式门禁；应先用于同一 cut 的对照回放，不能直接晋级正式执行。

## 赛后

赛后结算只接受 collector 的
`football-post-match-data-handoff-v1`，并校验其预测 cut SHA-256、官方场次集合、
`expected_count/output_count/settled_count` 和逐场 `FT_90`。赛果、半场比分和
命中字段只能来自中国竞彩网体彩官网结果服务；API-Football、8BO、Okooo、
Betfair 或网站数据库不得作为替代来源。结算追加事实，不改写盘口基线、评分
ledger 或分析单选，也不调参。`replay` 仍是回顾性影子核算，不能作为前瞻样本、
正式晋级或执行依据；官方结果缺失时保留 `missing/terminal_gap`，不得补零或
切换到另一 cut。

每次成功的赛后结算还必须自动生成
`market-settlement/shadow-diagnostics.json`。该诊断只追加三种单选（前台最终、
原始分析、TJ 研究）对照、纯 WDL 基线、TJ 净分分档、平局风险触发/实际平局
覆盖、评分证据完整性和 `prospective_pre_kickoff`/
`replay_only_after_kickoff` 时序统计。诊断不能修改冻结选择、评分、阈值、权重、
路由、stake 或执行状态；指标不足时保留 `missing/partial`，不得把回放结果写入
前瞻校准。

## 兼容性

`$football-pankou` 与 `$football-tj` 保留为已有自动化和历史回放的组件入口。新的市场分析、评分或单选请求应优先使用 `$football-market`，以保证它们绑定同一冻结 handoff；基线、TJ 评分路由和独立分析单选必须分别留存、可独立核验，TJ 不得干涉分析单选。
