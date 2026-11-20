from flask import Blueprint, jsonify, request

from app import store, verification
from app.errors import DomainError

bp = Blueprint("verification", __name__)


def _body():
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise DomainError("BAD_BODY", "请求需要对象")
    return payload


@bp.get("/api/verifications")
def listing():
    return jsonify({"tasks": verification.tasks(), "metrics": verification.summary(), "events": verification.recent_events(20)})


@bp.get("/api/verifications/<parcel>")
def one(parcel):
    return jsonify(verification.detail(parcel))


@bp.post("/api/verifications/sync")
def sync():
    payload = _body()
    run = store.get(payload["run_id"]) if payload.get("run_id") else store.latest()
    policy = payload.get("policy")
    if policy is not None and not isinstance(policy, dict):
        raise DomainError("BAD_POLICY", "分派策略需要对象")
    return jsonify(verification.sync(run, policy, payload.get("now")))


@bp.post("/api/verifications/<task_id>/review")
def review(task_id):
    payload = _body()
    return jsonify(verification.review(task_id, payload.get("version"), payload.get("decision")))
