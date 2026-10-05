"""三个月特展的端到端场景构造。

用命令服务产出一条完整事件流，覆盖：入藏≠著作权、合作作品并行核验、
组合 SKU 许可交集、公益/商业隔离、样品驳回、供应商换料、限量补货与配额、
展期改期、争议立案/解决、授权收窄与撤回联动下架、售罄期既有库存销售、
售罄期结束阻断、已售批次快照与分成结算。

build_scenario 返回 (store, outcomes)：outcomes 记录每一步的预期内拒绝，
供测试断言；checkpoints 是若干时点的门禁快照。
"""

from __future__ import annotations

from datetime import datetime

from .commands import CommandError, CommandService
from .domain import RightsProjection
from .event_store import EventStore


def build_scenario() -> tuple[EventStore, dict, dict]:
    store = EventStore()
    svc = CommandService(store)
    outcomes: dict[str, str] = {}

    def expect_rejected(key: str, fn):
        try:
            fn()
        except CommandError as exc:
            outcomes[key] = "；".join(exc.reasons)

    # ---------- 展期定档与改期 ----------
    svc.schedule_exhibition(
        "ev-ex-001", "EX-2026-LINE", "线描之间——二十世纪纸上特展",
        "2026-11-01", "2027-01-31", "2026-09-01T10:00:00+08:00", hall="三楼临展厅",
    )
    # 开幕前官宣延期：展期窗口整体后移
    svc.reschedule_exhibition(
        "ev-ex-002", "EX-2026-LINE",
        {"start_date": "2026-11-01", "end_date": "2027-01-31"},
        {"start_date": "2026-11-15", "end_date": "2027-02-28"},
        "展品借展协调，开幕推迟两周、展期顺延", "2026-09-12T09:00:00+08:00",
    )

    # ---------- 作品入藏（均不当然取得著作权） ----------
    svc.acquire_artwork(
        "ev-aw001-001", "AW-001", "春山线稿", "2019-06-01", "ESTATE_HELD",
        "2026-09-05T10:00:00+08:00", artist_display="吴隐之", acquisition_mode="DONATION",
    )
    svc.acquire_artwork(
        "ev-aw002-001", "AW-002", "双姝合作稿", "2021-03-01", "JOINTLY_OWNED",
        "2026-09-05T10:05:00+08:00", artist_display="林述、周白", acquisition_mode="COMMISSION",
    )
    svc.acquire_artwork(
        "ev-aw003-001", "AW-003", "馆藏肖像", "2018-01-10", "TRANSFERRED_WITH_DEED",
        "2026-09-05T10:10:00+08:00", artist_display="陈岱", acquisition_mode="PURCHASE",
        copyright_deed_ref="DEED-2018-014",
    )
    svc.acquire_artwork(
        "ev-aw004-001", "AW-004", "无题宣传稿", "2025-08-01", "RETAINED_BY_ARTIST",
        "2026-09-05T10:15:00+08:00", artist_display="何未", acquisition_mode="COLLECTION",
    )

    # ---------- 权属候选并行登记 ----------
    svc.claim_right(
        "ev-cl-wuson", "AW-001", "C-WU-SON", "吴家长子", "HEIR",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        ["NOTARY-2026-2201", "HOUSEHOLD-PROOF"], "2026-09-08T11:00:00+08:00",
    )
    svc.claim_right(
        "ev-cl-pub", "AW-001", "C-PUBLISHER-X", "某某出版社", "ASSIGNEE",
        ["REPRODUCTION", "DISTRIBUTION"],
        ["CONTRACT-1998-ANTHOLOGY"], "2026-09-09T11:00:00+08:00",
    )
    svc.claim_right(
        "ev-cl-lin", "AW-002", "C-LIN", "林述", "JOINT_ARTIST",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        ["ID-LIN", "COMMISSION-CONTRACT-2021"], "2026-09-10T09:30:00+08:00", share_pct=50,
    )
    svc.claim_right(
        "ev-cl-zhou", "AW-002", "C-ZHOU", "周白", "JOINT_ARTIST",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        ["ID-ZHOU"], "2026-09-10T09:35:00+08:00", share_pct=50,
    )
    svc.claim_right(
        "ev-cl-museum", "AW-003", "C-MUSEUM-RIGHTS", "浦东美术馆资产管理部", "ASSIGNEE",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        ["DEED-2018-014"], "2026-09-10T10:00:00+08:00",
    )
    svc.claim_right(
        "ev-cl-he", "AW-004", "C-HEWEI", "何未", "ARTIST",
        ["REPRODUCTION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        ["ID-HEWEI", "ARTIST-LETTER-2026"], "2026-09-10T10:30:00+08:00",
    )

    # ---------- 并行核验：长子通过；出版社候选被驳回（不影响长子授权） ----------
    svc.verify_claim(
        "ev-vf-wuson", "C-WU-SON", "法务-赵敏",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        "继承公证书 NOTARY-2026-2201 权属链条完整", "2026-09-15T14:00:00+08:00",
    )
    svc.reject_claim(
        "ev-rj-pub", "C-PUBLISHER-X", "法务-赵敏",
        "1998 年合同仅覆盖文集内页印制，未约定衍生品与信息网络传播", "2026-09-25T14:00:00+08:00",
    )
    svc.verify_claim(
        "ev-vf-lin", "C-LIN", "法务-赵敏",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        "委托创作合同与身份证明一致", "2026-09-16T14:00:00+08:00", share_pct=50,
    )
    # 周白在海外，材料晚到，10 月下旬才完成核验
    svc.verify_claim(
        "ev-vf-zhou", "C-ZHOU", "法务-赵敏",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        "本人到场补充身份证明，共同创作人身份确认", "2026-10-20T16:00:00+08:00", share_pct=50,
    )
    svc.verify_claim(
        "ev-vf-museum", "C-MUSEUM-RIGHTS", "法务-赵敏",
        ["REPRODUCTION", "ADAPTATION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        "著作权转让契书 DEED-2018-014 已归档", "2026-09-16T15:00:00+08:00",
    )
    svc.verify_claim(
        "ev-vf-he", "C-HEWEI", "法务-赵敏",
        ["REPRODUCTION", "DISTRIBUTION", "INFORMATION_NETWORK"],
        "艺术家本人书面声明", "2026-09-16T15:30:00+08:00",
    )

    # ---------- 授权 ----------
    svc.grant_license(
        "ev-lic-aw001", "L-AW001-ESTATE", ["AW-001"], "C-WU-SON",
        {"reproduction": True, "adaptation": "MODIFY"},
        "COMMERCIAL", ["CN"], ["MUSEUM_SHOP", "ONLINE_MALL", "POPUP_STORE"],
        ("2026-11-01", "2027-03-31"), "EXHIBITION_WINDOW",
        {"rate": 0.08, "basis": "PERCENT_OF_REVENUE"},
        "2026-09-20T10:00:00+08:00", exhibition_id="EX-2026-LINE",
        max_units=5000, sell_through_days=15,
    )
    svc.grant_license(
        "ev-lic-lin", "L-AW002-LIN", ["AW-002"], "C-LIN",
        {"reproduction": True, "adaptation": "MODIFY"},
        "COMMERCIAL", ["CN"], ["MUSEUM_SHOP", "ONLINE_MALL", "POPUP_STORE"],
        ("2026-11-01", "2027-03-31"), "EXHIBITION_WINDOW",
        {"rate": 0.06, "basis": "PERCENT_OF_REVENUE"},
        "2026-09-22T10:00:00+08:00", exhibition_id="EX-2026-LINE",
        max_units=5000, sell_through_days=15,
    )
    svc.grant_license(
        "ev-lic-zhou", "L-AW002-ZHOU", ["AW-002"], "C-ZHOU",
        {"reproduction": True, "adaptation": "MODIFY"},
        "COMMERCIAL", ["CN"], ["MUSEUM_SHOP", "ONLINE_MALL", "POPUP_STORE"],
        ("2026-11-01", "2027-03-31"), "EXHIBITION_WINDOW",
        {"rate": 0.06, "basis": "PERCENT_OF_REVENUE"},
        "2026-10-21T10:00:00+08:00", exhibition_id="EX-2026-LINE",
        max_units=5000, sell_through_days=15,
    )
    svc.grant_license(
        "ev-lic-aw003", "L-AW003-MUSEUM", ["AW-003"], "C-MUSEUM-RIGHTS",
        {"reproduction": True, "adaptation": "DERIVATIVE_FULL"},
        "COMMERCIAL", ["CN"], ["MUSEUM_SHOP", "ONLINE_MALL", "POPUP_STORE"],
        ("2026-11-15", "2027-03-31"), "EXPLICIT",
        {"rate": 0.10, "basis": "PERCENT_OF_REVENUE"},
        "2026-09-23T10:00:00+08:00", max_units=3000, sell_through_days=15,
    )
    # 公益宣传授权：仅限媒体资料包，不得商业售卖
    svc.grant_license(
        "ev-lic-aw004-np", "L-AW004-NONPROFIT", ["AW-004"], "C-HEWEI",
        {"reproduction": True, "adaptation": "NONE"},
        "NONPROFIT_PROMO", ["CN"], ["PRESS_KIT"],
        ("2026-11-01", "2027-03-31"), "EXHIBITION_WINDOW",
        {"rate": 0, "basis": "LUMP_SUM"},
        "2026-09-24T10:00:00+08:00", exhibition_id="EX-2026-LINE",
    )

    # ---------- 设计提案 ----------
    svc.submit_design(
        "ev-dg-mug", "D-MUG-01", ["AW-001", "AW-002"], "COMMERCIAL",
        "承制设计公司-拓物", "STYLIZED_REFERENCE", "SPEC-D-MUG-01-v3",
        "2026-09-28T10:00:00+08:00",
    )
    svc.submit_design(
        "ev-dg-scarf", "D-SCARF-02", ["AW-003"], "COMMERCIAL",
        "承制设计公司-拓物", "MODIFY", "SPEC-D-SCARF-02-v1",
        "2026-09-28T10:10:00+08:00",
    )
    svc.submit_design(
        "ev-dg-poster", "D-POSTER-NP", ["AW-004"], "NONPROFIT_PROMO",
        "馆方视觉组", "NONE", "SPEC-D-POSTER-NP-v1",
        "2026-09-28T10:20:00+08:00",
    )
    svc.submit_design(
        "ev-dg-badge", "D-BADGE-X", ["AW-004"], "COMMERCIAL",
        "外部设计公司-潮玩社", "STYLIZED_REFERENCE", "SPEC-D-BADGE-X-v1",
        "2026-09-29T10:00:00+08:00",
    )
    svc.approve_design("ev-dap-mug", "D-MUG-01", "策展-李闻", "2026-09-30T15:00:00+08:00")
    svc.approve_design("ev-dap-scarf", "D-SCARF-02", "策展-李闻", "2026-09-30T15:05:00+08:00")
    svc.approve_design("ev-dap-poster", "D-POSTER-NP", "策展-李闻", "2026-09-30T15:10:00+08:00")
    svc.approve_design("ev-dap-badge", "D-BADGE-X", "策展-李闻", "2026-09-30T15:15:00+08:00",
                       notes="设计本身通过，但能否商用取决于授权")

    # ---------- 打样：马克杯第一轮驳回，第二轮通过 ----------
    svc.submit_sample(
        "ev-sp-mug-r1", "S-MUG-R1", "D-MUG-01", 1, "供应商-德瓷",
        "MAT-CERAMIC-A", "2026-10-02T09:00:00+08:00",
    )
    svc.reject_sample(
        "ev-sp-mug-r1-no", "S-MUG-R1", "策展-李闻", "线稿套色偏移，超出艺术家家属确认样",
        "2026-10-05T17:00:00+08:00",
    )
    svc.submit_sample(
        "ev-sp-mug-r2", "S-MUG-R2", "D-MUG-01", 2, "供应商-德瓷",
        "MAT-CERAMIC-A", "2026-10-10T09:00:00+08:00",
    )
    svc.submit_sample(
        "ev-sp-scarf-r1", "S-SCARF-R1", "D-SCARF-02", 1, "供应商-锦绣",
        "MAT-SILK-12", "2026-10-03T09:00:00+08:00",
    )
    svc.submit_sample(
        "ev-sp-poster-r1", "S-POSTER-R1", "D-POSTER-NP", 1, "供应商-印务一厂",
        "MAT-PAPER-200", "2026-10-03T09:30:00+08:00",
    )
    svc.approve_sample("ev-sp-mug-r2-ok", "S-MUG-R2", "策展-李闻", "2026-10-12T17:00:00+08:00")
    svc.approve_sample("ev-sp-scarf-r1-ok", "S-SCARF-R1", "策展-李闻", "2026-10-06T17:00:00+08:00")
    svc.approve_sample("ev-sp-poster-r1-ok", "S-POSTER-R1", "策展-李闻", "2026-10-06T17:30:00+08:00")
    # 徽章也打样通过，但授权用途不支持商业
    svc.submit_sample(
        "ev-sp-badge-r1", "S-BADGE-R1", "D-BADGE-X", 1, "供应商-鑫泰",
        "MAT-METAL-03", "2026-10-04T09:00:00+08:00",
    )
    svc.approve_sample("ev-sp-badge-r1-ok", "S-BADGE-R1", "策展-李闻", "2026-10-07T10:00:00+08:00")

    # ---------- SKU 登记 ----------
    svc.register_sku("ev-sku-mug", "SKU-MUG-01", "D-MUG-01",
                     ["MUSEUM_SHOP", "ONLINE_MALL"], 89.0, "2026-10-08T09:00:00+08:00")
    svc.register_sku("ev-sku-scarf", "SKU-SCARF-02", "D-SCARF-02",
                     ["MUSEUM_SHOP", "ONLINE_MALL", "POPUP_STORE"], 199.0,
                     "2026-10-08T09:05:00+08:00")
    svc.register_sku("ev-sku-poster", "SKU-POSTER-NP", "D-POSTER-NP",
                     ["PRESS_KIT"], 0.0, "2026-10-08T09:10:00+08:00")
    svc.register_sku("ev-sku-badge", "SKU-BADGE-X", "D-BADGE-X",
                     ["MUSEUM_SHOP"], 39.0, "2026-10-08T09:15:00+08:00")

    # ---------- 开售门禁 ----------
    # 周白未授权 + 第一轮样品被驳回：组合马克杯此时必须被拦
    svc.request_clearance("ev-clr-mug-no1", "SKU-MUG-01", "法务-赵敏",
                          "2026-10-08T18:00:00+08:00")
    # 公益授权不得用于商业徽章
    svc.request_clearance("ev-clr-badge-no", "SKU-BADGE-X", "法务-赵敏",
                          "2026-10-09T10:00:00+08:00")
    # 周白授权完成、第二轮样品通过后放行
    svc.request_clearance("ev-clr-mug-ok", "SKU-MUG-01", "法务-赵敏",
                          "2026-10-22T10:00:00+08:00", notes="双权利人授权与样品均齐备")
    svc.request_clearance("ev-clr-scarf-ok", "SKU-SCARF-02", "法务-赵敏",
                          "2026-10-09T11:00:00+08:00")
    svc.request_clearance("ev-clr-poster-ok", "SKU-POSTER-NP", "法务-赵敏",
                          "2026-10-09T11:30:00+08:00")

    # ---------- 排产（改期后开幕日 11-15 之后方在授权生产窗口内） ----------
    svc.release_batch("ev-batch-mug-shop", "B-MUG-1001", "SKU-MUG-01", 800,
                      "MAT-CERAMIC-A", "2026-11-16T10:00:00+08:00", "MUSEUM_SHOP")
    svc.release_batch("ev-batch-mug-online", "B-MUG-1002", "SKU-MUG-01", 500,
                      "MAT-CERAMIC-A", "2026-11-17T10:00:00+08:00", "ONLINE_MALL")
    svc.release_batch("ev-batch-scarf-shop", "B-SCARF-2001", "SKU-SCARF-02", 1000,
                      "MAT-SILK-12", "2026-11-16T10:30:00+08:00", "MUSEUM_SHOP")
    # 公益海报仅在媒体资料包渠道发放，单价 0、无分成
    svc.release_batch("ev-batch-poster", "B-POSTER-NP-1", "SKU-POSTER-NP", 300,
                      "MAT-PAPER-200", "2026-11-16T11:00:00+08:00", "PRESS_KIT")
    svc.record_sale("ev-sale-poster-1", "ST-SALE-0000", "SKU-POSTER-NP", "B-POSTER-NP-1",
                    "PRESS_KIT", 50, 0.0, "2026-11-18T10:00:00+08:00")

    # ---------- 正常销售与首月结算 ----------
    svc.record_sale("ev-sale-01", "ST-SALE-0001", "SKU-MUG-01", "B-MUG-1001",
                    "MUSEUM_SHOP", 120, 89.0, "2026-11-20T15:00:00+08:00")
    svc.record_sale("ev-sale-02", "ST-SALE-0002", "SKU-MUG-01", "B-MUG-1002",
                    "ONLINE_MALL", 80, 89.0, "2026-11-22T15:00:00+08:00")
    svc.record_sale("ev-sale-03", "ST-SALE-0003", "SKU-SCARF-02", "B-SCARF-2001",
                    "MUSEUM_SHOP", 200, 199.0, "2026-11-23T15:00:00+08:00")

    # ---------- 供应商换料：未重新打样前补货被拦 ----------
    svc.substitute_material(
        "ev-mat-scarf", "SKU-SCARF-02", "MAT-SILK-12", "MAT-SILK-15",
        "供应商-锦绣", "2026-12-05T09:00:00+08:00",
    )
    expect_rejected("restock_before_resample", lambda: svc.commit_restock(
        "ev-restock-no1", "SKU-SCARF-02", 600, "B-SCARF-PLAN",
        "2026-12-08T10:00:00+08:00", "POPUP_STORE",
    ))
    svc.submit_sample("ev-sp-scarf-r2", "S-SCARF-R2", "D-SCARF-02", 2, "供应商-锦绣",
                      "MAT-SILK-15", "2026-12-12T09:00:00+08:00")
    svc.approve_sample("ev-sp-scarf-r2-ok", "S-SCARF-R2", "策展-李闻",
                       "2026-12-15T17:00:00+08:00")
    svc.commit_restock("ev-restock-popup", "SKU-SCARF-02", 600, "B-SCARF-2002",
                       "2026-12-16T10:00:00+08:00", "POPUP_STORE")
    svc.release_batch("ev-batch-scarf-popup", "B-SCARF-2002", "SKU-SCARF-02", 600,
                      "MAT-SILK-15", "2026-12-18T10:00:00+08:00", "POPUP_STORE")
    svc.release_batch("ev-batch-scarf-online", "B-SCARF-2003", "SKU-SCARF-02", 600,
                      "MAT-SILK-15", "2026-12-20T10:00:00+08:00", "ONLINE_MALL")
    # 超配额补货被拦：已排产 2200 / 配额 3000，余量 800
    expect_rejected("restock_over_headroom", lambda: svc.commit_restock(
        "ev-restock-no2", "SKU-SCARF-02", 3000, "B-SCARF-PLAN-X",
        "2026-12-22T10:00:00+08:00", "MUSEUM_SHOP",
    ))
    svc.commit_restock("ev-restock-shop2", "SKU-SCARF-02", 300, "B-SCARF-2004",
                       "2026-12-23T10:00:00+08:00", "MUSEUM_SHOP")
    svc.release_batch("ev-batch-scarf-shop2", "B-SCARF-2004", "SKU-SCARF-02", 300,
                      "MAT-SILK-15", "2027-01-02T10:00:00+08:00", "MUSEUM_SHOP")

    # ---------- 争议立案/解决：期间马克杯下架，其他候选核验不受影响 ----------
    svc.open_dispute_and_delist(
        "ev-disp-open", "AW-001", "DISP-001", "出版社主张衍生品权属",
        "某某出版社-代理律师", "2026-12-10T09:00:00+08:00",
        approver="法务-赵敏", disposition="HOLD",
        candidate_ids=["C-PUBLISHER-X", "C-WU-SON"],
    )
    expect_rejected("sale_during_dispute", lambda: svc.record_sale(
        "ev-sale-no-disp", "ST-SALE-9001", "SKU-MUG-01", "B-MUG-1001",
        "MUSEUM_SHOP", 10, 89.0, "2026-12-12T12:00:00+08:00",
    ))
    svc.resolve_dispute("ev-disp-resolve", "DISP-001", "UNENCUMBERED",
                        "法务-赵敏", "2026-12-28T15:00:00+08:00")
    svc.relist("ev-relist-mug", "SKU-MUG-01", ["MUSEUM_SHOP", "ONLINE_MALL"],
               "争议解决，长子权属确认无瑕疵", "法务-赵敏", "2026-12-29T10:00:00+08:00")
    svc.record_sale("ev-sale-04", "ST-SALE-0004", "SKU-MUG-01", "B-MUG-1001",
                    "MUSEUM_SHOP", 60, 89.0, "2027-01-03T15:00:00+08:00")
    svc.record_sale("ev-sale-05", "ST-SALE-0005", "SKU-SCARF-02", "B-SCARF-2003",
                    "ONLINE_MALL", 100, 199.0, "2027-01-03T16:00:00+08:00")
    svc.record_sale("ev-sale-06", "ST-SALE-0006", "SKU-SCARF-02", "B-SCARF-2002",
                    "POPUP_STORE", 30, 199.0, "2027-01-04T16:00:00+08:00")

    # ---------- 授权收窄：线上渠道被权利方收回，仅线上 SKU 渠道下架 ----------
    svc.narrow_license_and_delist(
        "ev-lic-aw003-narrow", "L-AW003-MUSEUM",
        {"remove_channels": ["ONLINE_MALL"]},
        "2027-01-05T00:00:00+08:00", approver="法务-赵敏",
        inventory_disposition="SELL_THROUGH", sell_through_until="2027-02-15",
    )
    # 售罄期内既有线上库存仍可销售，结算依据不变
    svc.record_sale("ev-sale-07", "ST-SALE-0007", "SKU-SCARF-02", "B-SCARF-2003",
                    "ONLINE_MALL", 50, 199.0, "2027-01-08T12:00:00+08:00")

    # ---------- 家属撤回周白授权：组合马克杯全渠道下架，已售批次不受影响 ----------
    svc.withdraw_license_and_delist(
        "ev-lic-zhou-withdraw", "L-AW002-ZHOU",
        "艺术家家属对衍生设计风格提出异议，撤回全部商用授权",
        "2027-01-10T00:00:00+08:00", approver="法务-赵敏",
        inventory_disposition="SELL_THROUGH", sell_through_until="2027-02-15",
    )
    # 撤回后不得新生产
    expect_rejected("produce_after_withdraw", lambda: svc.release_batch(
        "ev-batch-no-post", "B-MUG-9999", "SKU-MUG-01", 100,
        "MAT-CERAMIC-A", "2027-01-11T10:00:00+08:00", "MUSEUM_SHOP",
    ))
    # 售罄期内既有库存合法销售
    svc.record_sale("ev-sale-08", "ST-SALE-0008", "SKU-MUG-01", "B-MUG-1002",
                    "ONLINE_MALL", 40, 89.0, "2027-01-12T12:00:00+08:00")
    svc.record_sale("ev-sale-09", "ST-SALE-0009", "SKU-MUG-01", "B-MUG-1001",
                    "MUSEUM_SHOP", 30, 89.0, "2027-01-12T13:00:00+08:00")
    svc.record_sale("ev-sale-10", "ST-SALE-0010", "SKU-SCARF-02", "B-SCARF-2002",
                    "POPUP_STORE", 20, 199.0, "2027-01-12T14:00:00+08:00")

    # ---------- 提前撤展：EXHIBITION_WINDOW 授权的公益海报立即下架；EXPLICIT 丝巾不受影响 ----------
    svc.reschedule_exhibition_and_delist(
        "ev-ex-003", "EX-2026-LINE",
        {"start_date": "2026-11-15", "end_date": "2027-02-28"},
        {"start_date": "2026-11-15", "end_date": "2027-01-20"},
        "场馆运维安排，特展提前闭幕", "2027-01-25T10:00:00+08:00",
        approver="运营总监-孙恪", disposition="HOLD",
    )

    # ---------- 售罄期结束：一切销售阻断 ----------
    expect_rejected("mug_sale_after_sellthrough", lambda: svc.record_sale(
        "ev-sale-no-1", "ST-SALE-9002", "SKU-MUG-01", "B-MUG-1001",
        "MUSEUM_SHOP", 1, 89.0, "2027-02-20T12:00:00+08:00",
    ))
    expect_rejected("scarf_online_after_sellthrough", lambda: svc.record_sale(
        "ev-sale-no-2", "ST-SALE-9003", "SKU-SCARF-02", "B-SCARF-2003",
        "ONLINE_MALL", 1, 199.0, "2027-02-20T12:00:00+08:00",
    ))

    # ---------- 分成结算（按批次冻结条款，售罄期售出同样依据快照） ----------
    view = RightsProjection(store)
    payee = {lid: lic.licensor_candidate_id for lid, lic in view.licenses.items()}

    def settle_period(statement_id, start, end, settled_at):
        totals: dict[str, float] = {}
        for sale in view.sales:
            day = sale["sold_at"][:10]
            if start <= day <= end:
                for line in sale["royalty_due"]:
                    totals[line["license_id"]] = round(
                        totals.get(line["license_id"], 0.0) + line["amount"], 2,
                    )
        lines = [
            {"license_id": lid, "payee_candidate_id": payee[lid], "amount": amount}
            for lid, amount in sorted(totals.items())
        ]
        return svc.settle(statement_id, statement_id, start, end, settled_at, lines)

    settle_period("STMT-2026-11", "2026-11-01", "2026-11-30", "2026-12-05T10:00:00+08:00")
    settle_period("STMT-2026-12", "2026-12-01", "2026-12-31", "2027-01-05T10:00:00+08:00")
    settle_period("STMT-2027-01", "2027-01-01", "2027-02-15", "2027-03-05T10:00:00+08:00")

    # ---------- 门禁检查点 ----------
    checkpoints = {
        "2026-10-09_初次审批": RightsProjection(store).gate(
            "SKU-MUG-01", datetime.fromisoformat("2026-10-09T12:00:00+08:00")),
        "2026-11-20_热销期": RightsProjection(store).gate(
            "SKU-SCARF-02", datetime.fromisoformat("2026-11-20T12:00:00+08:00")),
        "2026-12-12_争议期": RightsProjection(store).gate(
            "SKU-MUG-01", datetime.fromisoformat("2026-12-12T12:00:00+08:00")),
        "2027-01-06_线上收窄": RightsProjection(store).gate(
            "SKU-SCARF-02", datetime.fromisoformat("2027-01-06T12:00:00+08:00")),
        "2027-01-12_撤回售罄": RightsProjection(store).gate(
            "SKU-MUG-01", datetime.fromisoformat("2027-01-12T12:00:00+08:00")),
        "2027-02-20_售罄结束": RightsProjection(store).gate(
            "SKU-MUG-01", datetime.fromisoformat("2027-02-20T12:00:00+08:00")),
    }

    return store, outcomes, checkpoints


if __name__ == "__main__":
    import json
    from pathlib import Path

    store_, outcomes_, checkpoints = build_scenario()
    out_dir = Path(__file__).resolve().parents[1] / "data"
    (out_dir / "scenario_events.json").write_text(
        json.dumps(store_.all_events(), ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"事件数：{len(store_.all_events())}，已写入 data/scenario_events.json")
    print(f"预期内拒绝：{list(outcomes_)}")
