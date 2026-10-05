"""领域事件校验。

只依赖标准库：先校验所有事件共用的信封字段，再按 event_type 校验业务载荷。
contracts/domain.schema.json 是同一套约定的 JSON Schema 表达，两者必须保持一致。
"""

from __future__ import annotations

from typing import Any

REQUIRED_ENVELOPE = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
)

EVENT_TYPES = (
    "EXHIBITION_SCHEDULED",
    "EXHIBITION_RESCHEDULED",
    "ARTWORK_ACQUIRED",
    "RIGHT_CLAIMED",
    "RIGHT_CLAIM_VERIFIED",
    "RIGHT_CLAIM_REJECTED",
    "RIGHT_DISPUTE_OPENED",
    "RIGHT_DISPUTE_RESOLVED",
    "LICENSE_GRANTED",
    "LICENSE_AMENDED",
    "LICENSE_WITHDRAWN",
    "DESIGN_SUBMITTED",
    "DESIGN_APPROVED",
    "DESIGN_REJECTED",
    "SAMPLE_SUBMITTED",
    "SAMPLE_APPROVED",
    "SAMPLE_REJECTED",
    "MATERIAL_SUBSTITUTED",
    "SKU_REGISTERED",
    "CLEARANCE_APPROVED",
    "CLEARANCE_REJECTED",
    "RESTOCK_COMMITTED",
    "BATCH_RELEASED",
    "SALE_RECORDED",
    "ROYALTY_SETTLED",
    "SKU_DELISTED",
    "SKU_RELISTED",
)

# event_type -> 合法的 aggregate_type
EVENT_AGGREGATE: dict[str, str] = {
    "EXHIBITION_SCHEDULED": "exhibition",
    "EXHIBITION_RESCHEDULED": "exhibition",
    "ARTWORK_ACQUIRED": "artwork_right",
    "RIGHT_CLAIMED": "artwork_right",
    "RIGHT_CLAIM_VERIFIED": "artwork_right",
    "RIGHT_CLAIM_REJECTED": "artwork_right",
    "RIGHT_DISPUTE_OPENED": "artwork_right",
    "RIGHT_DISPUTE_RESOLVED": "artwork_right",
    "LICENSE_GRANTED": "license_grant",
    "LICENSE_AMENDED": "license_grant",
    "LICENSE_WITHDRAWN": "license_grant",
    "DESIGN_SUBMITTED": "design_proposal",
    "DESIGN_APPROVED": "design_proposal",
    "DESIGN_REJECTED": "design_proposal",
    "SAMPLE_SUBMITTED": "sample_round",
    "SAMPLE_APPROVED": "sample_round",
    "SAMPLE_REJECTED": "sample_round",
    "MATERIAL_SUBSTITUTED": "product_sku",
    "SKU_REGISTERED": "product_sku",
    "CLEARANCE_APPROVED": "product_sku",
    "CLEARANCE_REJECTED": "product_sku",
    "RESTOCK_COMMITTED": "product_sku",
    "BATCH_RELEASED": "production_batch",
    "SALE_RECORDED": "royalty_statement",
    "ROYALTY_SETTLED": "royalty_statement",
    "SKU_DELISTED": "product_sku",
    "SKU_RELISTED": "product_sku",
}

USE_CLASSES = ("COMMERCIAL", "NONPROFIT_PROMO")
CHANNELS = ("MUSEUM_SHOP", "ONLINE_MALL", "POPUP_STORE", "WHOLESALE", "PRESS_KIT")
ADAPTATION_LEVELS = ("NONE", "STYLIZED_REFERENCE", "MODIFY", "DERIVATIVE_FULL")
COPYRIGHT_RIGHTS = ("REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK")

# 每种事件载荷中必填的标量/数组字段；嵌套结构由 _check_payload 补充校验。
_PAYLOAD_REQUIRED: dict[str, tuple[str, ...]] = {
    "EXHIBITION_SCHEDULED": ("exhibition_id", "title", "start_date", "end_date"),
    "EXHIBITION_RESCHEDULED": ("previous", "current", "reason"),
    "ARTWORK_ACQUIRED": ("artwork_id", "title", "acquisition_date", "copyright_status"),
    "RIGHT_CLAIMED": ("artwork_id", "candidate_id", "claimant", "relationship", "claimed_rights", "evidence_refs"),
    "RIGHT_CLAIM_VERIFIED": ("candidate_id", "verifier", "verified_rights", "basis"),
    "RIGHT_CLAIM_REJECTED": ("candidate_id", "verifier", "reason"),
    "RIGHT_DISPUTE_OPENED": ("artwork_id", "dispute_id", "subject", "opened_by"),
    "RIGHT_DISPUTE_RESOLVED": ("dispute_id", "resolution", "resolved_by"),
    "LICENSE_GRANTED": (
        "license_id", "artwork_ids", "licensor_candidate_id", "rights", "use_class",
        "territories", "channels", "term", "term_basis", "royalty",
    ),
    "LICENSE_AMENDED": ("change_type", "effective_at", "changes"),
    "LICENSE_WITHDRAWN": ("reason", "effective_at"),
    "DESIGN_SUBMITTED": ("design_id", "artwork_ids", "use_class", "designer", "adaptation_level", "spec_ref"),
    "DESIGN_APPROVED": ("reviewer", "approved_at"),
    "DESIGN_REJECTED": ("reviewer", "reason"),
    "SAMPLE_SUBMITTED": ("sample_id", "design_id", "round_no", "supplier", "material_spec_ref", "submitted_at"),
    "SAMPLE_APPROVED": ("reviewer", "approved_at"),
    "SAMPLE_REJECTED": ("reviewer", "reason"),
    "MATERIAL_SUBSTITUTED": ("sku", "previous_material_ref", "new_material_ref", "supplier", "changed_at"),
    "SKU_REGISTERED": ("sku", "design_id", "artwork_ids", "use_class", "channels", "unit_price", "registered_at"),
    "CLEARANCE_APPROVED": ("sku", "approver", "approved_at", "channels", "rights_snapshot"),
    "CLEARANCE_REJECTED": ("sku", "reviewer", "rejected_at", "reasons"),
    "RESTOCK_COMMITTED": ("sku", "quantity", "batch_id", "committed_at"),
    "BATCH_RELEASED": (
        "batch_id", "sku", "quantity", "material_ref", "released_at", "rights_snapshot", "royalty_terms",
    ),
    "SALE_RECORDED": (
        "statement_id", "sku", "batch_id", "channel", "quantity", "unit_price", "sold_at", "royalty_due",
    ),
    "ROYALTY_SETTLED": ("statement_id", "period_start", "period_end", "settled_at", "lines"),
    "SKU_DELISTED": (
        "sku", "channels", "reason_code", "trigger_event_id", "inventory_disposition", "approver", "delisted_at",
    ),
    "SKU_RELISTED": ("sku", "channels", "reason", "approver", "relisted_at"),
}


def _err(errors: list[str], msg: str) -> None:
    errors.append(msg)


def _check_window(errors: list[str], obj: Any, path: str) -> None:
    if not isinstance(obj, dict):
        _err(errors, f"{path} 必须是日期窗口对象")
        return
    for key in ("start_date", "end_date"):
        if not isinstance(obj.get(key), str):
            _err(errors, f"{path}.{key} 必须是日期字符串")
    if isinstance(obj.get("start_date"), str) and isinstance(obj.get("end_date"), str):
        if obj["start_date"] > obj["end_date"]:
            _err(errors, f"{path} 的 start_date 晚于 end_date")


def _check_payload(event_type: str, payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload 必须是对象"]
    for key in _PAYLOAD_REQUIRED[event_type]:
        if key not in payload:
            _err(errors, f"payload 缺少字段：{key}")
    if errors:
        return errors

    p = payload
    if event_type in ("EXHIBITION_SCHEDULED",):
        if p["start_date"] > p["end_date"]:
            _err(errors, "展期 start_date 晚于 end_date")
    elif event_type == "EXHIBITION_RESCHEDULED":
        _check_window(errors, p["previous"], "payload.previous")
        _check_window(errors, p["current"], "payload.current")
    elif event_type == "ARTWORK_ACQUIRED":
        if p["copyright_status"] not in (
            "RETAINED_BY_ARTIST", "TRANSFERRED_WITH_DEED", "ESTATE_HELD", "JOINTLY_OWNED", "UNVERIFIED",
        ):
            _err(errors, "copyright_status 取值非法")
    elif event_type == "RIGHT_CLAIMED":
        if p["relationship"] not in ("ARTIST", "JOINT_ARTIST", "HEIR", "ESTATE", "ASSIGNEE", "PUBLISHER"):
            _err(errors, "relationship 取值非法")
        if not isinstance(p["claimed_rights"], list) or not p["claimed_rights"]:
            _err(errors, "claimed_rights 必须是非空数组")
        elif any(r not in COPYRIGHT_RIGHTS for r in p["claimed_rights"]):
            _err(errors, "claimed_rights 含非法权能")
        if not isinstance(p["evidence_refs"], list) or not p["evidence_refs"]:
            _err(errors, "evidence_refs 必须是非空数组（权属候选必须有凭证）")
        if "share_pct" in p and not (0 <= p["share_pct"] <= 100):
            _err(errors, "share_pct 必须在 0~100 之间")
    elif event_type == "RIGHT_CLAIM_VERIFIED":
        if any(r not in COPYRIGHT_RIGHTS for r in p["verified_rights"]):
            _err(errors, "verified_rights 含非法权能")
    elif event_type == "RIGHT_DISPUTE_RESOLVED":
        if p["resolution"] not in ("CONFIRMED_CLAIMANT", "SHARED", "UNENCUMBERED", "COURT_PENDING"):
            _err(errors, "resolution 取值非法")
    elif event_type == "LICENSE_GRANTED":
        rights = p["rights"]
        if not isinstance(rights, dict) or not isinstance(rights.get("reproduction"), bool):
            _err(errors, "rights.reproduction 必须是布尔值")
        if rights.get("adaptation") not in ADAPTATION_LEVELS:
            _err(errors, "rights.adaptation 取值非法")
        if p["use_class"] not in USE_CLASSES:
            _err(errors, "use_class 取值非法")
        if not isinstance(p["artwork_ids"], list) or not p["artwork_ids"]:
            _err(errors, "artwork_ids 必须是非空数组")
        if not isinstance(p["territories"], list) or not p["territories"]:
            _err(errors, "territories 必须是非空数组")
        if any(c not in CHANNELS for c in p["channels"]):
            _err(errors, "channels 含非法渠道")
        _check_window(errors, p["term"], "payload.term")
        if p["term_basis"] not in ("EXHIBITION_WINDOW", "EXPLICIT"):
            _err(errors, "term_basis 取值非法")
        if p["term_basis"] == "EXHIBITION_WINDOW" and not p.get("exhibition_id"):
            _err(errors, "term_basis=EXHIBITION_WINDOW 时必须提供 exhibition_id")
        royalty = p["royalty"]
        if not isinstance(royalty, dict) or royalty.get("basis") not in (
            "PERCENT_OF_REVENUE", "PER_UNIT", "LUMP_SUM",
        ) or not isinstance(royalty.get("rate"), (int, float)):
            _err(errors, "royalty 必须含 basis 与数值 rate")
        if "max_units" in p and p["max_units"] is not None and (
            not isinstance(p["max_units"], int) or p["max_units"] < 1
        ):
            _err(errors, "max_units 必须是正整数或 null")
    elif event_type == "LICENSE_AMENDED":
        if p["change_type"] not in ("NARROW", "WIDEN", "CORRECT"):
            _err(errors, "change_type 取值非法")
    elif event_type == "DESIGN_SUBMITTED":
        if p["use_class"] not in USE_CLASSES:
            _err(errors, "use_class 取值非法")
        if p["adaptation_level"] not in ADAPTATION_LEVELS:
            _err(errors, "adaptation_level 取值非法")
        if not p["artwork_ids"]:
            _err(errors, "artwork_ids 必须是非空数组")
    elif event_type == "SAMPLE_SUBMITTED":
        if not isinstance(p["round_no"], int) or p["round_no"] < 1:
            _err(errors, "round_no 必须是正整数")
    elif event_type == "SKU_REGISTERED":
        if p["use_class"] not in USE_CLASSES:
            _err(errors, "use_class 取值非法")
        if any(c not in CHANNELS for c in p["channels"]):
            _err(errors, "channels 含非法渠道")
        if not p["artwork_ids"]:
            _err(errors, "artwork_ids 必须是非空数组")
        if not isinstance(p["unit_price"], (int, float)) or p["unit_price"] < 0:
            _err(errors, "unit_price 必须是非负数")
    elif event_type in ("CLEARANCE_APPROVED", "BATCH_RELEASED"):
        snap = p["rights_snapshot"]
        if not isinstance(snap, list) or not snap:
            _err(errors, "rights_snapshot 必须是非空数组")
        else:
            for i, item in enumerate(snap):
                for key in ("artwork_id", "license_id", "license_version", "licensor_candidate_id"):
                    if key not in item:
                        _err(errors, f"rights_snapshot[{i}] 缺少 {key}")
        if event_type == "BATCH_RELEASED":
            if not isinstance(p["quantity"], int) or p["quantity"] < 1:
                _err(errors, "quantity 必须是正整数")
            if not isinstance(p.get("royalty_terms"), list):
                _err(errors, "royalty_terms 必须是数组")
    elif event_type == "CLEARANCE_REJECTED":
        if not isinstance(p["reasons"], list) or not p["reasons"]:
            _err(errors, "reasons 必须是非空数组")
    elif event_type == "RESTOCK_COMMITTED":
        if not isinstance(p["quantity"], int) or p["quantity"] < 1:
            _err(errors, "quantity 必须是正整数")
    elif event_type == "SALE_RECORDED":
        if p["channel"] not in CHANNELS:
            _err(errors, "channel 取值非法")
        if not isinstance(p["quantity"], int) or p["quantity"] < 1:
            _err(errors, "quantity 必须是正整数")
    elif event_type == "SKU_DELISTED":
        if any(c not in CHANNELS for c in p["channels"]):
            _err(errors, "channels 含非法渠道")
        if p["reason_code"] not in (
            "RIGHT_WITHDRAWN", "LICENSE_NARROWED", "DISPUTE_OPENED",
            "MATERIAL_CHANGED", "EXHIBITION_ENDED", "MANUAL",
        ):
            _err(errors, "reason_code 取值非法")
        if p["inventory_disposition"] not in ("SELL_THROUGH", "HOLD", "RETURN_SUPPLIER", "DESTROY"):
            _err(errors, "inventory_disposition 取值非法")
    elif event_type == "SKU_RELISTED":
        if any(c not in CHANNELS for c in p["channels"]):
            _err(errors, "channels 含非法渠道")

    return errors


def validate_event(record: dict) -> list[str]:
    """校验一条事件记录，返回错误信息列表；空列表表示通过。

    保留对早期无 payload 样例的兼容：仅当 event_type 已知且携带 payload 时才深校验。
    """
    errors = [f"缺少字段：{name}" for name in REQUIRED_ENVELOPE if name not in record]
    if errors:
        return errors

    if not isinstance(record["event_id"], str) or not record["event_id"]:
        errors.append("event_id 必须是非空字符串")
    if record["event_type"] not in EVENT_TYPES:
        errors.append(f"event_type 未知：{record['event_type']}")
    if not isinstance(record["aggregate_id"], str) or not record["aggregate_id"]:
        errors.append("aggregate_id 必须是非空字符串")
    if not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] < 1:
        errors.append("version 必须是正整数")
    if not isinstance(record["occurred_at"], str) or not record["occurred_at"]:
        errors.append("occurred_at 必须是 date-time 字符串")
    if not isinstance(record["summary"], str) or not record["summary"]:
        errors.append("summary 必须是非空字符串")

    expected_aggregate = EVENT_AGGREGATE.get(record["event_type"])
    if expected_aggregate and record.get("aggregate_type") != expected_aggregate:
        errors.append(
            f"event_type={record['event_type']} 的 aggregate_type 必须是 {expected_aggregate}"
        )

    if "payload" in record and record["event_type"] in EVENT_TYPES:
        errors.extend(_check_payload(record["event_type"], record["payload"]))

    return errors
