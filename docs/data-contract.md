# 业务记录约定

排班表按片区与半小时时段记录 assigned、sequence 和 probability。sequence 是一次整体抽签产生的连续排班，不能拆成独立时段抽签。probability 是该完整序列在所属 randomization_set 内的抽取概率；所有候选序列见 designs；schedule.block_offset 是该序列内位置，event_at 为时段起点。
exposures 的 event_at 是曝光时间，received_at 是入库时间。订单以首次曝光所属片区归属，成交和金额在订单事件中记录，跨时段成交不自动归入成交时段。closures 是运营登记的停投区间，不意味着该时段没有流量。空缺曝光不等于零曝光。
existing exposure_coverage 按排班统计观察到的曝光、订单及停投标记，不做试验推断。schedule_effect 在同一份可见数据上估计排班分配效果，两者共用口径。所有时间为 UTC，区间左闭右开。

## 共同版本语义
所有示例记录均为本地合成，不包含真实客户资料。received_at 不晚于观察截止的记录才可见；缺省该字段的配置为静态配置。revision 表示同一业务身份的订正次数，不是新样本。时间不带时区时按 UTC 解读。
分析请求会保存可见数据的内容摘要及完整内容；重复 request_id 与原请求完全相同时返回原结果，即使源文件后来改变。其他请求内容复用该编号返回冲突。新观察须使用新编号。


`designs` 按 randomization_set 列出完整候选排班及抽取概率；`schedule.sequence` 引用实际抽取序列。每个 exposure 有独立 event_at 与 coupon_claimed，订单含 event_at、exposure_period 与所属 region。`coverage` 区分已确认观测和未报齐时段；只有已确认观测却没有曝光时才能解释为零。


## 快照样例字段补充

以上字段说明对应当前初始快照中的本地合成记录；示例不存在的历史字段不视为隐藏接口要求。


## 已有能力与字段边界

designs 中每行是一个可抽取的完整八时段序列，概率在 randomization_set 内归一；schedule.block_offset 定位序列内位置。同一片区的 period 从零递增，event_at 为时段开始。coverage 的 observed 表示该片区时段已确认报齐，只有这种情况下无曝光行才是确认零曝光。exposures 的 coupon_claimed 表示实际领取；orders 通过 exposure_id 指向首次曝光，即使 event_at 已跨时段也不改变归属。exposure_coverage 只输出覆盖，不计算分配效果或不确定性。


## schedule_effect 分配效果

请求通过 `POST /v1/analyses` 提交 `operation=schedule_effect`，可用 `options.carryover_periods` 指定残留影响窗口（半小时时段数，默认 `[0,1,2,3]`，非负整数、不重复、最多 48）。结果可通过 `GET /v1/analyses/{request_id}` 重新打开，并保留观察截止、所用排班、候选序列、分析选项和排除理由。

估计对象是 randomization_set 抽取出的完整排班序列，不是单条订单，也不是单个半小时时段。`assignment_effect` 按 block_offset 分层，用逆抽取概率加权，跨排班方案比较同一序列位置上的补贴开/关时段，标准误按 randomization_set 聚类并按 t(G-1) 给出 95% 区间。逐组 `raw_within_set_contrast` 和 `descriptive_arm_rate_difference` 只作描述：候选序列互为镜像时，臂与序列内位置共线。

`descriptive_claim_contrast` 是实际领券人群的对比，仅描述、不参与效果估计；领券发生在分配之后，包含选择效应，关补贴时段出现的领券按原排班保留，不据此重分时段。

证据规则：停投（closures）、coverage 非 observed、观察截止时尚未结束的时段、有曝光但没有任何可见订单的时段，以及设计校验失败的 randomization_set 都不进入比较。只有 coverage=observed 且无曝光记录才记为确认零曝光。所有排除都按事先规则列出具体片区时段，不补零，也不按成交结果挑时段。残留窗口 k 只使用当前及此前 k 个时段都可用且同臂的时段，因此窗口越大可用证据越少；`robustness` 汇总每个窗口的方向、区间和是否跨零。

输入按业务身份与 revision 取最新可见记录；同一 request_id 的重复请求返回原结果，选项不同则冲突。
