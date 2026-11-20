"""补传后的妥投核验分派规则。

裁决目标：每票始终只有一条有效核验任务。

- 首次到达：按包裹负责人（缺省为 default_owner）建任务。
- 补传未改变时效结论：原负责人续办，避免责任与上下文反复转移。
- 补传改变时效结论：改派具备争议处置权限的人重新核验。
- 同一补传指纹重复到达：复用既有任务，不新建、不重复分派。
- 改派失败：保留旧版任务（由 store 的事务回滚保证）。
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.values import instant

EVIDENCE_FIELDS = ("scan_id", "node", "event_time", "received_time", "destination", "promised_hours")
DEFAULT_POLICY = {
    "default_owner": "客服A",
    "dispute_owner": "质控-甲",
    "roster": {"客服A": "客服", "客服B": "客服", "客服C": "客服", "质控-甲": "争议处置", "质控-乙": "争议处置"},
    "dispute_roles": ["争议处置", "质控"],
    "window_hours": 24,
    "dispute_window_hours": 8,
    "owners": {},
}


def policy_of(parameters=None, override=None):
    """合并默认策略、参数与请求级策略；不改变调用方对象。"""
    merged = {**DEFAULT_POLICY, "roster": dict(DEFAULT_POLICY["roster"]), "owners": {}}
    for source in (parameters or {}, override or {}):
        if not isinstance(source, dict):
            continue
        for key in ("default_owner", "dispute_owner", "window_hours", "dispute_window_hours"):
            if source.get(key) not in (None, ""):
                merged[key] = source[key]
        for key in ("roster", "owners"):
            if isinstance(source.get(key), dict):
                merged[key].update(source[key])
        if isinstance(source.get("dispute_roles"), list):
            merged["dispute_roles"] = list(source["dispute_roles"])
    return merged


def fingerprint(scans):
    """只对影响路径/时效结论的采纳扫描取指纹；同内容补传重试得到同一摘要。"""
    payload = sorted(
        ({field: str(scan.get(field, "")) for field in EVIDENCE_FIELDS} for scan in scans),
        key=lambda item: (item["scan_id"], item["event_time"], item["node"]),
    )
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def evidence_for(run):
    """按包裹收集路径裁决实际采纳的扫描，以及时效结论。"""
    mapped = run["stages"][1]["rows"]
    conclusions = {row["parcel"]: row for row in run["stages"][5]["rows"]}
    evidence = {}
    for path in run["stages"][3]["rows"]:
        parcel = path["parcel"]
        lines = set(path.get("_sources", []))
        scans = [row for row in mapped if row.get("parcel") == parcel and lines & set(row.get("_sources", []))]
        evidence[parcel] = {
            "scans": [{key: row.get(key, "") for key in ("scan_id", "node", "event_time", "received_time", "destination", "promised_hours", "note")} for row in scans],
            "scan_ids": [str(row.get("scan_id", "")) for row in scans],
            "source_lines": sorted(lines),
            "fingerprint": fingerprint(scans),
            "conclusion": conclusions.get(parcel, {}),
        }
    return evidence


def _deadline(base, hours):
    return (base + timedelta(hours=float(hours))).isoformat()


def _shifted_deadline(previous, base, hours, continue_existing):
    """续办尽量保留原截止时间；已过期才顺延，避免任务延续即过期。"""
    if continue_existing and previous:
        try:
            if instant(previous) > base:
                return previous
        except Exception:
            pass
    return _deadline(base, hours)


def decide(parcel, current, active, policy, now):
    """对单票给出分派结论，不触库。

    只与当前有效任务比对：同一补传（证据摘要相同）重试直接复用，
    历史版本不参与去重，避免真实回退被误判成重试。
    """
    conclusion = current["conclusion"].get("sla")
    late_after = current["conclusion"].get("late_hours")
    expected_after = current["conclusion"].get("expected")
    scan_ids = current["scan_ids"]
    base = dict(
        parcel=parcel,
        run_id=current.get("run_id", ""),
        evidence_sha=current["fingerprint"],
        scan_ids=scan_ids,
        source_lines=current["source_lines"],
        evidence=current["scans"],
        conclusion_after=conclusion,
        late_hours_after=late_after,
        expected_after=expected_after,
        created_at=now.isoformat(),
    )

    if active is None:
        owner = policy["owners"].get(parcel) or policy["default_owner"]
        role = policy["roster"].get(owner, "客服")
        return {**base, "action": "create", "task_id": f"{parcel}-r1-{current['fingerprint'][:10]}", "revision": 1,
                "owner": owner, "role": role, "deadline": _deadline(now, policy["window_hours"]),
                "previous_deadline": None, "conclusion_before": None, "conclusion_changed": False,
                "late_hours_before": None, "expected_before": None,
                "handoff_reason": "首次分派，按包裹负责人建核验任务", "previous": None}

    before = active.get("conclusion_after")
    evidence_changed = active.get("evidence_sha") != current["fingerprint"]
    conclusion_changed = conclusion != before

    if not evidence_changed and not conclusion_changed:
        # 证据与结论都没变（含同一次补传重试）：复用当前任务，不新增分派。
        return {**base, "action": "noop", "task_id": active.get("task_id"), "revision": active.get("revision"),
                "owner": active.get("owner"), "role": active.get("role"), "deadline": active.get("deadline"),
                "previous_deadline": active.get("deadline"),
                "conclusion_before": before, "conclusion_changed": False,
                "late_hours_before": active.get("late_hours_after"), "expected_before": active.get("expected_after"),
                "handoff_reason": "同一补传已核验，复用现有核验任务，不新增分派", "previous": active}

    revision = int(active["revision"]) + 1
    if conclusion_changed:
        owner = policy["dispute_owner"]
        role = policy["roster"].get(owner)
        if not role or role not in policy["dispute_roles"]:
            return {**base, "action": "reject", "task_id": None, "revision": revision, "owner": active["owner"],
                    "role": active["role"], "deadline": active["deadline"],
                    "handoff_reason": f"补传改变时效结论（{before}→{conclusion}），但 {owner} 不具备争议处置权限；保留原任务",
                    "previous": active}
        return {**base, "action": "handoff", "task_id": f"{parcel}-r{revision}-{current['fingerprint'][:10]}",
                "revision": revision, "owner": owner, "role": role,
                "deadline": _deadline(now, policy["dispute_window_hours"]),
                "previous_deadline": active["deadline"], "conclusion_before": before, "conclusion_changed": True,
                "late_hours_before": active.get("late_hours_after"), "expected_before": active.get("expected_after"),
                "handoff_reason": f"补传改变时效结论（{before}→{conclusion}），转争议处置重新核验", "previous": active}

    return {**base, "action": "continue", "task_id": f"{parcel}-r{revision}-{current['fingerprint'][:10]}",
            "revision": revision, "owner": active["owner"], "role": active["role"],
            "deadline": _shifted_deadline(active["deadline"], now, policy["window_hours"], True),
            "previous_deadline": active["deadline"], "conclusion_before": before, "conclusion_changed": False,
            "late_hours_before": active.get("late_hours_after"), "expected_before": active.get("expected_after"),
            "handoff_reason": "补传未改变时效结论，原负责人续办", "previous": active}


def build(run, history_by_parcel, policy, now=None):
    now = now or datetime.now(timezone.utc)
    if isinstance(now, str):
        now = instant(now)
    evidence = evidence_for(run)
    decisions = []
    for parcel in sorted(evidence):
        current = {**evidence[parcel], "run_id": run.get("run_id", "")}
        active = history_by_parcel.get(parcel)
        decisions.append(decide(parcel, current, active, policy, now))
    return decisions
