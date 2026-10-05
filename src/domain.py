"""权利投影与开售门禁。

从 append-only 事件流重放得到当前状态，并在任意时点回答：
- 某 SKU 此刻能否生产、在哪些渠道/地域销售、交付（配额）余量还有多少；
- 多件作品组合产品的许可交集；
- 撤权/收窄/改期/换料/争议对 SKU、渠道的精确影响；
- 下架反查链（SKU → 放行审批 → 授权版本 → 权属候选与核验依据 → 批次与分成）。

已放行批次在 BATCH_RELEASED 中冻结权利快照与分成条件，授权事后变化不影响
其作为已售商品的合法性与结算依据。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

ADAPT_RANK = {"NONE": 0, "STYLIZED_REFERENCE": 1, "MODIFY": 2, "DERIVATIVE_FULL": 3}


def _date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class Candidate:
    candidate_id: str
    artwork_id: str
    claimant: str
    relationship: str
    claimed_rights: set[str]
    share_pct: Optional[float]
    evidence_refs: list[str]
    status: str = "PENDING"  # PENDING / VERIFIED / REJECTED
    verified_rights: set[str] = field(default_factory=set)
    verifier: str = ""
    basis: str = ""


@dataclass
class Amendment:
    change_type: str
    effective_at: datetime
    changes: dict


@dataclass
class License:
    license_id: str
    artwork_ids: list[str]
    licensor_candidate_id: str
    reproduction: bool
    adaptation: str
    use_class: str
    territories: list[str]
    channels: set[str]
    term: tuple[date, date]
    term_basis: str
    exhibition_id: Optional[str]
    max_units: Optional[int]
    sell_through_days: int
    royalty: dict
    granted_version: int = 1
    amendments: list[Amendment] = field(default_factory=list)
    withdrawn: Optional[dict] = None

    def state_at(self, at: datetime, exhibition_window: Optional[tuple[date, date]]):
        """返回某一时点的授权有效状态。"""
        d = at.date()
        channels = set(self.channels)
        territories = set(self.territories)
        reproduction = self.reproduction
        adaptation = self.adaptation
        max_units = self.max_units
        start, end = self.term

        for am in self.amendments:
            if am.effective_at <= at:
                ch = am.changes
                channels -= set(ch.get("remove_channels", []))
                channels |= set(ch.get("add_channels", []))
                territories -= set(ch.get("remove_territories", []))
                territories |= set(ch.get("add_territories", []))
                if ch.get("rights"):
                    reproduction = ch["rights"].get("reproduction", reproduction)
                    adaptation = ch["rights"].get("adaptation", adaptation)
                if ch.get("term"):
                    start, end = _date(ch["term"]["start_date"]), _date(ch["term"]["end_date"])
                if "max_units" in ch:
                    max_units = ch["max_units"]

        if self.term_basis == "EXHIBITION_WINDOW" and exhibition_window:
            ex_start, ex_end = exhibition_window
            start, end = max(start, ex_start), min(end, ex_end)

        sell_through_until: Optional[date] = end + timedelta(days=self.sell_through_days)
        withdrawn_active = bool(self.withdrawn and _dt(self.withdrawn["effective_at"]) <= at)
        if withdrawn_active:
            raw = self.withdrawn.get("sell_through_until")
            sell_through_until = _date(raw) if raw else _dt(self.withdrawn["effective_at"]).date()

        in_production_window = start <= d <= end and not withdrawn_active
        in_sell_window = start <= d <= sell_through_until
        return {
            "channels": channels,
            "territories": territories,
            "reproduction": reproduction,
            "adaptation": adaptation,
            "max_units": max_units,
            "window": (start, end),
            "in_production_window": in_production_window,
            "in_sell_window": in_sell_window,
            "withdrawn": withdrawn_active,
            "sell_through_until": sell_through_until,
        }


@dataclass
class Sku:
    sku: str
    design_id: str
    artwork_ids: list[str]
    use_class: str
    channels: list[str]
    unit_price: float
    clearance: Optional[dict] = None
    clearance_rejected: Optional[dict] = None
    material_changed_at: Optional[datetime] = None
    # channel -> 全部渠道动作（DELISTED/RELISTED），按时点排序，查询时取不晚于该时点的最后一个
    channel_actions: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    # 全部下架历史（重新上架不擦除），供下架反查审计
    delisting_history: list[dict] = field(default_factory=list)


class RightsProjection:
    def __init__(self, store) -> None:
        self.store = store
        self.exhibitions: dict[str, dict] = {}
        self.artworks: dict[str, dict] = {}
        self.candidates: dict[str, Candidate] = {}
        self.disputes: dict[str, dict] = {}
        self.licenses: dict[str, License] = {}
        self.designs: dict[str, dict] = {}
        self.samples: dict[str, dict] = {}
        self.skus: dict[str, Sku] = {}
        self.batches: dict[str, dict] = {}
        self.restock_commits: list[dict] = []
        self.sales: list[dict] = []
        self.settlements: list[dict] = []
        self._replay()

    # ---------- 重放 ----------

    def _replay(self) -> None:
        for e in self.store.all_events():
            p = e.get("payload", {})
            t = e["event_type"]
            at = _dt(e["occurred_at"])

            if t == "EXHIBITION_SCHEDULED":
                self.exhibitions[p["exhibition_id"]] = {
                    "title": p["title"],
                    "window": (_date(p["start_date"]), _date(p["end_date"])),
                    "schedule_event_id": e["event_id"],
                }
            elif t == "EXHIBITION_RESCHEDULED":
                # aggregate_id 即特展 id；只更新对应特展的窗口
                ex = self.exhibitions.get(e["aggregate_id"])
                if ex is not None:
                    ex["window"] = (_date(p["current"]["start_date"]), _date(p["current"]["end_date"]))
                    ex["last_reschedule_event_id"] = e["event_id"]
            elif t == "ARTWORK_ACQUIRED":
                self.artworks[p["artwork_id"]] = {
                    "title": p["title"],
                    "copyright_status": p["copyright_status"],
                    "copyright_deed_ref": p.get("copyright_deed_ref"),
                    "acquisition_event_id": e["event_id"],
                }
            elif t == "RIGHT_CLAIMED":
                self.candidates[p["candidate_id"]] = Candidate(
                    candidate_id=p["candidate_id"],
                    artwork_id=p["artwork_id"],
                    claimant=p["claimant"],
                    relationship=p["relationship"],
                    claimed_rights=set(p["claimed_rights"]),
                    share_pct=p.get("share_pct"),
                    evidence_refs=list(p["evidence_refs"]),
                )
            elif t == "RIGHT_CLAIM_VERIFIED":
                c = self.candidates[p["candidate_id"]]
                c.status = "VERIFIED"
                c.verified_rights = set(p["verified_rights"])
                c.verifier = p["verifier"]
                c.basis = p["basis"]
            elif t == "RIGHT_CLAIM_REJECTED":
                c = self.candidates[p["candidate_id"]]
                c.status = "REJECTED"
                c.verifier = p["verifier"]
            elif t == "RIGHT_DISPUTE_OPENED":
                self.disputes[p["dispute_id"]] = {
                    "artwork_id": p["artwork_id"],
                    "status": "OPEN",
                    "opened_event_id": e["event_id"],
                    "opened_at": at,
                    "resolved_at": None,
                    "subject": p["subject"],
                }
            elif t == "RIGHT_DISPUTE_RESOLVED":
                d = self.disputes[p["dispute_id"]]
                d["status"] = "RESOLVED"
                d["resolution"] = p["resolution"]
                d["resolved_at"] = at
            elif t == "LICENSE_GRANTED":
                r = p["rights"]
                self.licenses[p["license_id"]] = License(
                    license_id=p["license_id"],
                    artwork_ids=list(p["artwork_ids"]),
                    licensor_candidate_id=p["licensor_candidate_id"],
                    reproduction=r["reproduction"],
                    adaptation=r["adaptation"],
                    use_class=p["use_class"],
                    territories=list(p["territories"]),
                    channels=set(p["channels"]),
                    term=(_date(p["term"]["start_date"]), _date(p["term"]["end_date"])),
                    term_basis=p["term_basis"],
                    exhibition_id=p.get("exhibition_id"),
                    max_units=p.get("max_units"),
                    sell_through_days=p.get("sell_through_days", 0),
                    royalty=dict(p["royalty"]),
                    granted_version=e["version"],
                )
            elif t == "LICENSE_AMENDED":
                self.licenses[e["aggregate_id"]].amendments.append(
                    Amendment(p["change_type"], _dt(p["effective_at"]), p["changes"])
                )
            elif t == "LICENSE_WITHDRAWN":
                self.licenses[e["aggregate_id"]].withdrawn = {
                    "reason": p["reason"],
                    "effective_at": p["effective_at"],
                    "sell_through_until": p.get("sell_through_until"),
                    "event_id": e["event_id"],
                }
            elif t == "DESIGN_SUBMITTED":
                self.designs[p["design_id"]] = {
                    "artwork_ids": list(p["artwork_ids"]),
                    "use_class": p["use_class"],
                    "adaptation_level": p["adaptation_level"],
                    "designer": p["designer"],
                    "spec_ref": p["spec_ref"],
                    "status": "SUBMITTED",
                }
            elif t == "DESIGN_APPROVED":
                self.designs[e["aggregate_id"]].update(
                    status="APPROVED", reviewer=p["reviewer"], decided_at=at)
            elif t == "DESIGN_REJECTED":
                self.designs[e["aggregate_id"]].update(
                    status="REJECTED", reviewer=p["reviewer"], decided_at=at)
            elif t == "SAMPLE_SUBMITTED":
                self.samples[p["sample_id"]] = {
                    "design_id": p["design_id"],
                    "round_no": p["round_no"],
                    "supplier": p["supplier"],
                    "material_spec_ref": p["material_spec_ref"],
                    "submitted_at": at,
                    "status": "SUBMITTED",
                }
            elif t == "SAMPLE_APPROVED":
                self.samples[e["aggregate_id"]].update(
                    status="APPROVED", reviewer=p["reviewer"], decided_at=at)
            elif t == "SAMPLE_REJECTED":
                self.samples[e["aggregate_id"]].update(
                    status="REJECTED", reviewer=p["reviewer"], decided_at=at)
            elif t == "MATERIAL_SUBSTITUTED":
                sku = self.skus.get(p["sku"])
                if sku:
                    sku.material_changed_at = at
            elif t == "SKU_REGISTERED":
                self.skus[p["sku"]] = Sku(
                    sku=p["sku"],
                    design_id=p["design_id"],
                    artwork_ids=list(p["artwork_ids"]),
                    use_class=p["use_class"],
                    channels=list(p["channels"]),
                    unit_price=p["unit_price"],
                )
            elif t == "CLEARANCE_APPROVED":
                sku = self.skus[p["sku"]]
                sku.clearance = {
                    "approver": p["approver"],
                    "approved_at": p["approved_at"],
                    "at": at,
                    "channels": list(p["channels"]),
                    "rights_snapshot": list(p["rights_snapshot"]),
                    "event_id": e["event_id"],
                    "version": e["version"],
                }
                sku.clearance_rejected = None
            elif t == "CLEARANCE_REJECTED":
                self.skus[p["sku"]].clearance_rejected = {
                    "reviewer": p["reviewer"],
                    "reasons": list(p["reasons"]),
                    "at": at,
                    "event_id": e["event_id"],
                }
            elif t == "RESTOCK_COMMITTED":
                self.restock_commits.append(
                    {"sku": p["sku"], "quantity": p["quantity"], "batch_id": p["batch_id"], "at": at}
                )
            elif t == "BATCH_RELEASED":
                self.batches[p["batch_id"]] = {
                    "sku": p["sku"],
                    "quantity": p["quantity"],
                    "material_ref": p["material_ref"],
                    "released_at": at,
                    "rights_snapshot": list(p["rights_snapshot"]),
                    "royalty_terms": list(p["royalty_terms"]),
                    "event_id": e["event_id"],
                }
            elif t == "SALE_RECORDED":
                self.sales.append({**p, "sold_at_dt": at})
            elif t == "ROYALTY_SETTLED":
                self.settlements.append(p)
            elif t == "SKU_DELISTED":
                record = {
                    "action": "DELISTED",
                    "reason_code": p["reason_code"],
                    "trigger_event_id": p["trigger_event_id"],
                    "inventory_disposition": p["inventory_disposition"],
                    "sell_through_until": p.get("sell_through_until"),
                    "approver": p["approver"],
                    "event_id": e["event_id"],
                    "at": at,
                }
                for ch in p["channels"]:
                    self.skus[p["sku"]].channel_actions[ch].append(record)
                self.skus[p["sku"]].delisting_history.append({
                    "channels": list(p["channels"]), **record,
                })
            elif t == "SKU_RELISTED":
                record = {"action": "RELISTED", "approver": p["approver"],
                          "reason": p["reason"], "event_id": e["event_id"], "at": at}
                for ch in p["channels"]:
                    self.skus[p["sku"]].channel_actions[ch].append(record)
                    if self.skus[p["sku"]].clearance and ch not in self.skus[p["sku"]].clearance["channels"]:
                        self.skus[p["sku"]].clearance["channels"].append(ch)

    # ---------- 权利评估 ----------

    def _open_dispute(self, artwork_id: str, at: datetime) -> bool:
        return any(
            d["artwork_id"] == artwork_id
            and d["opened_at"] <= at
            and not (d.get("resolved_at") and d["resolved_at"] <= at)
            for d in self.disputes.values()
        )

    def effective_licenses(self, artwork_id: str, at: datetime) -> list[tuple[License, dict]]:
        """一件作品在某时点所有可用的授权（授权人候选已核验、作品无未决争议）。"""
        result = []
        for lic in self.licenses.values():
            if artwork_id not in lic.artwork_ids:
                continue
            candidate = self.candidates.get(lic.licensor_candidate_id)
            if not candidate or candidate.status != "VERIFIED" or candidate.artwork_id != artwork_id:
                continue
            if self._open_dispute(artwork_id, at):
                continue
            window = None
            if lic.exhibition_id and lic.exhibition_id in self.exhibitions:
                window = self.exhibitions[lic.exhibition_id]["window"]
            result.append((lic, lic.state_at(at, window)))
        return result

    def _coverage(self, sku: Sku, at: datetime, channel: str, region: Optional[str],
                  for_production: bool, ignore_window: bool = False) -> dict[str, dict]:
        """每件作品的许可交集明细：命中的授权 + 未满足原因（如共同权利人未授权）。

        ignore_window=True 用于开幕前的开售审批：只校验结构交集（用途/权能/地域/
        渠道/共同权利人/撤回/争议），实际期限在排产时点再校验。
        """
        design = self.designs[sku.design_id]
        need_adapt = ADAPT_RANK[design["adaptation_level"]]
        coverage: dict[str, dict] = {}
        for artwork_id in sku.artwork_ids:
            hits = []
            for lic, st in self.effective_licenses(artwork_id, at):
                if lic.use_class != sku.use_class:
                    continue
                if not st["reproduction"]:
                    continue
                if ADAPT_RANK[st["adaptation"]] < need_adapt:
                    continue
                if channel not in st["channels"]:
                    continue
                if region is not None and region not in st["territories"]:
                    continue
                if not ignore_window:
                    if for_production and not st["in_production_window"]:
                        continue
                    if not for_production and not st["in_sell_window"]:
                        continue
                if st["withdrawn"]:
                    continue
                hits.append((lic, st))

            missing: list[str] = []
            if not hits:
                missing.append("无满足用途/权能/地域/渠道/期限交集的有效授权")
            else:
                # 合作作品、多人继承等：每位已核验权利人都必须是某一命中授权的授权人
                licensors = {lic.licensor_candidate_id for lic, _ in hits}
                for c in self.candidates.values():
                    if c.artwork_id == artwork_id and c.status == "VERIFIED" and c.candidate_id not in licensors:
                        missing.append(f"共同权利人候选 {c.candidate_id}（{c.claimant}）未授权")
            coverage[artwork_id] = {"hits": hits, "missing": missing}
        return coverage

    def covering_licenses(
        self, sku: Sku, at: datetime, channel: str, region: Optional[str] = None,
        for_production: bool = True, ignore_window: bool = False,
    ) -> dict[str, list[tuple[License, dict]]]:
        """返回 artwork_id -> 覆盖该 SKU 权能/用途/渠道/地域的授权列表（共同权利人不全时为空）。"""
        return {
            artwork_id: (detail["hits"] if not detail["missing"] else [])
            for artwork_id, detail in self._coverage(
                sku, at, channel, region, for_production, ignore_window
            ).items()
        }

    def feasible_channels(self, sku: Sku, at: datetime, region: Optional[str] = None,
                          for_production: bool = True, ignore_window: bool = False) -> dict[str, bool]:
        """渠道是否落在全部作品许可的交集内。"""
        result = {}
        for channel in sku.channels:
            covering = self.covering_licenses(
                sku, at, channel, region, for_production, ignore_window
            )
            result[channel] = bool(sku.artwork_ids) and all(covering[a] for a in sku.artwork_ids)
        return result

    # ---------- 配额 ----------

    def _license_usage(self, license_id: str, at: datetime) -> int:
        """已计入某授权配额的单位数：已放行批次 + 尚未兑现批次的补货承诺。"""
        used = 0
        released_batches = set(self.batches)
        for commit in self.restock_commits:
            if commit["at"] > at:
                continue
            if commit["batch_id"] not in released_batches and self._sku_covered_by(
                commit["sku"], license_id, commit["at"]
            ):
                used += commit["quantity"]
        for batch in self.batches.values():
            if batch["released_at"] > at:
                continue
            if any(s["license_id"] == license_id for s in batch["rights_snapshot"]):
                used += batch["quantity"]
        return used

    def _sku_covered_by(self, sku_id: str, license_id: str, at: datetime) -> bool:
        sku = self.skus.get(sku_id)
        if not sku:
            return False
        return any(
            lic.license_id == license_id
            for artwork_id in sku.artwork_ids
            for lic, _ in self.effective_licenses(artwork_id, at)
        )

    def production_headroom(self, sku: Sku, at: datetime, channel: str,
                            region: Optional[str] = None) -> Optional[int]:
        """交付余量：交集内各授权剩余配额的最小值；任一授权不限量则不构成约束。"""
        covering = self.covering_licenses(sku, at, channel, region, for_production=True)
        minimum: Optional[int] = None
        for artwork_id, hits in covering.items():
            for lic, st in hits:
                if st["max_units"] is None:
                    continue
                remaining = st["max_units"] - self._license_usage(lic.license_id, at)
                minimum = remaining if minimum is None else min(minimum, remaining)
        return minimum

    # ---------- 门禁 ----------

    def _design_sample_blockers(self, sku: Sku, at: datetime) -> list[str]:
        blockers = []
        design = self.designs.get(sku.design_id)
        if not design:
            return [f"设计提案不存在：{sku.design_id}"]
        design_decided = design.get("decided_at")
        design_status = design["status"] if (design_decided is None or design_decided <= at) else "SUBMITTED"
        if design_status != "APPROVED":
            blockers.append(f"设计提案 {sku.design_id} 状态为 {design_status}，未经审核通过")
        rounds = [
            s for s in self.samples.values()
            if s["design_id"] == sku.design_id and s["submitted_at"] <= at
        ]
        if not rounds:
            blockers.append("尚无打样记录")
        else:
            latest = max(rounds, key=lambda s: (s["round_no"], s["submitted_at"]))
            status = latest["status"]
            decided = latest.get("decided_at")
            if decided is not None and decided > at:
                status = "SUBMITTED"
            if status != "APPROVED":
                blockers.append(f"最新打样批次（第 {latest['round_no']} 轮）状态为 {status}")
        change_at = sku.material_changed_at
        if change_at and change_at <= at and (
            not rounds or max(s["submitted_at"] for s in rounds) <= change_at
        ):
            blockers.append("供应商已换料，换料后尚无重新打样通过的批次，不得继续生产")
        return blockers

    def _channel_listing_state(self, sku: Sku, channel: str, at: datetime) -> dict:
        """渠道下架状态（只考虑查询时点之前的动作）：是否允许在售罄期内出售既有库存。"""
        action = None
        for candidate in sku.channel_actions.get(channel, []):
            if candidate["at"] <= at:
                action = candidate
            else:
                break
        if not action or action["action"] != "DELISTED":
            return {"delisted": False}
        disp = action["inventory_disposition"]
        sell_through_active = False
        until = None
        if disp == "SELL_THROUGH":
            raw = action.get("sell_through_until")
            until = _date(raw) if raw else at.date()
            sell_through_active = at.date() <= until
        return {
            "delisted": True,
            "reason_code": action["reason_code"],
            "disposition": disp,
            "sell_through_active": sell_through_active,
            "sell_through_until": until.isoformat() if until else None,
            "blocker": None if sell_through_active else (
                f"渠道 {channel} 已下架（{action['reason_code']}），售罄期已于 {until} 结束"
                if disp == "SELL_THROUGH"
                else f"渠道 {channel} 已下架（{action['reason_code']}，库存处置 {disp}）"
            ),
        }

    def _channel_listing_blockers(self, sku: Sku, channel: str, at: datetime) -> list[str]:
        state = self._channel_listing_state(sku, channel, at)
        return [state["blocker"]] if state.get("blocker") else []

    def gate(self, sku_id: str, at: datetime, region: Optional[str] = None) -> dict:
        """开售门禁：回答某 SKU 此刻能否生产、各渠道能否销售、余量多少。"""
        sku = self.skus[sku_id]
        report = {"sku": sku_id, "at": at.isoformat(), "region": region, "channels": {}}

        design_blockers = self._design_sample_blockers(sku, at)
        clearance_blockers = []
        clearance_at = sku.clearance["at"] if sku.clearance else None
        if not clearance_at or clearance_at > at:
            reason = "未经过开售放行审批"
            rejected = sku.clearance_rejected
            if rejected and rejected["at"] <= at:
                reason = f"开售审批被驳回：{'；'.join(rejected['reasons'])}"
            clearance_blockers.append(reason)

        channel_feasible = self.feasible_channels(sku, at, region, for_production=True)
        for channel in sku.channels:
            coverage = self._coverage(sku, at, channel, region, for_production=True)
            missing = [a for a in sku.artwork_ids if coverage[a]["missing"]]
            prod_blockers = list(design_blockers) + list(clearance_blockers)
            for a in missing:
                prod_blockers.append(f"作品 {a}：" + "；".join(coverage[a]["missing"]))
            headroom = None
            if not missing:
                headroom = self.production_headroom(sku, at, channel, region)
                if headroom is not None and headroom <= 0:
                    prod_blockers.append("授权生产配额已用尽，需取得补货授权或限量封顶")

            sell_coverage = self._coverage(sku, at, channel, region, for_production=False)
            sell_missing = [a for a in sku.artwork_ids if sell_coverage[a]["missing"]]
            listing = self._channel_listing_state(sku, channel, at)
            sell_blockers: list[str] = []
            sell_through_only = False
            on_hand = 0
            for bid, b in self.batches.items():
                if b["sku"] != sku_id or b["released_at"] > at:
                    continue
                sold = sum(
                    s["quantity"] for s in self.sales
                    if s["batch_id"] == bid and s["channel"] == channel
                    and s["sold_at_dt"] <= at
                )
                on_hand += b["quantity"] - sold
            if listing["delisted"]:
                if listing["sell_through_active"] and on_hand > 0:
                    # 撤权/收窄后的售罄安排：只允许出售已放行批次的既有库存
                    sell_through_only = True
                else:
                    sell_blockers.append(listing["blocker"])
            if sell_missing and not sell_through_only:
                sell_blockers.append(
                    "无销售授权：" + "；".join(
                        f"作品 {a}：{'；'.join(sell_coverage[a]['missing'])}" for a in sell_missing
                    )
                )

            report["channels"][channel] = {
                "can_produce": not prod_blockers,
                "production_blockers": prod_blockers,
                "headroom_units": headroom,
                "can_sell": not sell_blockers,
                "sell_through_only": sell_through_only,
                "on_hand_units": on_hand,
                "sales_blockers": sell_blockers,
                "rights_intersection": {
                    a: [
                        {
                            "license_id": lic.license_id,
                            "license_version": self.store.version_of("license_grant", lic.license_id),
                            "window": [st["window"][0].isoformat(), st["window"][1].isoformat()],
                            "withdrawn": st["withdrawn"],
                            "max_units": st["max_units"],
                        }
                        for lic, st in coverage[a]["hits"]
                    ]
                    for a in sku.artwork_ids
                },
            }

        report["can_produce_anywhere"] = any(
            c["can_produce"] for c in report["channels"].values()
        )
        return report

    # ---------- 批次、销售与分成 ----------

    def batch_legality(self, batch_id: str) -> dict:
        """已放行批次的合法性完全以其冻结快照为准，不受授权事后变化影响。"""
        batch = self.batches[batch_id]
        basis = []
        for snap in batch["rights_snapshot"]:
            lic = self.licenses.get(snap["license_id"])
            candidate = self.candidates.get(snap["licensor_candidate_id"])
            basis.append({
                "artwork_id": snap["artwork_id"],
                "license_id": snap["license_id"],
                "license_version_at_release": snap["license_version"],
                "license_current_version": self.store.version_of("license_grant", snap["license_id"]),
                "licensor": candidate.claimant if candidate else None,
                "licensor_verified": bool(candidate and candidate.status == "VERIFIED"),
                "evidence_refs": candidate.evidence_refs if candidate else [],
            })
        sold = sum(s["quantity"] for s in self.sales if s["batch_id"] == batch_id)
        return {
            "batch_id": batch_id,
            "sku": batch["sku"],
            "quantity": batch["quantity"],
            "sold": sold,
            "on_hand": batch["quantity"] - sold,
            "released_at": batch["released_at"].isoformat(),
            "rights_basis": basis,
            "royalty_terms": batch["royalty_terms"],
        }

    def sales_within_batch(self, batch_id: str) -> None:
        """校验：累计销量不得超过批次放行数量。"""
        batch = self.batches[batch_id]
        sold = sum(s["quantity"] for s in self.sales if s["batch_id"] == batch_id)
        if sold > batch["quantity"]:
            raise ValueError(
                f"批次 {batch_id} 销售 {sold} 件超过放行数量 {batch['quantity']} 件"
            )

    def royalty_due_for_batch(self, batch_id: str, quantity: int, revenue_per_unit: float) -> list[dict]:
        """按批次冻结的分成条件计算应付分成。"""
        due = []
        for term in self.batches[batch_id]["royalty_terms"]:
            if term["basis"] == "PERCENT_OF_REVENUE":
                amount = round(quantity * revenue_per_unit * term["rate"], 2)
            elif term["basis"] == "PER_UNIT":
                amount = round(quantity * term["rate"], 2)
            else:
                amount = round(term["rate"], 2)
            due.append({"license_id": term["license_id"], "amount": amount})
        return due

    # ---------- 反查链 ----------

    def trace_sku(self, sku_id: str) -> dict:
        """下架/放行反查：SKU → 审批人与快照 → 授权版本 → 权属候选/核验 → 批次。"""
        sku = self.skus[sku_id]
        clearance = None
        if sku.clearance:
            clearance = {
                "event_id": sku.clearance["event_id"],
                "approver": sku.clearance["approver"],
                "approved_at": sku.clearance["approved_at"],
                "snapshot": [],
            }
            for snap in sku.clearance["rights_snapshot"]:
                candidate = self.candidates.get(snap["licensor_candidate_id"])
                lic = self.licenses.get(snap["license_id"])
                clearance["snapshot"].append({
                    "artwork_id": snap["artwork_id"],
                    "license_id": snap["license_id"],
                    "license_version": snap["license_version"],
                    "license_current_version": self.store.version_of(
                        "license_grant", snap["license_id"]
                    ),
                    "licensor_candidate_id": snap["licensor_candidate_id"],
                    "licensor_claimant": candidate.claimant if candidate else None,
                    "verifier": candidate.verifier if candidate else None,
                    "verification_basis": candidate.basis if candidate else None,
                    "evidence_refs": candidate.evidence_refs if candidate else [],
                    "license_withdrawn": bool(lic and lic.withdrawn),
                })

        delistings = []
        for record in sku.delisting_history:
            entry = {"channels": record["channels"]}
            entry.update({
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in record.items() if k != "channels"
            })
            delistings.append(entry)
        return {
            "sku": sku_id,
            "design_id": sku.design_id,
            "artwork_ids": sku.artwork_ids,
            "use_class": sku.use_class,
            "clearance": clearance,
            "delistings": delistings,
            "batches": [
                self.batch_legality(bid)
                for bid, b in self.batches.items() if b["sku"] == sku_id
            ],
        }
