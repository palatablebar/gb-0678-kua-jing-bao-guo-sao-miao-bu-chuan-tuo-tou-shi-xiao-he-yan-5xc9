# 深度登记 跨境包裹扫描补传与妥投时效核验

规模：中型。使用者：跨境集运客服质控员。核心规则：以事件发生时刻重建路径，补传不改变既有妥投时间；承诺时钟按目的地节假日停表。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 扫描采信 | 查看并运行扫描采信 | `/api/scans` | `app/modules/m1_scans/rules.py` | `tests/test_m1.py` |
| M2 节点映射 | 查看并运行节点映射 | `/api/nodes` | `app/modules/m2_nodes/rules.py` | `tests/test_m2.py` |
| M3 时区归整 | 查看并运行时区归整 | `/api/timezones` | `app/modules/m3_timezones/rules.py` | `tests/test_m3.py` |
| M4 路径裁决 | 查看并运行路径裁决 | `/api/paths` | `app/modules/m4_paths/rules.py` | `tests/test_m4.py` |
| M5 时效计算 | 查看并运行时效计算 | `/api/timeliness` | `app/modules/m5_timeliness/rules.py` | `tests/test_m5.py` |
| M6 赔付对账 | 查看并运行赔付对账 | `/api/compensation` | `app/modules/m6_compensation/rules.py` | `tests/test_m6.py` |
| M7 批次归档 | 查看并运行批次归档 | `/api/archives` | `app/modules/m7_archives/rules.py` | `tests/test_m7.py` |
| M8 核验分派 | 查看并复核补传后的妥投核验任务 | `/api/verifications` | `app/modules/m8_verifications/rules.py` | `tests/test_m8.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 默认解码失败 | 回退GB18030 | `app/modules/m1_scans/rules.py` | `tests/test_m1.py::test_legacy_encoding_real_sample` |
| TRAP-02 | BOM | 首列名多字符 | utf-8-sig | `app/modules/m1_scans/rules.py` | `tests/test_m1.py::test_bom_real_sample` |
| TRAP-03 | 带引号换行 | 拆成两条 | csv逻辑行 | `app/modules/m1_scans/rules.py` | `tests/test_m1.py::test_quoted_newline_is_one_record` |
| TRAP-05 | 列数不匹配 | 静默错列 | 隔离原行 | `app/modules/m1_scans/rules.py` | `tests/test_m1.py::test_bad_columns_real_sample` |
| TRAP-08 | 前导零ID | 编码变形 | 全程字符串 | `app/modules/m1_scans/rules.py` | `tests/test_m1.py::test_identifier_preserved_real_sample` |
| TRAP-14 | 重复键冲突 | 后写覆盖 | 保留首条并隔离冲突行 | `app/modules/m1_scans/rules.py` | `tests/test_m1.py::test_duplicate_conflict_real_sample` |
| TRAP-18 | 空态三态 | 一律成功 | 分别错误码 | `app/modules/m1_scans/rules.py` | `tests/test_m1.py::test_three_empty_states` |
| TRAP-26 | 旧版本提交 | 覆盖新结果 | 版本条件更新 | `app/store.py` | `tests/test_e2e.py::test_stale_review_409` |
| TRAP-06 | 全角时限 | 数字转换失败 | Decimal归一 | `app/values.py` | `tests/test_m3.py::test_numeric_normalization` |
| TRAP-07 | 时限哨兵 | 按零赔付 | 隔离缺失 | `app/values.py` | `tests/test_m3.py::test_real_sentinel_not_zero` |
| TRAP-11 | 目的地偏移 | 同刻多桶 | UTC比较 | `app/values.py` | `tests/test_m3.py::test_equivalent_timezone` |
| TRAP-12 | DST自然日 | 固定24小时 | 按当地午夜切片 | `app/modules/m5_timeliness/rules.py` | `tests/test_m5.py::test_dst_actual_elapsed_hours` |
| TRAP-13 | 迟到妥投补传 | 按上传时间赔付 | 发生时间重建 | `app/modules/m4_paths/rules.py` | `tests/test_m4.py::test_late_upload_event_time_decides` |
| TRAP-23 | 假日端点 | 误算全天 | 当地日半开切段 | `app/modules/m5_timeliness/rules.py` | `tests/test_m5.py::test_holiday_full_day_stops` |
| TRAP-27 | 补传后重复排队 | 每票各自建任务 | 每票唯一有效任务+部分唯一索引 | `app/verification.py` | `tests/test_m8.py::test_one_active_task_per_parcel_across_revisions` |
| TRAP-28 | 双人同时处理 | 结论变了仍留原负责人 | 按原/新时效结论裁决续办或改派 | `app/modules/m8_verifications/rules.py` | `tests/test_m8.py::test_conclusion_change_hands_off_to_dispute_owner` |
| TRAP-29 | 同补传重试 | 再建一版任务 | 证据摘要相同即复用 | `app/verification.py` | `tests/test_m8.py::test_same_retry_is_idempotent` |
| TRAP-30 | 替换失败 | 旧版被删或半写 | 单事务回滚并保留旧版与原因 | `app/verification.py` | `tests/test_m8.py::test_failed_handoff_keeps_previous_task` |

## 对账规则

输入原始逻辑行=最终采纳来源行+隔离来源行+重复行+空白行+分段表头。派生记录用_sources保存完整来源，因此分组不丢行。业务预期合计与上游申报合计之差等于逐行差异之和。结果按输入摘要、规则版本及参数产生唯一批次，事务写入；同源重跑只复用已有批次。

## 核验分派

补传到达后按票裁决唯一有效核验任务，结论见下表。每票由部分唯一索引保证同时只有一条 active 任务；历史版本软取代、不删除；事件表只追加。

| 情形 | 判定 | 负责人 | 截止时间 |
|---|---|---|---|
| 首次分派 | 无 active 任务 | 包裹指定负责人，缺省 default_owner | 当前 + window_hours（默认 24h） |
| 证据变化、时效结论不变 | 新旧 sla 相同 | 原负责人续办 | 原截止未过期则保留，过期则顺延 |
| 证据变化、时效结论改变 | 新旧 sla 不同 | 具备争议处置权限的人 | 当前 + dispute_window_hours（默认 8h） |
| 同一补传重试 | 证据摘要与 active 相同 | 不新建、不重复分派 | 沿用当前任务 |
| 改派对象无争议处置权限 | 角色不在 dispute_roles | 拒绝改派 | 保留旧任务 |

证据摘要只取路径裁决实际采纳的扫描（scan_id、节点、发生/上传时间、目的地、承诺时限），因此同一次补传重试摘要稳定；批次号不同也不会重派。策略可经批次参数 `verification` 或同步接口的 `policy` 覆盖（default_owner、dispute_owner、roster、dispute_roles、window_hours、dispute_window_hours、owners）。

## 已知不覆盖

4MB以上文件需预先按业务键分批；不模拟生产支付与外部回调。人工复核只变更批次状态，不改写原始记录。核验分派不提供登录与权限校验，角色权限来自策略配置；未覆盖跨批次的工单系统同步。
