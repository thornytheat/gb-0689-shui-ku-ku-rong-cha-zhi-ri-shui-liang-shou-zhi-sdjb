# 深度登记 水库库容插值与日水量收支

- 规模：中型
- 核心规则：单调水位库容曲线分段线性插值；流量梯形积分；缺口超限不插补，日边界拆分且收支误差显式归因
- 使用者与工作流：水库调度数据员；采信水位流量日志，复核日库容变化与入出库偏差

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 测站采信 | 采信水位流量与来源 | `/api/m1` | `app/modules/m1_receive/rules.py` `app/modules/m1_receive/service.py` | `tests/test_m1_receive.py` |
| M2 曲线校核 | 校验曲线及测次冲突 | `/api/m2` | `app/modules/m2_curve/rules.py` `app/modules/m2_curve/service.py` | `tests/test_m2_curve.py` |
| M3 流量积分 | 梯形积分并定位缺口 | `/api/m3` | `app/modules/m3_integrate/rules.py` `app/modules/m3_integrate/service.py` | `tests/test_m3_integrate.py` |
| M4 库容换算 | 按曲线分段换算库容 | `/api/m4` | `app/modules/m4_storage/rules.py` `app/modules/m4_storage/service.py` | `tests/test_m4_storage.py` |
| M5 日收支 | 跨午夜切分日水量账 | `/api/m5` | `app/modules/m5_budget/rules.py` `app/modules/m5_budget/service.py` | `tests/test_m5_budget.py` |
| M6 异常复核 | 按缺测优先裁决超差 | `/api/m6` | `app/modules/m6_review/rules.py` `app/modules/m6_review/service.py` | `tests/test_m6_review.py` |
| M6 曲线复测影响 | 逐日期比对新旧曲线库容变化并裁决重开 | `/api/m6/impacts` | `app/modules/m6_review/impact_rules.py` `app/modules/m6_review/impact_service.py` | `tests/test_m6_curve_impact.py` |
| M7 重算留痕 | 核对总积分与重算水位 | `/api/m7` | `app/modules/m7_trace/rules.py` `app/modules/m7_trace/service.py` | `tests/test_m7_trace.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030测站 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m1_receive/rules.py` | `tests/test_m1_receive.py::test_legacy_encoding` |
| TRAP-06 | 全角流量 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_fullwidth` |
| TRAP-07 | 未知水位 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_sentinel` |
| TRAP-10 | 小数插值 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m4_storage/rules.py` | `tests/test_m4_storage.py::test_decimal_precision` |
| TRAP-13 | 缺口和乱序 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m3_integrate/rules.py` | `tests/test_m3_integrate.py::test_real_late_point_retained` |
| TRAP-14 | 同刻水文矛盾 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_real_conflict` |
| TRAP-15 | 升每秒换算 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_unit_conversion` |
| TRAP-23 | 午夜切分 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m5_budget/rules.py` | `tests/test_m5_budget.py::test_midnight_split` |
| TRAP-30 | 曲线复测全线退回 | 按复测点日期退回会漏掉水位跨过变更区间的日子 | 逐日用同一批 flows 在新旧曲线重算日账，按库容变化判定受影响日期 | `app/modules/m6_review/impact_rules.py` | `tests/test_m6_curve_impact.py::test_day_crossing_changed_interval_is_caught` |
| TRAP-31 | 同曲线修订重复生成影响记录 | 每次提交都插一条清单 | 曲线指纹 + 唯一约束幂等，重跑复用既有清单 | `app/store.py` `app/modules/m6_review/impact_service.py` | `tests/test_m6_curve_impact.py::test_service_is_idempotent_same_revision` |
| TRAP-32 | 影响分析失败覆盖旧清单 | 算到一半写库导致旧版丢失 | 先内存完整计算再单行原子写入，失败不触碰既有清单 | `app/modules/m6_review/impact_service.py` | `tests/test_m6_curve_impact.py::test_failure_preserves_previous_list` |
| TRAP-33 | 新曲线越界整体中断 | 一日越界导致全部日期无法分析 | 单桶越界登记为无法换算，其余日期照常出清单 | `app/modules/m6_review/impact_rules.py` | `tests/test_m6_curve_impact.py::test_out_of_range_day_marked_not_crashed` |

## 对账规则

输入逻辑行数 = 采信行数 + 隔离行数；空行单列。每条重复、冲突、超限、缺失均保留原始行号与原因码。计算阶段的输出数量可聚合，原始采信records始终保留，业务平衡另按模块给出的重量、时长、电量或座席数量核对。
相同输入摘要、规则版本、参数产生相同批次ID。运行原子插入SQLite；复核使用整数版本乐观锁。

## 已知不覆盖

库容曲线采用线性分段，不作范围外推；流量与水位相邻点间线性变化。未建蒸发降雨及地下渗漏模型，收支差保留待人工归因。
