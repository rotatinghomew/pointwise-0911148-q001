# 业务记录约定

排班表按片区与半小时时段记录 assigned、sequence 和 probability。sequence 是一次整体抽签产生的连续排班，不能拆成独立时段抽签。probability 是该完整序列在所属 randomization_set 内的抽取概率；所有候选序列见 designs；schedule.block_offset 是该序列内位置，event_at 为时段起点。
exposures 的 event_at 是曝光时间，received_at 是入库时间。订单以首次曝光所属片区归属，成交和金额在订单事件中记录，跨时段成交不自动归入成交时段。closures 是运营登记的停投区间，不意味着该时段没有流量。空缺曝光不等于零曝光。
现有 exposure_coverage 按排班统计观察到的曝光、订单及停投标记，不做试验推断。所有时间为 UTC，区间左闭右开。

## 共同版本语义
所有示例记录均为本地合成，不包含真实客户资料。received_at 不晚于观察截止的记录才可见；缺省该字段的配置为静态配置。revision 表示同一业务身份的订正次数，不是新样本。时间不带时区时按 UTC 解读。
分析请求会保存可见数据的内容摘要及完整内容；重复 request_id 与原请求完全相同时返回原结果，即使源文件后来改变。其他请求内容复用该编号返回冲突。新观察须使用新编号。


`designs` 按 randomization_set 列出完整候选排班及抽取概率；`schedule.sequence` 引用实际抽取序列。每个 exposure 有独立 event_at 与 coupon_claimed，订单含 event_at、exposure_period 与所属 region。`coverage` 区分已确认观测和未报齐时段；只有已确认观测却没有曝光时才能解释为零。


## 快照样例字段补充

以上字段说明对应当前初始快照中的本地合成记录；示例不存在的历史字段不视为隐藏接口要求。
