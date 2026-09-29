# Smart Money

目标主链路：候选钱包 → 历史画像与资格 → 交易观察与信号 → MAS 核验与输出 → 结算及前向反馈。
本轮精简保留该功能边界，历史回测不再要求保留。`smart-money watchlist` 执行一次候选发现、
历史核查、资格判断和名单保存；`smart-money monitor` 从唯一名单建立链上持仓基线，持续读取确认交易并提交 MAS；
`smart-money analyze` 也可直接接收钱包画像、交易信号和市场证据，
输出经过核验的聪明钱分析 JSON。
MAS 保留钱包分析、市场规则调查、领域分析、反向质疑、事实核验和文案生成。

钱包流程按地址读取官方公开数据，也可读取本地事实快照，复用 `markets`、`wallets` 的分类和资格规则。
监听是显式启动的单机命令；本仓库没有数据库队列、自动安装的定时任务或 HTTP 服务。

## 安装与使用

使用 Python 3.12 的专属环境：

```bash
conda create -n smart-money python=3.12 pip --no-default-packages -y
conda env config vars set -n smart-money PYTHONNOUSERSITE=1
conda activate smart-money
python -m pip install -e '.[dev]'
smart-money analyze --input packet.json --output analysis.json --offline
```

`--offline` 仅分析输入证据，不调用模型或外部来源。省略 `--output` 时输出到标准输出。
输入格式由 `research.models.AnalysisRequest` 定义，包含：

- `candidate`：信号时间、钱包、市场、事件、领域、交易方向，以及显式的 `wallet_profiles`。
- `context`：市场快照 `market`、规则及相关市场等背景。
- `evidence`：外部证据列表，携带来源、发布时间、首次获取时间及对应市场的事实字段。

最小输入示例可用于检查入口；它缺少画像和外部证据，因此只能得到证据不足的调查结果：

```json
{
  "candidate": {
    "candidate_id": "example",
    "as_of": "2026-09-01T00:00:00Z",
    "wallet": "0x1234",
    "market_id": "market-example",
    "event_cluster_id": "event-example",
    "sector_id": "TECH.AI.MODELS",
    "signal_type": "OBSERVATION",
    "evidence": {"market": {"title": "Will a new AI model launch?"}}
  },
  "context": {},
  "evidence": []
}
```

联网分析使用 `SMART_MONEY_ENV_FILE=.env smart-money analyze --input packet.json --output analysis.json`，
显式加载项目模型超时和按需启动配置。模型调用已统一为本地 `Qwen3.8-27B`，默认地址
`http://127.0.0.1:30000/v1`；仅接受环回服务或本地 SSH 隧道，不再读取旧模型供应商变量，也没有主备模型切换。
模型说明见 `/data/LLMzoo/service_configs/Qwen3.8-27B/README.md`。本项目启动入口为
`deploy/systemd/smart-money-qwen.service`，使用共享 SGLang 环境和同一份原始权重，不再依赖临时 tmux 会话。
安装一次：`systemctl --user link "$PWD/deploy/systemd/smart-money-qwen.service"`，然后 `systemctl --user daemon-reload`。
在显式加载的环境文件中设置 `SMART_MONEY_QWEN_AUTOSTART=1`：在线 MAS 首次请求时检查指定模型，未就绪则启动服务并等待；
已就绪直接复用，离线分析不启动。启动等待上限默认 600 秒，同时受研究总期限约束；失败保留原因，不切换模型。
本机将 `SMART_MONEY_QWEN_TIMEOUT_SECONDS` 设为 600；调用方的研究期限与单请求上限仍优先生效。
服务不随开机启动，手动停止用 `systemctl --user stop smart-money-qwen`，下次在线请求会重新启动。
状态和启动失败原因用 `systemctl --user status smart-money-qwen`、`journalctl --user -u smart-money-qwen` 检查。
当前使用模型说明中的 GPU `4,5`、双卡并行、原始精度、单请求和小块预填充。启动前要求每卡至少 30000 MiB 空闲显存，
不足立即报出可用量；不会结束其他用户任务。环境隔离旧 NCCL，直接使用共享 SGLang，不修改模型计算和权重。
配置项见 [.env.example](.env.example)，环境文件仅在 `SMART_MONEY_ENV_FILE` 显式指向它时加载。
本地推理不设置 Token 费用门槛；Token 数仅用于使用记录，输出长度与超时仍受限。
外部证据按目标市场与缺失字段获取，保留请求预算、来源核验和信号时间边界。
来源许可只读取一份 `external_sources.json`：默认读取当前目录，可用
`SMART_MONEY_SOURCES_FILE` 指定绝对路径；默认文件不存在时不启用外部来源，显式配置无效则报错。
未登记、停用或尚无适配器的来源保留缺口，
规则中的新网址也不会自动获得许可。问题按能力、地区、主体、指定来源和用途角色筛选；来源声明不等于材料已匹配。
公共网页改为本方直接取证：具体 URL → 原文，或登记的 feed／目录／sitemap → 少量文章。
只有 `external_sources.json` 中显式启用的来源可访问；当前生产清单 10 条均停用。
`SMART_MONEY_SOURCES_FILE` 可指向隔离清单；`SMART_MONEY_EVIDENCE_DIR` 指定正文归档目录（默认 `data/evidence`）。
正文复用内容寻址压缩块，结果保存引用，MAS 核验时读取原文；归档和结果须一并保留。
直连使用经全量 DNS 检查且固定 IP 的 HTTPS，不继承环境代理。需要隔离生产公网网段时配置
`SMART_MONEY_EVIDENCE_DENY_CIDRS`（逗号分隔 CIDR）；缺浏览器、访问拒绝、字段或赛事身份不明均保留缺口。
取证上限、来源维护和发布边界见 IDEA 4.10；不需要 Tavily 密钥。

本次隔离实测输入及结果在 `reports/direct_evidence/20260929T135437Z/`：
`probe.json` 区分各来源成功/失败；`brazil.gap.json` 保留真实 Brazil 观察及身份缺口；
`fed.input.json` 是明确标注无真实钱包/交易的公告取证样例。
可用现有入口复现完整研究（需要本地 Qwen；保留输入原有预算和失败边界）：

```bash
SMART_MONEY_SOURCES_FILE=reports/direct_evidence/20260929T135437Z/sources.json \
SMART_MONEY_EVIDENCE_DIR=reports/direct_evidence/20260929T135437Z/evidence \
SMART_MONEY_QWEN_TIMEOUT_SECONDS=600 \
python -m smart_money analyze \
  --input reports/direct_evidence/20260929T135437Z/fed.input.json \
  --output reports/direct_evidence/20260929T135437Z/fed.replay.json
```

输入内的研究截止时间与共享预算同样生效；新一轮联网研究须明确新截止时间和预算，不能把重跑
伪装成旧时点复原。本次全流程样例中领域模型超过输入的 240 秒单请求上限，已保留失败；
`fed.stage-validation.json` 单独记录 600 秒模型时限下的原文交接复验，不覆盖或替代该全流程结果。
其中保存了真实 Qwen 的领域分析与独立核验输出；修复引用交接后，重放保存的核验响应，
3 项主张通过现有检查（2 项公告事实、1 项合成输入说明）。未完成全流程成稿或真实钱包上线验收。
核验编号从已校验的原文引用生成，模型仍须判断主体、语义和必要限定；矛盾编号或失效引用不能通过。

结果中 `wallet_report`、`domain_report`、`skeptic_report` 是分析，
`evidence_contract`、`claims`、`verification_report`、`policy` 记录证据与质量检查，
`publication` 是分析文案。命令成功退出只表示生成了结果；证据不足、模型降级或核验失败
会反映在结果中。该命令不会发布消息、下单或生成模拟跟随账本。

监听冻结名单中已有的显示名称；钱包说明同时呈现买卖、份额、成交金额、均价与前后仓位，
缺少历史业绩或可比投入时明确保留缺口。Gamma 市场原件通过既有市场规范化器进入研究，
已保存的同板块官方榜单记录随画像冻结，榜单 PnL 标明周期与获取时间，不代替独立事件业绩或资格。
`description` 中的规则使用实际获取时间；事后取得的规则只能支持研究更新。
公开发布能力不足不再提前取消领域分析，原始材料仍可供分析并由最终政策决定发布范围。
模型只接收一份原文及必要的结构化字段，完整审计保留在结果中；正文使用 `publication.content_text`，
不要求把同一篇文章复制到多个输出字段。外部取证停用时，这些改动不会凭空补出新闻、战绩或动机。
逐条核验按三项分批，全部主张和段落都须覆盖；空审核标记失败，预算在批次间不重置。
输出截断且未返回可修复文本时直接保留失败；没有生成正文时不调用成稿核验。

MAS 模块和类型按职责命名：`research/classifier.py`、`research/contracts.py`、`research/crypto.py`、
`research/evidence.py` 与 `publication/policy.py`，调用方统一使用 `CaseClassifier`、`EvidenceItem`、
`PublicationPolicy` 等名称。旧代次文件及其兼容导入已删除；格式修订号保留以识别历史证据和审计。
当前 MAS 的 `evidence` 直接采用 `EvidenceItem`，结果格式标识为 `result_schema_version=3`。
原始来源快照、哈希、时点和独立性信息保留。旧任务规划、独立深查、报告适配、主张图、故事评分、
关键词场景拼装与天气高斯概率模型已删除；结果不再包含旧 `plan`、`claim_graph` 和深查报告字段。
已删除面板预取、RSS/Google 搜索回退、重复行情补充及其独立来源配置；取证按证据合同缺失字段进入同一活动链。
各领域复用统一证据合同和现有领域调用。钱包事实由代码摘要，规则解读先于取证；格式至多修复一次。
`verification_report` 的支持判断要求原始快照中的定位和原文，`publication.claim_refs` 以 `字段名:段落序号`
关联已核验主张；`draft_review` 对实际标题、各正文和摘要重新核验。原文、输入、模型阶段或草稿变化会使旧复核失效。
最终硬检查先于唯一发布判断；缺复核、漏审、失败及旧结果均不能通过。现有活动额度在首次取证和最多一轮补查间共用，
模型与来源的实际请求、传输重试和格式修复使用同一 `context.research_limits`：
`max_requests`、`total_seconds`、`request_timeout_seconds`、`max_input_chars`、`max_response_bytes`。
`agent_runtime.sharedBudget` 保存使用量；监听器对已保存失败结果的重试继承计数和开始时间。
缺少参数时仍记录 `SHARED_RESEARCH_BUDGET_UNCONFIRMED`，局部保护默认值不代表统一预算已验收。
主张依赖缺失、循环或不受支持时不得继承通过；反证要求的限定须经过主张和成稿两次核验。
`stage_history` 保留各轮输出及输入摘要，研究截止时间在最终取证后冻结，避免以请求创建时间拒绝本轮材料。
真实模型语义质量及联网研究尚未验收。
`signals/follow_policy.py` 保留前向验证、执行条件、价格区间、板块准入及钱包事件去重门槛。
钱包默认执行一次；加 `watchlist --watch` 可持续维护名单。监听与 MAS 已有命令接线和离线故障验证，实际合格钱包的在线全链路尚未验收。
模拟账本、结算回写、审核和发送恢复仍缺接线，不能称为完整在线服务。回测及其命令不恢复。
详细入口和缺口保留在本地设计文档 `docs/IDEA.md`，其中的运行记录不随公开源码分发。已删除指向退役脚本的旧用户级 Smart Money 服务与定时器；
外部数据库、研究结果和当前本地 Qwen 服务保留。

## 钱包发现与观察名单

```bash
SMART_MONEY_ENV_FILE=.env smart-money watchlist --output var/wallet_acceptance.json
smart-money watchlist --input wallet_facts.json --offline --output /tmp/wallet-facts-check.json
```

默认读取支持板块周/月盈利榜各前 50 条；每个板块历史榜在首次运行及距离上次成功读取满 7 天时补充。
可用 `--categories crypto politics` 限定本次榜单预算，已有候选和观察地址仍进入同一维护队列。
先提交候选，再逐钱包确认映射和近期真实成交摘要；历史尚未读完可按 IDEA 的研究理由分配有限探索名额。
主评价为最近 180 天正式结算的独立事件，90 天为嵌套子集；数字门槛不变。先枚举参与事件，
再读取这些事件的成交、费用和操作；保留 180 天之前的入场成本及未结算风险。
官方接口和本地数据库均只读访问，来源配置不借用 MAS 的新闻许可。
接口按[官方游标合同](https://docs.polymarket.com/api-reference/data-api/overview)读取；
不足一页不等于结束，超时、无游标、重复游标或达到分页预算均保留缺口。

`--input` 可提供本地线索和已有事实，字段如下。事实字段采用官方 v2 的 snake_case 格式，市场分类采用 Gamma 格式。

| 字段 | 内容 |
| --- | --- |
| `as_of` | 带时区的统一截止时间；离线必填，联网只允许当前时点 |
| `candidates` | `account`、`sector_id`、`source`；可附有证据的 `trading_wallet` / `mapping_evidence` |
| `profiles` | 账户标识到 `{wallet, evidence}` 的已核实映射；缺失映射保留待核查 |
| `boards` | 离线榜单：`category`、`period`（week/month/all）、`rows`、`coverage.complete` |
| `histories` | 以交易钱包为键的事实快照，包含 `wallet`、`as_of`、四类事实列表、覆盖和市场/结算映射 |

四类事实是 `closed_positions`、`open_positions`、`trades`、`activity`；
`coverage` 分别记录分页完整性，`markets` 和 `resolutions` 按 condition ID 索引。
每次评估保存原始事实与事件摘要，名单记录引用该评估。示例结构和核心边界见
[钱包流程测试](tests/markets/test_watchlist.py)。离线模式不会调用网络，缺少的输入也不会回退联网。

本地读取使用配置文件中的 `xue_lab_ip`、`xue_lab_user`、`xue_lab_pwd`，通过已验证主机密钥的 SSH
读取 XUE-LAB 上现有 PostgreSQL（`XUE_LAB_POSTGRES_PORT` 默认 45432）；数据库名、用户和密码使用
`POLYDATA_POSTGRES_DATABASE`、`POLYDATA_POSTGRES_USER`、`POLYDATA_POSTGRES_PASSWORD`。配置示例见 `.env.example`。
服务端使用已有 `psql`，读取处于只读、可重复读事务；参与事件按 condition 分页，事实按区块与日志游标每页 1,000 条读取。
本地查询有限时，失败保留 SQLSTATE 和游标，不在 SSH 内睡眠重试，也不输出连接凭据。
市场及 CTF 正式结算优先复用本地事实，缺失项再走原有官方接口的有界批次；仍缺失时单独记录，不能隐藏潜在亏损事件。

非交易明细复用同一台 XUE-LAB 的 ClickHouse `poly_orderfilled.non_trade_cashflows`，
结合 `block_timestamps` 限定截止时间，通过同一 SSH 读取，使用 `.env.example` 中已有数据源的 HTTP 端口及账户配置。
不新增数据采集器或数据库表；查询到零行不代表钱包没有非交易操作。

历史快照的 `local_history` 保存本地成交、链上费用、市场、正式结算及扫描区间证据；离线输入使用同一结构。
`coverage.boundary` 保存截止时间前后两个相邻区块的编号、时间、hash 和来源，复用现有 `block_timestamps`。
覆盖只检查到截止区块，不追随数据源最新区块；缺相邻区块证据时保留待核查，不能用最后一笔成交推测边界。
程序计算 `reconciliation` 并写入不可覆盖的评估记录，不接受输入里的 `complete` 或 `fees_included` 作为通过依据。
检查包含：扫描区间连续性和截止时间、官方与本地成交的数量/金额/身份、活动成交、赎回、当前持仓数量及
扣除实际费用后的持仓净收益。不同来源的同一成交按交易哈希、token 和方向比较，原始链上记录按日志身份去重。
官方无日志编号的相同成交行保留，再与链上唯一日志核对；不能仅因金额相同而合并。活动现金按买入含费、卖出扣费核对。
奖励、做市/吃单返利和利息不计入交易盈利。无 token 的赎回只有在 condition 与结果唯一对应持仓时才能归属。
二元完整集合的拆分和合并同时更新两个 token 的份额，现金按 condition 核对，不人为分摊到单个结果；
本地与官方操作按交易、condition 和类型核对，链日志重复不重复计账，冲突保留待核查。
一个赎回无法解释时继续核查其他操作。账目核对通过不解除组合交易方式的方向资格复核。

`fees_by_asset` 分别记录现金与份额费用；`fee_transactions_required/verified` 显示所需/已核验交易数，
缺口按交易写入 `repair_targets`。适用部署、退款合同及实测缺口统一维护在 [IDEA 第 6.1 节](docs/IDEA.md#61-钱包流程的修复要求与进展2026-09-28)。
金额计算使用 Decimal；数量容差 0.0001 份、现金核对容差 0.01 USDC。费用不重复从官方净盈亏中扣除。

官方分页结束不能证明声明事件集合完整。本地扫描缺口、数据源滞后、未解释的差额、费用退款/组合操作归因缺失时，
均保留待核查。原始差额和证据引用保存在评估中。
只有上述核查及原有资格/质量规则都通过才进入合格观察；本命令不会启动采集器补写源数据库。
未结算持仓还须有同截止时点的 `valuation_as_of` 和 `valuation_evidence`，否则风险复核保持未知。

观察资格沿用 V4 数字门槛，使用 180/90 天结算窗口和独立规则版本；双向事件保留审计盈亏，但排除方向成绩。
盈利集中、无亏损 PF 分母、提前退出归因、多市场组合归因或数据缺口均不能自动通过。
历史合格的新对象为 `SHADOW_OBSERVE`，可正常观察；已有前向状态和 `manual_paused` 保留。
高盈利和组合观察策略函数仍保留，这个命令不自动授予其替代资格，也不授予跟单资格。

唯一输出文件同时保存候选、wallet × sector 当前记录及不可覆盖的评估引用；重复输入不会重复建档。
存储格式为 `schema_version=3`：评价和 `history_ref` 指向外部不可变压缩档案，热文件不保存历史集合。
核查时用 `smart_money.infrastructure.wallet_storage.load_history(state, history_ref)` 按需还原独立副本；
监听只读小型评价与快照身份。不支持的格式报错并保留原文件，旧格式通过下述显式命令迁移。
获取失败保留旧画像与最近成功引用，标记 `stale` 并关闭正常观察资格。重新成功核查后可恢复。
`policy` 保存本次实际参数及引用；`tasks` 只保存阶段、固定截止、游标摘要、错误、退避时间和可续作的 `history_ref`，
完整获取进度和成员清单随历史档案保存。
默认每轮至多处理 50 个新钱包及 50 个已有钱包，按板块和等待时间轮转；每钱包至多 60 秒或 10 次读取。
当前执行并发为 1，HTTP 连接 5 秒、总超时 20 秒；失败最多 2 次短间隔重试，随后隔日尝试，主机退避跨重启保留。
资格最多有效 48 小时，并提前在活跃/窗口边界复核。正常名额 100、探索 50，探索 7 天到期后需新成交证据才能续期。
摘要、数据完整性、历史资格、前向状态和人工暂停分别保留；局部板块缺口不否决另一已完整核查的板块。

针对已保存的一次评价重试缺失事实：

```bash
SMART_MONEY_ENV_FILE=.env smart-money watchlist --repair --output var/wallet_acceptance.json
```

`--repair` 固定原钱包和截止时间，复用已成功的官方快照，失败的成交/活动分页从保存的游标续取，
可加 `--wallet 0x...` 仅修复一个已保存的实际交易钱包，其余钱包记录保持不变；省略则检查全部候选。
消费 `reconciliation.repair_targets`，按交易或缺失区块区间补读本地成交与非交易操作；续页发现的新交易也加入补读范围。
已有事实去重合并，冲突保留待核查；完全缺失或身份不符的本地快照须重新读取。只补缺失的市场元数据，
不刷新榜单，不与 `--input`、`--offline`、`--as-of` 混用。缺失的历史持仓快照不能用当前持仓补造，须重新进行当前评价。
每个切片先持久化证据和板块评价，再原子发布包含游标的小名单；中断恢复从上次已提交切片续作，最多重读未提交切片。
当前规则核查完整的钱包直接复用，不重复读取。旧评估保留，新证据生成新引用。
补取只读取已有上游，不会启动扫描器或修复源数据库；退款回执缺失、不支持的归属路径等缺口仍保留待核查。
写入使用单写者锁和原子替换；标准输出是本次摘要，退出成功仅表示名单已保存，需同时检查
`discovery_failures`、`stale`、各记录的 `status` 和 `reasons`。`summary.listening_wallets` 是名单分配的去重监听地址，
包括探索；`qualified` 是合格板块记录数，`exploring` 是探索钱包数，不能混为同一指标。
链上监听读取对应记录的生效时间、板块和历史证据引用，不能仅根据该地址列表授予交易资格。

名单只保存当前状态（格式 3），历史制品保存在名单所引用的 `.archive` 目录，按需解压读取。
原始成交、回执、完整评价和修复明细不再内联。原因有分类计数、最多 3 个示例和完整明细引用；
名单超过 10 MiB 或单条记录超过 8 KiB 时拒绝发布，旧有效视图保留。

旧格式只支持显式隔离迁移，先冻结写入者并保留一致输入：

```bash
smart-money watchlist --migrate-storage var/wallet_acceptance.json --output var/wallet_storage_stage.json
```

迁移保留旧 ID，分块流式读写并核验原值与所有历史成员引用；中断后用同一冻结源和输出路径续作。
确认人工状态、资格、指标及引用等价后，再停旧读取器、切换当前文件和档案路径，启动新进程。
不得把旧快照直接覆盖切换后新增的观察；原件仅在恢复和引用检查通过后删除。此命令不改变生产原件。

持续维护同一份名单：

```bash
SMART_MONEY_ENV_FILE=.env smart-money watchlist --watch --output var/wallet_acceptance.json
SMART_MONEY_ENV_FILE=.env smart-money watchlist --due --output var/wallet_acceptance.json
```

`--watch` 默认每 300 秒检查；`--due` 只执行一轮到期工作，便于已有调度器调用。每日按 UTC 日期读取周/月榜并
把全部已有候选（含暂停和未合格对象）加入到期待办，逐轮按预算推进；历史总榜仍按距上次成功读取满 7 天补充。
实时核查在每个钱包开始读取时确定其评价截止时间，避免沿用耗时的候选发现或前一钱包开始时间；历史修复仍固定原快照截止。
复用旧事实，所选事件的成交/活动按时间增量读取，链事实按新区块及已知缺口补取，完整回执不重复获取；
当前/已关闭持仓、参与索引、覆盖证据和结算重新核对。未完成任务沿用原截止与分页，不把残缺历史当作完整增量基线。

结算检查只读 XUE-LAB 已有 `oracle.oracle_events` 中 CTF 的正式结算，按已知 condition 的事实指纹发现新增、
迟到或修订结果，只刷新关联钱包；不依赖结算时间水位，也不把市场到期或价格接近 1 当作结算。
`maintenance` 与名单原子保存：先保存触发依据与钱包待办，再逐钱包处理；失败保留旧指标/证据并标记过期，待办下轮重试。
来源检查失败保留原结算指纹并报告 `settlement_check=FAILED`；不能据此声称没有新结算。
维护命令退出 1 表示还有失败待办、榜单失败或结算检查失败，退出 0 不表示钱包合格。
人工暂停和前向状态保留；本次触发更新历史画像与资格，前向样本及结算反馈仍待第 3.9 节接线，不拿历史成绩晋升前向状态。
监听进程继续读取这同一名单路径；维护耗时不会占用监听线程。常驻部署方式见下文。

## 从名单监听并提交 MAS

安装相邻 `market-data` 仓库的现有包，复用其 RPC 客户端和成交解码器；再加载配置中的
`POLYDATA_ORDERFILLED_RPC_URLS`（逗号分隔）。节点必须是 Polygon 137，支持 `finalized`、日志、完整回执及近期指定区块余额读取。

```bash
python -m pip install -e ../market-data
python -m pip install -e '.[monitor]'
SMART_MONEY_ENV_FILE=.env smart-money monitor --watchlist var/wallet_acceptance.json --output var/wallet_monitor.json --watch
```

首次运行只建立确认区块的份额基线，历史成本保持未知，不把旧仓位当成新交易。
地址被名单纳入监听后，其确认 BUY / SELL 都作为 `SMART_MONEY_TRADE` 交给现有 MAS，包含卖出、跨板块和探索地址。
历史资格、前向状态、配对持仓、未知基线或费用作为分析背景，不再阻断真实成交研究；未知动作保持 `UNKNOWN`。
监听成员身份按实际应用时间冻结，暂停或移出后停止新信号；名单内期间的断线补漏带延迟提交，不冒充实时前向样本。

默认一次处理至多 100 个区块及一条待研究观察；`--watch` 持续运行，默认每 5 秒扫描。
`--offline-mas` 仅关闭 MAS 模型和外部取证，链上读取仍联网。正常地址、有效探索和已有库存/研究跟进责任组成实际监听集合；
只有该集合为空时才返回 `NO_MONITORED_WALLETS` 并退出 1。历史合格数不再决定能否提交 MAS。
读取、余额或确认链冲突时为 `BLOCKED`，保留旧游标；名单短暂读取失败保留上次已接受监听配置并报告错误，资格有效性作为独立背景提供。
启动自检实际读取近期 CTF 日志、完整回执及指定区块余额；链 ID 错误、同步中或能力未证实时停止扫描。
持仓清单、市场映射或新 token 的历史余额暂不可读时，保存待补原因和可信成交，返回 `RECOVERY_PENDING`（退出 1）。
后续运行自动重试；节点明确报告历史状态不可用（包括裁剪）时，须先核实新确认区块的余额才能重建份额基线，保存
`REBASED_WITH_HISTORY_GAP` 区间。游标仍逐批补读中间交易，区间内未知仓位不冒充新开仓或前向样本。
映射恢复以 `supplement` 补充原观察，保留原始证据和首次发现时间；尚未提交的确认成交沿原编号首次提交 MAS，已提交的不会重复入队。
退出 0 只说明本批处理完成，须检查观察中的抑制原因、研究状态及 MAS 结果质量。

状态文件保存唯一交易回执、市场映射、资格版本引用、仓位、观察、冻结研究请求和结果，使用现有原子写入与单写者锁。
请求引用已有观察证据和不可变市场快照，调用 MAS 时还原输入，不为每条待研究请求再次保存原始市场事实。
先提交完整批次，再推进下次读取和提交研究；研究在线程中运行，不阻塞扫描。失败最多尝试 3 次，再标记
`REVIEW_REQUIRED`；已提交结果不重复研究。进程中断且结果未保存时可能再次调用模型，观察和份额不会重复累计。
`DONE` 表示 MAS 返回且没有可重试的模型传输失败，不表示发布通过；显式离线执行记为 `OFFLINE_DONE`。
可重试失败保存为 `FAILED`，保留每次尝试和此前结果；已设置统一限额但进程中断导致使用量未知时转人工复核。
同钱包、同 token 的后续观察引用父研究；确认买卖及可安全解释的非交易更新，
沿原研究入口形成新结果，不覆盖父结果、冻结资格和原始证据。规则更正、新材料等独立事件尚未自动触发。
命令不发送文章或下单。前向/模拟账本反馈、审核发送恢复及长期运行验收仍待完成；
当前只核对已知 token 范围，状态文件仍整体读取和原子重写。

本机持续运行复用两个命令，由 `deploy/systemd/smart-money-watchlist.service` 和
`deploy/systemd/smart-money-monitor.service` 管理；已启用开机启动、退出重启和用户 linger。
两个服务共用项目 `.env`，沿用已有 `var/wallet_acceptance.json` 作为唯一名单，监听状态保存到
`var/wallet_monitor.json`。合格地址自动加入；失去资格、过期或暂停时移出正常观察，保留历史及已有仓位记录。
监听每轮读取名单变化，名单维护每 300 秒检查到期工作；没有合格地址时继续等待，不退出常驻循环。
服务使用在线 MAS，没有设置 `--offline-mas`；模型失败沿现有持久化队列重试，不阻塞扫描。
网络访问使用 `.env` 中已有的 HTTP 代理配置；本地 Polygon 补同步期间，优先使用已核验的原配置备用 RPC，
链 ID、确认块时效、回执和指定区块余额检查保持不变。

```bash
systemctl --user status smart-money-watchlist smart-money-monitor
journalctl --user -u smart-money-watchlist -u smart-money-monitor -f
```

部署到同一目录的新环境时，先配置 `.env`、Python 环境与已有数据源，再安装两个单元：

```bash
systemctl --user enable --now "$PWD/deploy/systemd/smart-money-watchlist.service" "$PWD/deploy/systemd/smart-money-monitor.service"
```

服务运行不代表已有合格钱包；名单分配看 `summary.listening_wallets`、`qualified` 和 `exploring`，监听进度看状态文件的
`health`、`cursor`、`observations` 及其 `research` 结果。缺失事实仍保留待核查，不降低筛选条件。

2026-09-29 的 180 天筛选与探索改动仅完成代码、离线检查及独立临时文件的有界真实读取，未切换以上生产服务。
切换时先停止两个服务，保留一次原名单和监听状态的回退副本，核对路径、规则版本及人工状态后，
重启 `smart-money-watchlist`、`smart-money-monitor`。检查任务游标推进、探索/资格分别计数、节点健康和观察保存，
不能只检查进程存活。需要回退时先停止新进程，再恢复上一已审查代码版本；保留新产生事实与审计引用，
不直接用旧状态覆盖新证据，也不使用 `git reset --hard` 清除本工作区既有改动。

## 检查

```bash
make test lint format-check typecheck
python -m build
```

默认测试离线执行；CI 同时验证安装后的 wheel 和真实分析命令。

## 公开源码前的检查

环境凭据、运行数据、研究报告、压缩归档、录屏、浏览器验证目录和包含实际运行记录的
`docs/IDEA.md` 只在本地保留，由 `.gitignore` 排除。前端运行所需的小型图片与明确标注的 demo 数据可提交。

发布前直接复核暂存差异、文件大小、凭据、个人信息及业务资料的公开范围；公开文件控制在单文件 **1 MiB** 以内。
忽略规则不影响已经暂存或提交的内容。已有私密历史的开发仓库保留在本地，公开发布使用不继承旧提交的独立源码副本。
