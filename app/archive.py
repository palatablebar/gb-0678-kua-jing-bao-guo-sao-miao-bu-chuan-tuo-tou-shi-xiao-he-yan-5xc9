import csv,io,json
from app import verification
def build_archive(run):
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","source_lines","reason","evidence"])
    meta=run["manifest"]
    for row in run["stages"][-1]["rows"]:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"accepted",json.dumps(row.get("_sources",[])),row.get("status",""),json.dumps(row,ensure_ascii=False)])
    for row in run["stages"][-1].get("cumulative_quarantine",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"quarantine",json.dumps(row.get("_sources",[])),row.get("reason",""),json.dumps(row,ensure_ascii=False)])
    # 核验任务的每个版本都导出：旧版任务、交接原因与原始扫描证据不能被覆盖删除。
    parcels=sorted({row["parcel"] for row in run["stages"][-1]["rows"] if row.get("parcel")})
    for parcel in parcels:
        try:detail=verification.detail(parcel)
        except Exception:continue
        for task in detail["revisions"]:
            writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"verification_task",json.dumps(task["source_lines"]),task["handoff_reason"],json.dumps(task,ensure_ascii=False)])
        for event in detail["events"]:
            writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"verification_event",json.dumps([]),event["reason"],json.dumps(event,ensure_ascii=False)])
    return "﻿"+stream.getvalue()
