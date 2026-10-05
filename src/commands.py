"""应用命令服务：执行业务前置校验并发出后继版本事件。

所有写操作都经过这里：命令本身不直接改状态，只产出事件交给 append-only 存储。
开售门禁的关键前置（权属核验、争议、许可交集、配额、样品/换料、销售窗口）在
放行、排产、补货、销售命令处强制执行；不满足时返回/抛出拒绝原因。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .domain import RightsProjection
from .event_store import EventStore, EventStoreError


class CommandError(ValueError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("；".join(reasons))


class CommandService:
    def __init__(self, store: EventStore):
        self.store = store

    # ---------- 基础设施 ----------

    def _next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self.store.version_of(aggregate_type, aggregate_id) + 1

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str,
              at: str, summary: str, payload: dict, event_id: str,
              causation_id: Optional[str] = None) -> dict:
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": at,
            "version": self._next_version(aggregate_type, aggregate_id),
            "summary": summary,
            "payload": payload,
        }
        if causation_id:
            event["causation_id"] = causation_id
        return self.store.append(event)

    def _view(self) -> RightsProjection:
        return RightsProjection(self.store)

    # ---------- 展期 ----------

    def schedule_exhibition(self, event_id, exhibition_id, title, start_date, end_date, at, hall=None):
        payload = {"exhibition_id": exhibition_id, "title": title,
                   "start_date": start_date, "end_date": end_date}
        if hall:
            payload["hall"] = hall
        return self._emit("EXHIBITION_SCHEDULED", "exhibition", exhibition_id, at,
                          f"特展《{title}》定档 {start_date} 至 {end_date}", payload, event_id)

    def reschedule_exhibition(self, event_id, exhibition_id, previous, current, reason, at):
        payload = {"previous": previous, "current": current, "reason": reason}
        return self._emit("EXHIBITION_RESCHEDULED", "exhibition", exhibition_id, at,
                          f"特展改期：{reason}", payload, event_id)

    def reschedule_exhibition_and_delist(self, event_id, exhibition_id, previous, current,
                                         reason, at, approver, disposition, sell_through_until=None):
        """改期后把新窗口外无法销售的 SKU×渠道精确下架（EXHIBITION_WINDOW 授权）。"""
        rescheduled = self.reschedule_exhibition(
            event_id, exhibition_id, previous, current, reason, at,
        )
        at_dt = datetime.fromisoformat(at)
        view = self._view()
        delistings, seq = [], 0
        for sku_id, sku in view.skus.items():
            if not sku.clearance:
                continue
            license_ids = {s["license_id"] for s in sku.clearance["rights_snapshot"]}
            if not any(
                view.licenses[lid].exhibition_id == exhibition_id
                for lid in license_ids if lid in view.licenses
            ):
                continue
            affected = []
            for channel in sku.clearance["channels"]:
                action = (sku.channel_actions[channel][-1] if sku.channel_actions.get(channel) else None)
                if action and action["action"] == "DELISTED":
                    continue
                after = view.covering_licenses(sku, at_dt, channel, None, for_production=False)
                if any(not after.get(a) for a in sku.artwork_ids):
                    affected.append(channel)
            if affected:
                seq += 1
                delistings.append(self.delist(
                    f"{event_id}#delist-{seq}", sku_id, affected, "EXHIBITION_ENDED",
                    rescheduled["event_id"], disposition, approver, at, sell_through_until,
                ))
        return rescheduled, delistings

    # ---------- 权属 ----------

    def acquire_artwork(self, event_id, artwork_id, title, acquisition_date,
                        copyright_status, at, artist_display=None, acquisition_mode=None,
                        copyright_deed_ref=None):
        payload = {"artwork_id": artwork_id, "title": title,
                   "acquisition_date": acquisition_date, "copyright_status": copyright_status}
        if artist_display:
            payload["artist_display"] = artist_display
        if acquisition_mode:
            payload["acquisition_mode"] = acquisition_mode
        if copyright_deed_ref:
            payload["copyright_deed_ref"] = copyright_deed_ref
        return self._emit("ARTWORK_ACQUIRED", "artwork_right", artwork_id, at,
                          f"作品《{title}》入藏登记（著作权状态：{copyright_status}）", payload, event_id)

    def claim_right(self, event_id, artwork_id, candidate_id, claimant, relationship,
                    claimed_rights, evidence_refs, at, share_pct=None):
        payload = {"artwork_id": artwork_id, "candidate_id": candidate_id, "claimant": claimant,
                   "relationship": relationship, "claimed_rights": claimed_rights,
                   "evidence_refs": evidence_refs}
        if share_pct is not None:
            payload["share_pct"] = share_pct
        return self._emit("RIGHT_CLAIMED", "artwork_right", artwork_id, at,
                          f"登记权属候选：{claimant} 主张 {','.join(claimed_rights)}", payload, event_id)

    def verify_claim(self, event_id, candidate_id, verifier, verified_rights, basis, at, share_pct=None):
        payload = {"candidate_id": candidate_id, "verifier": verifier,
                   "verified_rights": verified_rights, "basis": basis}
        if share_pct is not None:
            payload["share_pct"] = share_pct
        view = self._view()
        candidate = view.candidates.get(candidate_id)
        if not candidate:
            raise CommandError([f"权属候选不存在：{candidate_id}"])
        return self._emit("RIGHT_CLAIM_VERIFIED", "artwork_right", candidate.artwork_id, at,
                          f"权属候选 {candidate_id}（{candidate.claimant}）经 {verifier} 核验通过",
                          payload, event_id)

    def reject_claim(self, event_id, candidate_id, verifier, reason, at):
        view = self._view()
        candidate = view.candidates.get(candidate_id)
        if not candidate:
            raise CommandError([f"权属候选不存在：{candidate_id}"])
        return self._emit("RIGHT_CLAIM_REJECTED", "artwork_right", candidate.artwork_id, at,
                          f"权属候选 {candidate_id} 核验驳回：{reason}",
                          {"candidate_id": candidate_id, "verifier": verifier, "reason": reason}, event_id)

    def open_dispute(self, event_id, artwork_id, dispute_id, subject, opened_by, at, candidate_ids=None):
        payload = {"artwork_id": artwork_id, "dispute_id": dispute_id,
                   "subject": subject, "opened_by": opened_by}
        if candidate_ids:
            payload["candidate_ids"] = candidate_ids
        return self._emit("RIGHT_DISPUTE_OPENED", "artwork_right", artwork_id, at,
                          f"作品 {artwork_id} 权利争议立案：{subject}", payload, event_id)

    def open_dispute_and_delist(self, event_id, artwork_id, dispute_id, subject, opened_by, at,
                                approver, disposition, candidate_ids=None, sell_through_until=None):
        """争议立案：其他候选仍可并行核验，但该作品相关 SKU 渠道立即下架。"""
        dispute = self.open_dispute(event_id, artwork_id, dispute_id, subject, opened_by, at,
                                    candidate_ids)
        at_dt = datetime.fromisoformat(at)
        view = self._view()
        delistings, seq = [], 0
        for sku_id, sku in view.skus.items():
            if not sku.clearance or artwork_id not in sku.artwork_ids:
                continue
            affected = []
            for channel in sku.clearance["channels"]:
                action = (sku.channel_actions[channel][-1] if sku.channel_actions.get(channel) else None)
                if action and action["action"] == "DELISTED":
                    continue
                after = view.covering_licenses(sku, at_dt, channel, None, for_production=False)
                if any(not after.get(a) for a in sku.artwork_ids):
                    affected.append(channel)
            if affected:
                seq += 1
                delistings.append(self.delist(
                    f"{event_id}#delist-{seq}", sku_id, affected, "DISPUTE_OPENED",
                    dispute["event_id"], disposition, approver, at, sell_through_until,
                ))
        return dispute, delistings

    def resolve_dispute(self, event_id, dispute_id, resolution, resolved_by, at,
                        winner_candidate_id=None, shares=None):
        payload = {"dispute_id": dispute_id, "resolution": resolution, "resolved_by": resolved_by}
        if winner_candidate_id:
            payload["winner_candidate_id"] = winner_candidate_id
        if shares:
            payload["shares"] = shares
        view = self._view()
        dispute = view.disputes.get(dispute_id)
        if not dispute:
            raise CommandError([f"争议不存在：{dispute_id}"])
        return self._emit("RIGHT_DISPUTE_RESOLVED", "artwork_right", dispute["artwork_id"], at,
                          f"争议 {dispute_id} 解决：{resolution}", payload, event_id)

    # ---------- 授权 ----------

    def grant_license(self, event_id, license_id, artwork_ids, licensor_candidate_id, rights,
                      use_class, territories, channels, term, term_basis, royalty, at,
                      exhibition_id=None, max_units=None, sell_through_days=0):
        view = self._view()
        candidate = view.candidates.get(licensor_candidate_id)
        if not candidate:
            raise CommandError([f"授权人候选不存在：{licensor_candidate_id}"])
        if candidate.artwork_id not in artwork_ids:
            raise CommandError(["授权人候选与被授权作品不匹配"])
        payload = {
            "license_id": license_id, "artwork_ids": artwork_ids,
            "licensor_candidate_id": licensor_candidate_id, "rights": rights,
            "use_class": use_class, "territories": territories, "channels": channels,
            "term": {"start_date": term[0], "end_date": term[1]}, "term_basis": term_basis,
            "max_units": max_units, "sell_through_days": sell_through_days, "royalty": royalty,
        }
        if exhibition_id:
            payload["exhibition_id"] = exhibition_id
        return self._emit("LICENSE_GRANTED", "license_grant", license_id, at,
                          f"授权 {license_id} 生效（{use_class}，{len(artwork_ids)} 件作品）",
                          payload, event_id)

    def amend_license(self, event_id, license_id, change_type, changes, effective_at, summary):
        view = self._view()
        if license_id not in view.licenses:
            raise CommandError([f"授权不存在：{license_id}"])
        return self._emit("LICENSE_AMENDED", "license_grant", license_id, effective_at, summary,
                          {"change_type": change_type, "effective_at": effective_at, "changes": changes},
                          event_id)

    def narrow_license_and_delist(self, event_id, license_id, changes, effective_at,
                                  approver, inventory_disposition, sell_through_until=None):
        """授权收窄：先追加修订，再把失去交集的 SKU×渠道精确下架。"""
        amendment = self.amend_license(
            event_id, license_id, "NARROW", changes, effective_at,
            f"授权 {license_id} 收窄并联动下架受影响渠道",
        )
        return amendment, self._propagate_rights_change(
            license_id, effective_at, approver, "LICENSE_NARROWED",
            amendment["event_id"], inventory_disposition, sell_through_until,
        )

    def withdraw_license(self, event_id, license_id, reason, effective_at,
                         sell_through_until=None):
        view = self._view()
        if license_id not in view.licenses:
            raise CommandError([f"授权不存在：{license_id}"])
        payload = {"reason": reason, "effective_at": effective_at}
        if sell_through_until:
            payload["sell_through_until"] = sell_through_until
        return self._emit("LICENSE_WITHDRAWN", "license_grant", license_id, effective_at,
                          f"授权 {license_id} 撤回：{reason}", payload, event_id)

    def withdraw_license_and_delist(self, event_id, license_id, reason, effective_at,
                                    approver, inventory_disposition, sell_through_until=None):
        """撤回授权：撤回事件 + 受影响 SKU×渠道联动下架，causation 指向撤回事件。"""
        withdrawal = self.withdraw_license(event_id, license_id, reason, effective_at,
                                           sell_through_until)
        delistings = self._propagate_rights_change(
            license_id, effective_at, approver, "RIGHT_WITHDRAWN",
            withdrawal["event_id"], inventory_disposition, sell_through_until,
        )
        return withdrawal, delistings

    def _propagate_rights_change(self, license_id, effective_at, approver, reason_code,
                                 trigger_event_id, inventory_disposition, sell_through_until):
        """比较变更后状态，对每个已放行 SKU 恰好下架失去授权交集的渠道。"""
        at = datetime.fromisoformat(effective_at)
        view = self._view()
        lic = view.licenses[license_id]
        delistings = []
        seq = 0
        for sku_id, sku in view.skus.items():
            if not sku.clearance or not set(sku.artwork_ids) & set(lic.artwork_ids):
                continue
            affected = []
            for channel in sku.clearance["channels"]:
                action = (sku.channel_actions[channel][-1] if sku.channel_actions.get(channel) else None)
                if action and action["action"] == "DELISTED":
                    continue
                after = view.covering_licenses(sku, at, channel, None, for_production=False)
                if any(not after.get(a) for a in sku.artwork_ids):
                    affected.append(channel)
            if affected:
                seq += 1
                delistings.append(self.delist(
                    f"{trigger_event_id}#delist-{seq}", sku_id, affected, reason_code,
                    trigger_event_id, inventory_disposition, approver, effective_at,
                    sell_through_until,
                ))
        return delistings

    # ---------- 设计与打样 ----------

    def submit_design(self, event_id, design_id, artwork_ids, use_class, designer,
                      adaptation_level, spec_ref, at):
        payload = {"design_id": design_id, "artwork_ids": artwork_ids, "use_class": use_class,
                   "designer": designer, "adaptation_level": adaptation_level, "spec_ref": spec_ref}
        return self._emit("DESIGN_SUBMITTED", "design_proposal", design_id, at,
                          f"设计提案 {design_id} 提交（{designer}）", payload, event_id)

    def approve_design(self, event_id, design_id, reviewer, approved_at, notes=None):
        payload = {"reviewer": reviewer, "approved_at": approved_at}
        if notes:
            payload["notes"] = notes
        return self._emit("DESIGN_APPROVED", "design_proposal", design_id, approved_at,
                          f"设计提案 {design_id} 审核通过", payload, event_id)

    def reject_design(self, event_id, design_id, reviewer, reason, at):
        return self._emit("DESIGN_REJECTED", "design_proposal", design_id, at,
                          f"设计提案 {design_id} 驳回：{reason}",
                          {"reviewer": reviewer, "reason": reason}, event_id)

    def submit_sample(self, event_id, sample_id, design_id, round_no, supplier,
                      material_spec_ref, submitted_at):
        payload = {"sample_id": sample_id, "design_id": design_id, "round_no": round_no,
                   "supplier": supplier, "material_spec_ref": material_spec_ref,
                   "submitted_at": submitted_at}
        return self._emit("SAMPLE_SUBMITTED", "sample_round", sample_id, submitted_at,
                          f"设计 {design_id} 第 {round_no} 轮打样送检", payload, event_id)

    def approve_sample(self, event_id, sample_id, reviewer, approved_at, notes=None):
        payload = {"reviewer": reviewer, "approved_at": approved_at}
        if notes:
            payload["notes"] = notes
        return self._emit("SAMPLE_APPROVED", "sample_round", sample_id, approved_at,
                          f"样品 {sample_id} 审核通过", payload, event_id)

    def reject_sample(self, event_id, sample_id, reviewer, reason, at):
        return self._emit("SAMPLE_REJECTED", "sample_round", sample_id, at,
                          f"样品 {sample_id} 驳回：{reason}",
                          {"reviewer": reviewer, "reason": reason}, event_id)

    def substitute_material(self, event_id, sku, previous_material_ref, new_material_ref,
                            supplier, changed_at):
        return self._emit("MATERIAL_SUBSTITUTED", "product_sku", sku, changed_at,
                          f"SKU {sku} 供应商换料：{previous_material_ref} → {new_material_ref}",
                          {"sku": sku, "previous_material_ref": previous_material_ref,
                           "new_material_ref": new_material_ref, "supplier": supplier,
                           "changed_at": changed_at}, event_id)

    # ---------- SKU 与门禁 ----------

    def register_sku(self, event_id, sku, design_id, channels, unit_price, at):
        view = self._view()
        design = view.designs.get(design_id)
        if not design:
            raise CommandError([f"设计提案不存在：{design_id}"])
        payload = {"sku": sku, "design_id": design_id, "artwork_ids": list(design["artwork_ids"]),
                   "use_class": design["use_class"], "channels": channels,
                   "unit_price": unit_price, "registered_at": at}
        return self._emit("SKU_REGISTERED", "product_sku", sku, at,
                          f"SKU {sku} 登记（{design_id}）", payload, event_id)

    def _snapshot_for(self, view, sku, at, region):
        approved_channels = []
        chosen: dict[str, list] = {}
        feasible = view.feasible_channels(sku, at, region, for_production=True, ignore_window=True)
        for channel, ok in feasible.items():
            if ok:
                approved_channels.append(channel)
                covering = view.covering_licenses(
                    sku, at, channel, region, for_production=True, ignore_window=True
                )
                for artwork_id, hits in covering.items():
                    for lic, _ in hits:
                        entry = {
                            "artwork_id": artwork_id,
                            "license_id": lic.license_id,
                            "license_version": self.store.version_of("license_grant", lic.license_id),
                            "licensor_candidate_id": lic.licensor_candidate_id,
                        }
                        chosen.setdefault(artwork_id, [])
                        if entry not in chosen[artwork_id]:
                            chosen[artwork_id].append(entry)
        snapshot = [item for items in chosen.values() for item in items]
        return approved_channels, snapshot

    def request_clearance(self, event_id, sku, approver, at, region=None, notes=None):
        """开售门禁审批：通过则冻结权利快照；不通过则记录驳回原因。"""
        view = self._view()
        if sku not in view.skus:
            raise CommandError([f"SKU 不存在：{sku}"])
        sku_obj = view.skus[sku]
        at_dt = datetime.fromisoformat(at)

        reasons = view._design_sample_blockers(sku_obj, at_dt)
        approved_channels, snapshot = self._snapshot_for(view, sku_obj, at_dt, region)
        missing_artworks = set()
        for channel in sku_obj.channels:
            covering = view.covering_licenses(
                sku_obj, at_dt, channel, region, for_production=True, ignore_window=True
            )
            missing_artworks.update(a for a, hits in covering.items() if not hits)
        if missing_artworks:
            reasons.append(f"作品 {','.join(sorted(missing_artworks))} 缺少覆盖全部渠道的有效授权交集")
        if not approved_channels:
            reasons.append("没有任何渠道同时满足许可交集、期限与用途要求")

        if reasons:
            return self._emit(
                "CLEARANCE_REJECTED", "product_sku", sku, at,
                f"SKU {sku} 开售审批驳回（{len(reasons)} 项原因）",
                {"sku": sku, "reviewer": approver, "rejected_at": at, "reasons": reasons},
                event_id,
            )

        payload = {"sku": sku, "approver": approver, "approved_at": at,
                   "channels": approved_channels, "rights_snapshot": snapshot}
        if notes:
            payload["notes"] = notes
        return self._emit("CLEARANCE_APPROVED", "product_sku", sku, at,
                          f"SKU {sku} 开售放行，渠道：{','.join(approved_channels)}",
                          payload, event_id)

    def commit_restock(self, event_id, sku, quantity, batch_id, committed_at,
                       channel: str, region=None):
        view = self._view()
        sku_obj = view.skus[sku]
        at = datetime.fromisoformat(committed_at)
        blockers = view._design_sample_blockers(sku_obj, at)
        if not sku_obj.clearance:
            blockers.append("SKU 未获开售放行")
        headroom = view.production_headroom(sku_obj, at, channel, region)
        covering = view.covering_licenses(sku_obj, at, channel, region, for_production=True)
        if any(not hits for hits in covering.values()):
            blockers.append(f"渠道 {channel} 授权交集不成立，不得补货")
        if headroom is not None and headroom < quantity:
            blockers.append(
                f"补货 {quantity} 件超出授权交付余量 {headroom} 件"
            )
        if blockers:
            raise CommandError(blockers)
        return self._emit("RESTOCK_COMMITTED", "product_sku", sku, committed_at,
                          f"SKU {sku} 限量补货承诺 {quantity} 件（批次 {batch_id}）",
                          {"sku": sku, "quantity": quantity, "batch_id": batch_id,
                           "committed_at": committed_at}, event_id)

    def release_batch(self, event_id, batch_id, sku, quantity, material_ref, released_at,
                      channel, region=None):
        view = self._view()
        sku_obj = view.skus[sku]
        at = datetime.fromisoformat(released_at)
        blockers = view._design_sample_blockers(sku_obj, at)
        if not sku_obj.clearance or channel not in sku_obj.clearance["channels"]:
            blockers.append(f"渠道 {channel} 不在放行渠道内")
        covering = view.covering_licenses(sku_obj, at, channel, region, for_production=True)
        missing = [a for a, hits in covering.items() if not hits]
        if missing:
            blockers.append(f"作品 {','.join(missing)} 在排产时点无有效授权交集")
        headroom = view.production_headroom(sku_obj, at, channel, region) if not missing else None
        if headroom is not None and headroom < quantity:
            blockers.append(f"排产 {quantity} 件超出授权交付余量 {headroom} 件")
        if blockers:
            raise CommandError(blockers)

        snapshot, seen = [], set()
        royalty_terms, term_seen = [], set()
        for artwork_id, hits in covering.items():
            # 合作作品：命中的每一授权（每位共同权利人）都要冻结快照与分成条款
            for lic, _ in hits:
                key = (artwork_id, lic.license_id)
                if key not in seen:
                    seen.add(key)
                    snapshot.append({
                        "artwork_id": artwork_id,
                        "license_id": lic.license_id,
                        "license_version": self.store.version_of("license_grant", lic.license_id),
                        "licensor_candidate_id": lic.licensor_candidate_id,
                    })
                if lic.license_id not in term_seen:
                    term_seen.add(lic.license_id)
                    royalty_terms.append({"license_id": lic.license_id, **lic.royalty})

        payload = {"batch_id": batch_id, "sku": sku, "quantity": quantity,
                   "material_ref": material_ref, "released_at": released_at,
                   "rights_snapshot": snapshot, "royalty_terms": royalty_terms}
        return self._emit("BATCH_RELEASED", "production_batch", batch_id, released_at,
                          f"批次 {batch_id} 排产 {quantity} 件（SKU {sku}，渠道 {channel}）",
                          payload, event_id)

    def record_sale(self, event_id, statement_id, sku, batch_id, channel, quantity,
                    unit_price, sold_at):
        view = self._view()
        at = datetime.fromisoformat(sold_at)
        batch = view.batches.get(batch_id)
        if not batch or batch["sku"] != sku:
            raise CommandError([f"批次 {batch_id} 不存在或不属于 SKU {sku}"])
        already = sum(s["quantity"] for s in view.sales if s["batch_id"] == batch_id)
        if already + quantity > batch["quantity"]:
            raise CommandError([
                f"批次 {batch_id} 累计销售将达 {already + quantity} 件，超过放行 {batch['quantity']} 件"
            ])
        sku_obj = view.skus[sku]
        listing = view._channel_listing_state(sku_obj, channel, at)
        sell_through_ok = False
        if listing["delisted"]:
            on_hand = batch["quantity"] - already
            if listing["sell_through_active"] and on_hand >= quantity:
                # 售罄安排：只动既有库存，结算依据仍是批次冻结快照
                sell_through_ok = True
            else:
                raise CommandError([
                    listing["blocker"]
                    or f"售罄期内剩余库存 {on_hand} 件，不足以销售 {quantity} 件"
                ])
        if not sell_through_ok:
            sell_coverage = view._coverage(sku_obj, at, channel, None, for_production=False)
            missing = [a for a, d in sell_coverage.items() if d["missing"]]
            if missing:
                raise CommandError([
                    "无销售授权：" + "；".join(
                        f"作品 {a}：{'；'.join(sell_coverage[a]['missing'])}" for a in missing
                    )
                ])

        royalty_due = view.royalty_due_for_batch(batch_id, quantity, unit_price)
        for line in royalty_due:
            lic = view.licenses.get(line["license_id"])
            if lic:
                line["candidate_id"] = lic.licensor_candidate_id
        payload = {"statement_id": statement_id, "sku": sku, "batch_id": batch_id,
                   "channel": channel, "quantity": quantity, "unit_price": unit_price,
                   "sold_at": sold_at, "royalty_due": royalty_due}
        return self._emit("SALE_RECORDED", "royalty_statement", statement_id, sold_at,
                          f"销售记账 {statement_id}：{sku} ×{quantity}（{channel}）",
                          payload, event_id)

    def settle(self, event_id, statement_id, period_start, period_end, settled_at, lines):
        payload = {"statement_id": statement_id, "period_start": period_start,
                   "period_end": period_end, "settled_at": settled_at, "lines": lines}
        return self._emit("ROYALTY_SETTLED", "royalty_statement", statement_id, settled_at,
                          f"分成结算单 {statement_id} 结清", payload, event_id)

    def delist(self, event_id, sku, channels, reason_code, trigger_event_id,
               inventory_disposition, approver, delisted_at, sell_through_until=None):
        payload = {"sku": sku, "channels": channels, "reason_code": reason_code,
                   "trigger_event_id": trigger_event_id,
                   "inventory_disposition": inventory_disposition,
                   "approver": approver, "delisted_at": delisted_at}
        if sell_through_until:
            payload["sell_through_until"] = sell_through_until
        return self._emit("SKU_DELISTED", "product_sku", sku, delisted_at,
                          f"SKU {sku} 在 {','.join(channels)} 下架（{reason_code}）",
                          payload, event_id, causation_id=trigger_event_id)

    def relist(self, event_id, sku, channels, reason, approver, relisted_at):
        return self._emit("SKU_RELISTED", "product_sku", sku, relisted_at,
                          f"SKU {sku} 在 {','.join(channels)} 重新上架：{reason}",
                          {"sku": sku, "channels": channels, "reason": reason,
                           "approver": approver, "relisted_at": relisted_at}, event_id)
