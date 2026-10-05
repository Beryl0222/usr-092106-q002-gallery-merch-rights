# 美术馆文创授权台

浦东美术馆三个月临展的**展创协作与权利后端**：登记作品权属候选、复制/改编范围、
地域渠道、展期、设计提案、打样批次、生产承诺、销售与分成，并在任意时点回答
开售门禁三问——**此刻能否生产、在哪些渠道销售、还有多少交付余量**。

## 核心不变量

1. **基础事件信封不可丢失**：所有记录共用 `event_id / event_type / aggregate_type /
   aggregate_id / occurred_at / version / summary`；每个聚合的 `version` 从 1 起逐 1
   递增，事件一经接收不可原地改写，更正只追加后继版本。
2. **入藏 ≠ 著作权**：`ARTWORK_ACQUIRED` 只记录物权与著作权状态断言（`copyright_status`），
   除非存在书面转让契书，否则不视为馆方取得著作权；商用必须存在已核验的权属候选。
3. **并行核验**：同一作品可有多个权属候选（艺术家、家属、继承人、出版社、联合创作人），
   一个候选被驳回不影响其他候选；争议立案期间该作品相关 SKU 全部下架，但其他候选
   仍可持续核验。
4. **组合产品取许可交集**：多件作品的 SKU 必须每件作品都有满足权能（复制 + 改编级别）、
   用途类别、地域、渠道、期限的授权；合作作品还要求**每位已核验共同权利人都授权**。
5. **公益/商业严格隔离**：`use_class` 为 `COMMERCIAL` 与 `NONPROFIT_PROMO` 的设计、
   授权、SKU 互斥；公益授权（如 PRESS_KIT）不能放行商业 SKU。
6. **已放行批次的权利快照不可变**：`BATCH_RELEASED` 与 `CLEARANCE_APPROVED` 冻结当时命中的
   全部授权版本与分成条款；日后授权收窄/撤回不影响已放行批次的合法性，售罄期内的
   既有库存销售仍按快照结算。

## 目录结构

- `contracts/domain.schema.json`：27 种领域事件的信封与按 `event_type` 条件化的载荷契约。
- `src/validator.py`：标准库实现的同一套契约（无第三方依赖），信封 + 载荷深校验。
- `src/event_store.py`：append-only 存储，强制版本连续、event_id 唯一、防御性拷贝。
- `src/domain.py`：权利投影（事件重放）、许可交集、配额余量、售罄期、开售门禁 `gate()`、
  批次合法性与 `trace_sku()` 反查链。
- `src/commands.py`：命令服务——所有写操作先过领域规则，产出后继版本事件；
  改期/争议/收窄/撤回提供 `*_and_delist` 传播方法，精确下架受影响的 **SKU × 渠道**。
- `src/scenario.py`：三个月特展的端到端场景（86 个事件），覆盖全部关键规则与 6 类预期拒绝。
- `data/sample.json`：最小信封样例；`data/scenario_events.json`：完整场景事件流（生成物）。
- `tests/test_contract.py`：24 项测试，覆盖信封、版本语义与全部业务规则。

## 事件目录

| 领域 | 事件 |
|---|---|
| 展期 | `EXHIBITION_SCHEDULED` `EXHIBITION_RESCHEDULED` |
| 入藏/权属 | `ARTWORK_ACQUIRED` `RIGHT_CLAIMED` `RIGHT_CLAIM_VERIFIED` `RIGHT_CLAIM_REJECTED` |
| 争议 | `RIGHT_DISPUTE_OPENED` `RIGHT_DISPUTE_RESOLVED` |
| 授权 | `LICENSE_GRANTED` `LICENSE_AMENDED` `LICENSE_WITHDRAWN` |
| 设计 | `DESIGN_SUBMITTED` `DESIGN_APPROVED` `DESIGN_REJECTED` |
| 打样/换料 | `SAMPLE_SUBMITTED` `SAMPLE_APPROVED` `SAMPLE_REJECTED` `MATERIAL_SUBSTITUTED` |
| SKU/门禁 | `SKU_REGISTERED` `CLEARANCE_APPROVED` `CLEARANCE_REJECTED` `SKU_DELISTED` `SKU_RELISTED` |
| 生产 | `RESTOCK_COMMITTED` `BATCH_RELEASED` |
| 销售/结算 | `SALE_RECORDED` `ROYALTY_SETTLED` |

## 门禁如何裁决

对 SKU 的每个登记渠道分别给出 `can_produce / can_sell / headroom_units / on_hand_units`：

- **生产条件**：设计与最新打样均通过（换料后必须重新打样）+ 已获开售放行 +
  全部作品在该渠道/地域/当前时点存在许可交集（合作作品含全部共同权利人）+
  未下架（HOLD/DESTROY/RETURN 或售罄期已过）+ 最小授权剩余配额 ≥ 排产量。
- **销售条件**：要么当前授权窗口仍覆盖该渠道（可持续售卖），要么处于下架时约定的
  `SELL_THROUGH` 售罄期内且只能消耗已放行批次的既有库存；超过批次放行数量的销售被拒绝。
- **交付余量**：交集内各授权 `max_units − 已放行批次量 − 待兑现补货承诺量` 的最小值；
  补货承诺在批次放行前就占用配额，防止"已排产商品越界"。
- **期限基础**：`term_basis=EXHIBITION_WINDOW` 的授权，实际窗口是授权期限与最新展期
  的交集——展期一改，生产窗口与受影响 SKU 立即重算；`EXPLICIT` 授权不受展期变动影响。

## 下架反查链

`RightsProjection.trace_sku(sku)` 返回：

```
SKU → 放行审批（审批人、时间、事件 id）
    → rights_snapshot：作品 → 授权 id / 当时版本 / 当前版本 / 是否已撤回
                            → 授权人候选、核验人、核验依据、凭证 refs
    → 全部下架历史：渠道、原因码（撤权/收窄/争议/换料/展期结束/人工）、
                   触发事件 id（causation）、库存处置、批准人
    → 各生产批次：放行量、已售量、在手量、快照依据、冻结分成条款
```

## 场景验证的六个关键时点

1. **10-09 审批前夕**：周白未授权 + 马克杯首轮样品驳回 → 组合 SKU 审批驳回；
   公益授权无法支撑商业徽章 → 同样驳回。
2. **11-20 热销期**：双权利人齐备、样品通过，三渠道可产可销，丝巾配额余量 2000。
3. **12-10 出版社争议**：AW-001 相关马克杯全渠道 HOLD 下架、销售阻断；
   月底争议解决后重新上架。
4. **01-05 授权收窄**：ONLINE_MALL 被收回 → 只下架丝巾线上渠道（售罄至 02-15），
   场馆店与快闪不受影响。
5. **01-10 家属撤权**：周白授权撤回 → 马克杯全渠道停产、售罄期内消耗库存、
   已放行批次快照保留 v1 授权与 6% 分成；01-20 提前闭幕只影响展期窗口授权（海报 HOLD），
   EXPLICIT 丝巾继续售卖。
6. **02-20 售罄期结束**：撤权/收窄渠道一切销售阻断，在手库存只能 HOLD/退供/销毁。

## 本地检查

```bash
python3 -m unittest discover -s tests      # 24 项测试
python3 -m src.scenario                    # 重放场景并生成 data/scenario_events.json
```

## 领域边界

事件一旦被接收，其标识、发生时间和版本不应被原地改写；业务更正应产生后继记录。
涉及个人、机构或商业敏感信息时，调用方只读取完成职责所必需的字段。
