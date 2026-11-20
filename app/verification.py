"""妥投核验任务的分派与持久化。

每条包裹最多只有一条 active 任务（由部分唯一索引兜底），补传到达时：
- 结论不变：原负责人续办，保留原截止时间（过期才顺延）；
- 结论改变：改派争议处置人，按争议时限重设截止时间；
- 同一补传重试：识别为同一证据摘要，直接复用，不新增任务；
- 替换失败：事务回滚，旧任务及其证据、交接原因原样保留。

历史版本只做软取代，不删除；事件表只追加，不更新。
"""
import json
import sqlite3
from datetime import datetime, timezone

from app import store
from app.errors import DomainError
from app.values import instant
from app.modules.m8_verifications import rules

TASK_COLUMNS = [
    "task_id", "parcel", "revision", "active", "status", "owner", "role", "deadline",
    "evidence_sha", "scan_ids", "source_lines", "evidence", "conclusion_before",
    "conclusion_after", "late_hours_before", "late_hours_after", "expected_before",
    "expected_after", "conclusion_changed", "handoff_reason", "run_id", "created_at",
    "superseded_at", "superseded_by", "superseded_reason", "version",
]
JSON_FIELDS = {"scan_ids", "source_lines", "evidence"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_tasks (
    task_id TEXT PRIMARY KEY,
    parcel TEXT NOT NULL,
    revision INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '待核验',
    owner TEXT NOT NULL,
    role TEXT NOT NULL,
    deadline TEXT NOT NULL,
    evidence_sha TEXT NOT NULL,
    scan_ids TEXT NOT NULL,
    source_lines TEXT NOT NULL,
    evidence TEXT NOT NULL,
    conclusion_before TEXT,
    conclusion_after TEXT,
    late_hours_before TEXT,
    late_hours_after TEXT,
    expected_before TEXT,
    expected_after TEXT,
    conclusion_changed INTEGER NOT NULL DEFAULT 0,
    handoff_reason TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    superseded_by TEXT,
    superseded_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS verification_one_active_per_parcel
    ON verification_tasks(parcel) WHERE active=1;
CREATE TABLE IF NOT EXISTS verification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    parcel TEXT NOT NULL,
    action TEXT NOT NULL,
    revision INTEGER NOT NULL,
    from_owner TEXT,
    to_owner TEXT,
    deadline TEXT,
    evidence_sha TEXT NOT NULL,
    conclusion_before TEXT,
    conclusion_after TEXT,
    reason TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _connect():
    db = store.connect()
    db.isolation_level = None
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _now(value=None):
    if value in (None, ""):
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return instant(value)


def _task(row):
    item = {key: row[key] for key in TASK_COLUMNS}
    for key in JSON_FIELDS:
        item[key] = json.loads(item[key]) if item[key] else []
    item["active"] = bool(item["active"])
    item["conclusion_changed"] = bool(item["conclusion_changed"])
    return item


def _active_map(db):
    return {row["parcel"]: dict(row) for row in db.execute("SELECT * FROM verification_tasks WHERE active=1")}


def _event(db, decision, now):
    if decision["action"] == "reject":
        # 同一次被拒绝的补传重试不再刷事件；裁决仍可重复执行。
        exists = db.execute(
            "SELECT 1 FROM verification_events WHERE parcel=? AND action='reject' AND evidence_sha=?",
            (decision["parcel"], decision["evidence_sha"]),
        ).fetchone()
        if exists:
            return
    db.execute(
        "INSERT INTO verification_events(task_id,parcel,action,revision,from_owner,to_owner,deadline,evidence_sha,"
        "conclusion_before,conclusion_after,reason,run_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision.get("task_id"),
            decision["parcel"],
            decision["action"],
            decision["revision"],
            (decision.get("previous") or {}).get("owner"),
            decision.get("owner"),
            decision.get("deadline"),
            decision["evidence_sha"],
            decision.get("conclusion_before"),
            decision.get("conclusion_after"),
            decision.get("handoff_reason", ""),
            decision.get("run_id", ""),
            now.isoformat(),
        ),
    )


def _insert(db, decision):
    values = {
        "task_id": decision["task_id"],
        "parcel": decision["parcel"],
        "revision": decision["revision"],
        "status": "待核验",
        "owner": decision["owner"],
        "role": decision["role"],
        "deadline": decision["deadline"],
        "evidence_sha": decision["evidence_sha"],
        "scan_ids": json.dumps(decision.get("scan_ids", []), ensure_ascii=False),
        "source_lines": json.dumps(decision.get("source_lines", []), ensure_ascii=False),
        "evidence": json.dumps(decision.get("evidence", []), ensure_ascii=False),
        "conclusion_before": decision.get("conclusion_before"),
        "conclusion_after": decision.get("conclusion_after"),
        "late_hours_before": decision.get("late_hours_before"),
        "late_hours_after": decision.get("late_hours_after"),
        "expected_before": decision.get("expected_before"),
        "expected_after": decision.get("expected_after"),
        "conclusion_changed": int(bool(decision.get("conclusion_changed"))),
        "handoff_reason": decision.get("handoff_reason", ""),
        "run_id": decision.get("run_id", ""),
        "created_at": decision.get("created_at", ""),
    }
    columns = ",".join(values)
    marks = ",".join("?" for _ in values)
    db.execute(f"INSERT INTO verification_tasks({columns}) VALUES ({marks})", tuple(values.values()))


def _replace_active(db, decision, now):
    cursor = db.execute(
        "UPDATE verification_tasks SET active=0,status='已取代',superseded_at=?,superseded_by=?,superseded_reason=? "
        "WHERE parcel=? AND active=1",
        (now.isoformat(), decision["task_id"], decision.get("handoff_reason", ""), decision["parcel"]),
    )
    if cursor.rowcount != 1:
        raise DomainError("TASK_CONFLICT", "该票核验任务已被其他操作更新，请刷新后重试", 409)


def _persist(db, decisions, now):
    for decision in decisions:
        action = decision["action"]
        if action in {"create"}:
            _insert(db, decision)
            _event(db, decision, now)
        elif action in {"continue", "handoff"}:
            _replace_active(db, decision, now)
            _insert(db, decision)
            _event(db, decision, now)
        elif action == "reject":
            _event(db, decision, now)
        # noop：不新增任务，也不写事件，避免重试把审计表刷满


def sync(run=None, policy=None, now=None):
    """按当前批次重算每票核验任务；重复调用对相同补传是幂等的。"""
    run = run or store.latest()
    moment = _now(now)
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        if run is None:
            db.commit()
            return {"decisions": [], "tasks": [], "metrics": summary(db), "now": moment.isoformat()}
        merged = rules.policy_of((run.get("parameters") or {}).get("verification"), policy)
        decisions = rules.build(run, _active_map(db), merged, moment)
        _persist(db, decisions, moment)
        db.commit()
    except DomainError:
        db.rollback()
        raise
    except (sqlite3.Error, ValueError) as error:
        db.rollback()
        raise DomainError("TASK_WRITE_FAILED", f"核验任务写入失败，已保留旧版任务：{error}", 500)
    finally:
        db.close()
    return {
        "run_id": run.get("run_id", ""),
        "now": moment.isoformat(),
        "policy": merged,
        "decisions": [public_decision(item) for item in decisions],
        "tasks": tasks(),
        "metrics": summary(),
    }


def public_decision(decision):
    keys = ("parcel", "action", "task_id", "revision", "owner", "role", "deadline", "previous_deadline",
            "evidence_sha", "scan_ids", "conclusion_before", "conclusion_after", "late_hours_before",
            "late_hours_after", "expected_before", "expected_after", "conclusion_changed", "handoff_reason", "run_id")
    return {key: decision.get(key) for key in keys}


def tasks(parcel=None, include_inactive=False):
    db = _connect()
    try:
        query = "SELECT * FROM verification_tasks"
        clauses, values = [], []
        if parcel is not None:
            clauses.append("parcel=?")
            values.append(parcel)
        if not include_inactive:
            clauses.append("active=1")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY parcel,revision"
        return [_task(row) for row in db.execute(query, values)]
    finally:
        db.close()


def detail(parcel):
    db = _connect()
    try:
        rows = [_task(row) for row in db.execute("SELECT * FROM verification_tasks WHERE parcel=? ORDER BY revision", (parcel,))]
        events = [dict(row) for row in db.execute("SELECT * FROM verification_events WHERE parcel=? ORDER BY id", (parcel,))]
        if not rows:
            raise DomainError("NOT_FOUND", "该票没有核验任务", 404)
        active = next((row for row in rows if row["active"]), None)
        return {"parcel": parcel, "active": active, "revisions": rows, "events": events}
    finally:
        db.close()


def recent_events(limit=50):
    db = _connect()
    try:
        return [dict(row) for row in db.execute("SELECT * FROM verification_events ORDER BY id DESC LIMIT ?", (int(limit),))]
    finally:
        db.close()


def summary(db=None):
    owned = db is None
    db = db or _connect()
    try:
        active = db.execute("SELECT COUNT(*) parcels, SUM(status='待核验') pending, SUM(conclusion_changed) changed FROM verification_tasks WHERE active=1").fetchone()
        handoffs = db.execute("SELECT COUNT(*) FROM verification_events WHERE action='handoff'").fetchone()[0]
        continues = db.execute("SELECT COUNT(*) FROM verification_events WHERE action='continue'").fetchone()[0]
        rejects = db.execute("SELECT COUNT(*) FROM verification_events WHERE action='reject'").fetchone()[0]
        return {
            "parcels": active["parcels"] or 0,
            "pending": active["pending"] or 0,
            "changed": active["changed"] or 0,
            "handoffs": handoffs,
            "continues": continues,
            "rejects": rejects,
        }
    finally:
        if owned:
            db.close()


def review(task_id, version, decision):
    if decision not in {"已核验", "退回复核"}:
        raise DomainError("BAD_DECISION", "核验状态无效")
    if not isinstance(version, int):
        raise DomainError("BAD_VERSION", "请提供版本号")
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            "UPDATE verification_tasks SET status=?,version=version+1 WHERE task_id=? AND version=? AND active=1",
            (decision, task_id, version),
        )
        if cursor.rowcount != 1:
            row = db.execute("SELECT active FROM verification_tasks WHERE task_id=?", (task_id,)).fetchone()
            db.rollback()
            if row is None:
                raise DomainError("NOT_FOUND", "核验任务不存在", 404)
            if not row["active"]:
                raise DomainError("TASK_NOT_ACTIVE", "该版本已被取代，请处理当前任务", 409)
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
        task = dict(db.execute("SELECT * FROM verification_tasks WHERE task_id=?", (task_id,)).fetchone())
        db.execute(
            "INSERT INTO verification_events(task_id,parcel,action,revision,from_owner,to_owner,deadline,evidence_sha,"
            "conclusion_before,conclusion_after,reason,run_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, task["parcel"], "review", task["revision"], task["owner"], task["owner"], task["deadline"],
             task["evidence_sha"], task["conclusion_before"], task["conclusion_after"], decision, task["run_id"],
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
        return _task(db.execute("SELECT * FROM verification_tasks WHERE task_id=?", (task_id,)).fetchone())
    except DomainError:
        db.rollback()
        raise
    except sqlite3.Error as error:
        db.rollback()
        raise DomainError("TASK_WRITE_FAILED", f"核验任务写入失败：{error}", 500)
    finally:
        db.close()
