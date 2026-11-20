# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## 核验分派

- `GET /api/verifications`：当前有效任务、分派汇总与最近交接事件。
- `GET /api/verifications/<parcel>`：该票全部版本、有效任务与交接事件。
- `POST /api/verifications/sync`：按批次重算分派，幂等。body可含`run_id`（缺省最新批次）、`policy`（覆盖分派策略）、`now`（测试用时间锚点）。
- `POST /api/verifications/<task_id>/review`：body需`version`与`decision`（已核验/退回复核）；版本失效或任务已被取代返回409。

`POST /api/runs`的返回新增`verification`字段，包含本次分派结论。批次保存时自动执行一次同步；同一补传重试的裁决为`noop`，不新增任务。
