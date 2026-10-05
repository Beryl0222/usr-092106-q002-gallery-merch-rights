import json
import unittest
from datetime import datetime
from pathlib import Path

from src.commands import CommandError, CommandService
from src.domain import RightsProjection
from src.event_store import EventStore, EventStoreError
from src.scenario import build_scenario
from src.validator import validate_event

ROOT = Path(__file__).parents[1]
DT = datetime.fromisoformat


class EnvelopeTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_missing_fields_and_bad_version(self) -> None:
        errors = validate_event({"event_id": "x"})
        self.assertIn("缺少字段：event_type", errors)
        bad = {
            "event_id": "e1", "event_type": "RIGHT_CLAIMED", "aggregate_type": "artwork_right",
            "aggregate_id": "a1", "occurred_at": "2026-09-01T10:00:00+08:00",
            "version": 0, "summary": "s",
        }
        self.assertIn("version 必须是正整数", validate_event(bad))

    def test_event_aggregate_mismatch(self) -> None:
        event = {
            "event_id": "e1", "event_type": "BATCH_RELEASED", "aggregate_type": "product_sku",
            "aggregate_id": "a1", "occurred_at": "2026-09-01T10:00:00+08:00",
            "version": 1, "summary": "s",
        }
        self.assertTrue(any("aggregate_type 必须是 production_batch" for e in validate_event(event)))

    def test_payload_deep_validation(self) -> None:
        event = {
            "event_id": "e1", "event_type": "RIGHT_CLAIMED", "aggregate_type": "artwork_right",
            "aggregate_id": "AW-1", "occurred_at": "2026-09-01T10:00:00+08:00",
            "version": 1, "summary": "s",
            "payload": {"artwork_id": "AW-1"},
        }
        errors = validate_event(event)
        self.assertIn("payload 缺少字段：candidate_id", errors)

    def test_exhibition_window_requires_exhibition_id(self) -> None:
        event = {
            "event_id": "e1", "event_type": "LICENSE_GRANTED",
            "aggregate_type": "license_grant", "aggregate_id": "L-1",
            "occurred_at": "2026-09-01T10:00:00+08:00", "version": 1, "summary": "s",
            "payload": {
                "license_id": "L-1", "artwork_ids": ["AW-1"], "licensor_candidate_id": "C-1",
                "rights": {"reproduction": True, "adaptation": "NONE"},
                "use_class": "COMMERCIAL", "territories": ["CN"], "channels": ["MUSEUM_SHOP"],
                "term": {"start_date": "2026-11-01", "end_date": "2026-10-01"},
                "term_basis": "EXHIBITION_WINDOW",
                "royalty": {"rate": 0.1, "basis": "PERCENT_OF_REVENUE"},
            },
        }
        errors = validate_event(event)
        self.assertIn("payload.term 的 start_date 晚于 end_date", errors)
        self.assertIn("term_basis=EXHIBITION_WINDOW 时必须提供 exhibition_id", errors)


class EventStoreTest(unittest.TestCase):
    def _event(self, version, eid=None, aggregate="a1"):
        return {
            "event_id": eid or f"e{version}", "event_type": "RIGHT_CLAIMED",
            "aggregate_type": "artwork_right", "aggregate_id": aggregate,
            "occurred_at": "2026-09-01T10:00:00+08:00", "version": version, "summary": "s",
        }

    def test_versions_must_be_sequential(self) -> None:
        store = EventStore()
        store.append(self._event(1))
        with self.assertRaises(EventStoreError):
            store.append(self._event(3))  # 跳号
        store.append(self._event(2))
        with self.assertRaises(EventStoreError):
            store.append(self._event(2))  # 重放
        # 不同聚合各自从 1 开始
        store.append(self._event(1, eid="e-a2-1", aggregate="a2"))

    def test_duplicate_event_id_rejected(self) -> None:
        store = EventStore()
        store.append(self._event(1, eid="dup"))
        with self.assertRaises(EventStoreError):
            store.append(self._event(1, eid="dup", aggregate="a2"))

    def test_appended_event_is_defensively_copied(self) -> None:
        """业务更正只能追加后继事件；调用方事后改动入参不会污染已存储记录。"""
        store = EventStore()
        event = self._event(1)
        store.append(event)
        event["summary"] = "被篡改"
        event["version"] = 99
        stored = store.events_for("artwork_right", "a1")[0]
        self.assertEqual(stored["summary"], "s")
        self.assertEqual(stored["version"], 1)


class ScenarioRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.outcomes, cls.checkpoints = build_scenario()
        cls.view = RightsProjection(cls.store)

    def test_acquisition_is_not_copyright(self) -> None:
        # 入藏登记的四件作品中，只有一件带转让契书；吴隐之作品为家属持有
        aw001 = self.view.artworks["AW-001"]
        self.assertEqual(aw001["copyright_status"], "ESTATE_HELD")
        self.assertIsNone(aw001["copyright_deed_ref"])
        # 没有已核验候选就没有任何有效授权
        self.assertEqual(self.view.effective_licenses("AW-999", DT("2026-12-01T00:00:00+08:00")), [])

    def test_parallel_claims_independent_verification(self) -> None:
        # 出版社候选被驳回，长子候选仍为 VERIFIED，授权有效
        self.assertEqual(self.view.candidates["C-PUBLISHER-X"].status, "REJECTED")
        self.assertEqual(self.view.candidates["C-WU-SON"].status, "VERIFIED")
        hits = self.view.effective_licenses("AW-001", DT("2026-12-01T00:00:00+08:00"))
        self.assertEqual([lic.license_id for lic, _ in hits], ["L-AW001-ESTATE"])

    def test_joint_work_requires_all_coowners(self) -> None:
        # 10 月 9 日：林述已授权、周白未核验，组合作品无交集
        g = self.checkpoints["2026-10-09_初次审批"]
        self.assertFalse(any(c["can_produce"] for c in g["channels"].values()))
        # 周白授权后（10 月 22 日）才放行
        sku = self.view.skus["SKU-MUG-01"]
        self.assertIsNotNone(sku.clearance)
        self.assertEqual(sku.clearance["approver"], "法务-赵敏")

    def test_nonprofit_and_commercial_isolation(self) -> None:
        # 公益海报走 PRESS_KIT，商业徽章审批必须被驳回
        rejected = self.view.skus["SKU-BADGE-X"].clearance_rejected
        self.assertIsNotNone(rejected)
        self.assertTrue(any("授权交集" in r or "授权" in r for r in rejected["reasons"]))
        poster = self.view.skus["SKU-POSTER-NP"]
        self.assertEqual(poster.use_class, "NONPROFIT_PROMO")
        self.assertIsNotNone(poster.clearance)

    def test_sample_rejection_blocks_until_new_round_approved(self) -> None:
        rounds = {s["round_no"]: s["status"] for s_id, s in self.view.samples.items()
                  if s["design_id"] == "D-MUG-01" for s_id in [s_id]}
        # 马克杯两轮打样：第一轮 REJECTED，第二轮 APPROVED
        statuses = sorted({(s["round_no"], s["status"]) for s in self.view.samples.values()
                           if s["design_id"] == "D-MUG-01"})
        self.assertIn((1, "REJECTED"), statuses)
        self.assertIn((2, "APPROVED"), statuses)
        self.assertIn("restock_before_resample", self.outcomes)

    def test_material_substitution_requires_resample(self) -> None:
        self.assertIn("换料", self.outcomes["restock_before_resample"])

    def test_quota_headroom(self) -> None:
        # 丝巾配额 3000：排产 2500（1000+600+600+300），余量 500
        hot = self.checkpoints["2026-11-20_热销期"]
        self.assertEqual(hot["channels"]["MUSEUM_SHOP"]["headroom_units"], 2000)
        self.assertIn("超出授权交付余量 800", self.outcomes["restock_over_headroom"])
        # 承诺占用配额：第二次仅 300 件的承诺成功
        commits = [c for c in self.view.restock_commits if c["sku"] == "SKU-SCARF-02"]
        quantities = sorted(c["quantity"] for c in commits)
        self.assertEqual(quantities, [300, 600])

    def test_dispute_blocks_but_parallel_verification_continues(self) -> None:
        dispute_gate = self.checkpoints["2026-12-12_争议期"]
        for d in dispute_gate["channels"].values():
            self.assertFalse(d["can_produce"])
            self.assertFalse(d["can_sell"])
        self.assertIn("sale_during_dispute", self.outcomes)
        # 争议解决后重新上架，销售恢复
        self.assertEqual(self.view.disputes["DISP-001"]["status"], "RESOLVED")

    def test_license_narrow_only_affected_channel(self) -> None:
        g = self.checkpoints["2027-01-06_线上收窄"]
        self.assertTrue(g["channels"]["MUSEUM_SHOP"]["can_produce"])
        self.assertTrue(g["channels"]["POPUP_STORE"]["can_produce"])
        online = g["channels"]["ONLINE_MALL"]
        self.assertFalse(online["can_produce"])           # 线上不得新生产
        self.assertTrue(online["can_sell"])               # 售罄期内可卖库存
        self.assertTrue(online["sell_through_only"])
        self.assertTrue(online["on_hand_units"] > 0)

    def test_withdraw_blocks_production_but_keeps_sold_batches(self) -> None:
        g = self.checkpoints["2027-01-12_撤回售罄"]
        for d in g["channels"].values():
            self.assertFalse(d["can_produce"])
            self.assertTrue(d["can_sell"])
            self.assertTrue(d["sell_through_only"])
        self.assertIn("produce_after_withdraw", self.outcomes)
        # 已放行批次的快照仍指向被撤回授权的 v1（撤回事件使授权聚合变为 v2）
        trace = self.view.trace_sku("SKU-MUG-01")
        zhou = next(s for s in trace["clearance"]["snapshot"] if s["license_id"] == "L-AW002-ZHOU")
        self.assertEqual(zhou["license_version"], 1)
        self.assertEqual(zhou["license_current_version"], 2)
        self.assertTrue(zhou["license_withdrawn"])
        # 批次 1001 在撤回后仍售出 30 件，结算依据不变
        legality = self.view.batch_legality("B-MUG-1001")
        self.assertGreaterEqual(legality["sold"], 210)
        self.assertEqual({t["basis"] for t in legality["royalty_terms"]}, {"PERCENT_OF_REVENUE"})

    def test_sales_blocked_after_sell_through_ends(self) -> None:
        g = self.checkpoints["2027-02-20_售罄结束"]
        for d in g["channels"].values():
            self.assertFalse(d["can_sell"])
        self.assertIn("mug_sale_after_sellthrough", self.outcomes)
        self.assertIn("scarf_online_after_sellthrough", self.outcomes)

    def test_early_close_hits_exhibition_window_only(self) -> None:
        at = DT("2027-01-26T12:00:00+08:00")
        poster = self.view.gate("SKU-POSTER-NP", at)
        self.assertFalse(poster["channels"]["PRESS_KIT"]["can_sell"])
        scarf = self.view.gate("SKU-SCARF-02", at)
        # 丝巾授权为 EXPLICIT，展期提前闭幕不影响其线下与快闪渠道
        self.assertTrue(scarf["channels"]["MUSEUM_SHOP"]["can_sell"])
        self.assertTrue(scarf["channels"]["POPUP_STORE"]["can_sell"])

    def test_delist_trace_chain(self) -> None:
        trace = self.view.trace_sku("SKU-MUG-01")
        reasons = {(tuple(d["channels"]), d["reason_code"], d["trigger_event_id"])
                   for d in trace["delistings"]}
        self.assertIn((("MUSEUM_SHOP", "ONLINE_MALL"), "DISPUTE_OPENED", "ev-disp-open"), reasons)
        self.assertIn((("MUSEUM_SHOP", "ONLINE_MALL"), "RIGHT_WITHDRAWN",
                       "ev-lic-zhou-withdraw"), reasons)
        for d in trace["delistings"]:
            self.assertIn(d["inventory_disposition"], {"HOLD", "SELL_THROUGH"})
            self.assertTrue(d["approver"])

    def test_batch_cannot_oversell(self) -> None:
        svc = CommandService(self.store)
        with self.assertRaises(CommandError):
            svc.record_sale("oversale-1", "ST-X", "SKU-MUG-01", "B-MUG-1002",
                            "ONLINE_MALL", 10_000, 89.0, "2026-11-25T10:00:00+08:00")

    def test_royalty_settled_against_frozen_terms(self) -> None:
        # 撤权后的售罄期销售仍按批次快照计算分成（120+40 线上批次 ×89×8% 给家属、6% 给林述等）
        jan = next(s for s in self.view.settlements if s["statement_id"] == "STMT-2027-01")
        licenses = {line["license_id"] for line in jan["lines"]}
        self.assertIn("L-AW002-ZHOU", licenses)
        zhou_line = next(line for line in jan["lines"] if line["license_id"] == "L-AW002-ZHOU")
        # 周白分成 = 林述/周白各 6%，马克杯线上 40 件（撤后售罄）+ 前期已结 11 月不含
        self.assertGreater(zhou_line["amount"], 0)
        self.assertEqual(zhou_line["payee_candidate_id"], "C-ZHOU")


class SchemaParityTest(unittest.TestCase):
    """Python 校验器与 JSON Schema 中声明的事件目录保持一致。"""

    def test_event_catalog_parity(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]["event_type"]["enum"]),
                         set(__import__("src.validator", fromlist=["EVENT_TYPES"]).EVENT_TYPES))


if __name__ == "__main__":
    unittest.main()
