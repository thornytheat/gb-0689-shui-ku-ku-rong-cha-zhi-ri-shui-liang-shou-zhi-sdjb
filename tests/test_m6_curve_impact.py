import html as html_lib
import json as json_lib
import re
import pytest
from decimal import Decimal as D
from app.errors import DomainError
from app import store
from app.pipeline import run
from app.modules.m6_review.impact_rules import (
    build_impact, curve_diff, curve_fingerprint, day_boundary_levels,
)
from app.modules.m6_review.impact_service import generate_impact, _rehydrate_base
from app.modules.m4_storage.service import level_to_volume

HEADER = "id,station,time,level,inflow,outflow,flow_unit,version\n"
CURVE = [["100", "0"], ["102", "30000"], ["105", "100000"], ["108", "220000"]]


def hourly_csv(station="青岚库", day="2026-11-16", levels=None, inflow="1.0", outflow="1.0"):
    """24 个整点（当日 00 时~次日 00 时）、相邻间隔 1h，完整覆盖 24h 可裁决为通过。"""
    if levels is None:
        levels = [str(D("101.0") + D(i) * D("0.01")) for i in range(25)]
    lines = [HEADER]
    next_day = "2026-11-17" if day == "2026-11-16" else day
    for i, level in enumerate(levels[:-1]):
        lines.append(f"{i+1:05d},{station},{day}T{i:02d}:00:00+08:00,{level},{inflow},{outflow},m3/s,1\n")
    lines.append(f"{len(levels):05d},{station},{next_day}T00:00:00+08:00,{levels[-1]},{inflow},{outflow},m3/s,1\n")
    return "".join(lines).encode("utf-8")


@pytest.fixture
def approved_run(isolate):
    """完整一天 -> 持久化 -> 复核通过的基线批次（容差放宽到覆盖构造数据的收支差）。"""
    params = {"curve": CURVE, "max_gap_hours": "2", "tolerance_m3": "1000000"}
    result = run(hourly_csv(), params)
    assert result["review_rows"][0]["state"] == "通过"
    store.review(result["manifest"]["run_id"], result["version"], "通过")
    return store.fetch(result["manifest"]["run_id"])


def test_curve_fingerprint_stable_and_order_insensitive():
    a = [(D("100"), D("0")), (D("102"), D("30000"))]
    b = [("100", "0"), ("102", "30000")]
    assert curve_fingerprint(a) == curve_fingerprint(b)
    other = [("100", "0"), ("102", "30001")]
    assert curve_fingerprint(a) != curve_fingerprint(other)


def test_curve_diff_reports_only_changed_segments():
    old = [(D("100"), D("0")), (D("102"), D("30000")), (D("105"), D("100000"))]
    # 只复测 102~105 一段，100~102 保持不变
    new = [(D("100"), D("0")), (D("102"), D("30000")), (D("105"), D("110000"))]
    segments, notes = curve_diff(old, new, [D("101"), D("103")])
    assert notes == []
    assert len(segments) == 1
    seg = segments[0]
    assert seg["level_from"] == "102" and seg["level_to"] == "105"
    # 区间中点 103.5：旧库容 65000，新库容 70000，差值 5000
    assert D(seg["old_volume"]) == D("65000.0")
    assert D(seg["new_volume"]) == D("70000.0")
    assert D(seg["delta_volume"]) == D("5000.0")


def test_curve_diff_flags_out_of_range_levels():
    old = [(D("100"), D("0")), (D("102"), D("30000"))]
    new = [(D("101"), D("1000")), (D("102"), D("30000"))]
    segments, notes = curve_diff(old, new, [D("100.5")])
    assert any("新曲线越界" in n["reason"] and n["level"] == "100.5" for n in notes)


def test_identical_curve_rejected(approved_run):
    base = _rehydrate_base(approved_run)
    with pytest.raises(DomainError) as exc:
        build_impact(base, [tuple(map(D, p)) for p in CURVE], level_to_volume)
    assert exc.value.code == "CURVE"


def test_affected_day_reopens_when_previously_approved(approved_run):
    # 改动覆盖观测水位 101.00~101.24 的 100~102 段
    revised = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    impact = build_impact(_rehydrate_base(approved_run), revised, level_to_volume)
    assert impact["affected_dates"] == 1 and impact["reopen_dates"] == 1
    day = impact["days"][0]
    assert day["affected"] is True and day["need_reopen"] is True
    assert day["old_review"] == "通过" and day["old_code"] == "PASS"
    assert D(day["storage_delta"]) != 0
    # 依据必须落到该日水位所在的变化区间
    assert any(s["level_from"] == "100" and s["level_to"] == "102"
               for s in day["curve_basis"]["changed_segments"])


def test_unaffected_approved_day_remains_valid(approved_run):
    # 只改 105~108 一段，观测水位（101.x）完全不跨入该区间
    revised = [["100", "0"], ["102", "30000"], ["105", "100000"], ["108", "230000"]]
    impact = build_impact(_rehydrate_base(approved_run), revised, level_to_volume)
    assert impact["affected_dates"] == 0 and impact["reopen_dates"] == 0
    day = impact["days"][0]
    assert day["affected"] is False and day["need_reopen"] is False
    assert day["new_storage_change"] == day["old_storage_change"]
    assert day["curve_basis"]["changed_segments"] == []


def test_day_crossing_changed_interval_is_caught(isolate):
    # 水位从复测段下半区（100.2）单调抬升到上半区（101.8）：
    # 只按复测点所在日期退回会漏掉这种水位跨过变更区间的日子
    levels = [str(D("100.2") + D(i) * D("1.6") / D("24")) for i in range(25)]
    result = run(hourly_csv(station="S", levels=levels), {"curve": CURVE, "tolerance_m3": "1000000"})
    store.review(result["manifest"]["run_id"], result["version"], "通过")
    hydrated = _rehydrate_base(store.fetch(result["manifest"]["run_id"]))
    # 真正的局部复测：102 节点库容由 30000 改为 33000，整个 100~102 段的换算都变
    revised = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    impact = build_impact(hydrated, revised, level_to_volume)
    day = impact["days"][0]
    assert day["affected"] is True and day["need_reopen"] is True
    assert D(day["storage_delta"]) != 0
    boundary = day_boundary_levels(hydrated["flows"])
    assert any(v > D("101") for vals in boundary.values() for v in vals)
    # 该日水位同时落在变更区间的下半区与上半区，依据必须能把整段变化交代清楚
    assert any(D(s["level_from"]) <= D("100.2") <= D(s["level_to"])
               for s in day["curve_basis"]["changed_segments"])


def test_out_of_range_day_marked_not_crashed(approved_run):
    # 新曲线下限抬到 101.1，覆盖不了 101.0 的历史水位
    revised = [["101.1", "16000"], ["102", "30000"], ["105", "100000"], ["108", "220000"]]
    impact = build_impact(_rehydrate_base(approved_run), revised, level_to_volume)
    day = impact["days"][0]
    assert day["affected"] is True and day["need_reopen"] is True
    assert day["new_state"] == "无法换算" and day["new_code"] == "CURVE"
    assert day["new_storage_change"] is None
    assert day["curve_basis"]["range_notes"]


def test_service_is_idempotent_same_revision(approved_run):
    revised = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    first, created_first = generate_impact(approved_run["manifest"]["run_id"], revised)
    second, created_second = generate_impact(approved_run["manifest"]["run_id"], revised)
    assert created_first is True and created_second is False
    assert first == second
    # 库里只有一条影响记录
    assert len(store.list_impacts(approved_run["manifest"]["run_id"])) == 1


def test_distinct_revisions_keep_separate_lists(approved_run):
    rid = approved_run["manifest"]["run_id"]
    rev1 = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    rev2 = [["100", "0"], ["102", "36000"], ["105", "100000"], ["108", "220000"]]
    generate_impact(rid, rev1)
    generate_impact(rid, rev2)
    items = store.list_impacts(rid)
    assert len(items) == 2
    assert {i["new_curve_fingerprint"] for i in items} != {i["old_curve_fingerprint"] for i in items}


def test_failure_preserves_previous_list(approved_run):
    rid = approved_run["manifest"]["run_id"]
    rev1 = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    good, _ = generate_impact(rid, rev1)
    with pytest.raises(DomainError):
        generate_impact(rid, [["100", "0"], ["99", "10"]])  # 非递增，校验失败
    remaining = store.list_impacts(rid)
    assert len(remaining) == 1 and remaining[0] == good


def test_interior_knot_change_with_equal_boundary_volume_not_affected(isolate):
    # 日账库容变化只取决于日界端点库容：内部节点 101 改值但 100/102 端点库容不变时，
    # 该日库变不变，不应误判为受影响（依据仍可在 curve_diff 中查到区间形状差异）。
    levels = [str(D("100.2") + D(i) * D("1.6") / D("24")) for i in range(25)]
    result = run(hourly_csv(station="S", levels=levels), {"curve": CURVE, "tolerance_m3": "1000000"})
    hydrated = _rehydrate_base(store.fetch(result["manifest"]["run_id"]))
    revised = [["100", "0"], ["101", "18000"], ["102", "30000"], ["105", "100000"], ["108", "220000"]]
    impact = build_impact(hydrated, revised, level_to_volume)
    assert impact["days"][0]["affected"] is False
    assert impact["curve_diff"]["changed_segments"]  # 区间形状差异仍有案可查


def test_pending_day_affected_but_not_marked_reopen(isolate):
    # 未走通过复核的批次：受影响但 old_review 不是“通过”，不标记重开
    result = run(hourly_csv(), {"curve": CURVE, "tolerance_m3": "1000000"})
    revised = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    impact = build_impact(_rehydrate_base(store.fetch(result["manifest"]["run_id"])), revised, level_to_volume)
    day = impact["days"][0]
    assert day["affected"] is True and day["need_reopen"] is False
    assert day["old_review"] == "待复核"


def test_rejected_run_affected_but_never_reopens(isolate):
    # 退回批次的日账没有“已通过”效力：受影响但不产生重开标记
    result = run(hourly_csv(), {"curve": CURVE, "tolerance_m3": "1000000"})
    store.review(result["manifest"]["run_id"], result["version"], "退回")
    revised = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    impact, created = generate_impact(result["manifest"]["run_id"], revised)
    assert created is True
    day = impact["days"][0]
    assert day["affected"] is True and day["need_reopen"] is False
    assert day["old_review"] == "退回"


def test_verdict_flip_pass_to_limit_is_recorded(isolate):
    # 小容差下，曲线修订把收支差推过阈值：原 PASS 翻成 LIMIT，且因原核定通过必须重开
    levels = [str(D("101.0") + D(i) * D("0.0001")) for i in range(25)]
    result = run(hourly_csv(station="S", levels=levels), {"curve": CURVE, "tolerance_m3": "100"})
    assert result["review_rows"][0]["code"] == "PASS"
    store.review(result["manifest"]["run_id"], result["version"], "通过")
    revised = [["100", "0"], ["102", "120000"], ["105", "200000"], ["108", "320000"]]
    impact, _ = generate_impact(result["manifest"]["run_id"], revised)
    day = impact["days"][0]
    assert day["old_code"] == "PASS" and day["new_code"] == "LIMIT"
    assert abs(D(day["new_residual"])) > D("100")
    assert day["need_reopen"] is True


def test_mixed_affected_and_unaffected_days(client):
    """同一批次里同时存在受影响与未受影响日期：未受影响的批准继续有效。"""
    lines = [HEADER]
    n = 1
    # 第一天水位落在 100~102 段，第二天落在 102~105 段
    for day, base_lv, step in [("2026-11-16", D("100.2"), D("0.02")),
                               ("2026-11-17", D("103.0"), D("0.01"))]:
        for i in range(24):
            lv = base_lv + D(i) * step
            lines.append(f"{n:05d},青岚库,{day}T{i:02d}:00:00+08:00,{lv},1,1,m3/s,1\n")
            n += 1
    lines.append(f"{n:05d},青岚库,2026-11-18T00:00:00+08:00,{D('103.0') + D(23) * D('0.01')},1,1,m3/s,1\n")
    r = client.post("/api/runs", json={"text": "".join(lines),
                                       "params": {"curve": CURVE, "tolerance_m3": "1000000"}}).json
    rid = r["manifest"]["run_id"]
    client.post(f"/api/review/{rid}", json={"version": r["version"], "decision": "通过"})
    # 复测只在 100~102 段内插一个改值节点，102 及以上完全不动 -> 第二天不受影响
    revised = [["100", "0"], ["101", "16000"], ["102", "30000"], ["105", "100000"], ["108", "220000"]]
    body = client.post("/api/m6/impacts", json={"base_run_id": rid, "new_curve": revised}).json
    by_date = {d["date"]: d for d in body["days"]}
    assert by_date["2026-11-16"]["affected"] is True
    assert by_date["2026-11-16"]["need_reopen"] is True
    assert by_date["2026-11-17"]["affected"] is False
    assert by_date["2026-11-17"]["need_reopen"] is False
    assert by_date["2026-11-17"]["curve_basis"]["changed_segments"] == []
    html = client.get(f"/m/6?run={rid}").get_data(as_text=True)
    assert "row-reopen" in html and "row-ok" in html and "原批准继续有效" in html


def test_http_endpoints_and_render(client):
    r = client.post("/api/runs", json={"text": hourly_csv().decode(),
                                       "params": {"curve": CURVE, "tolerance_m3": "1000000"}}).json
    rid = r["manifest"]["run_id"]
    client.post(f"/api/review/{rid}", json={"version": r["version"], "decision": "通过"})
    revised = [["100", "0"], ["102", "33000"], ["105", "100000"], ["108", "220000"]]
    first = client.post("/api/m6/impacts", json={"base_run_id": rid, "new_curve": revised})
    assert first.status_code == 201 and first.json["created"] is True
    second = client.post("/api/m6/impacts", json={"base_run_id": rid, "new_curve": revised})
    assert second.status_code == 200 and second.json["created"] is False
    listing = client.get(f"/api/m6/impacts?run={rid}")
    assert listing.status_code == 200 and len(listing.json["items"]) == 1
    page = client.get(f"/m/6?run={rid}").get_data(as_text=True)
    assert "需重开" in page and "row-reopen" in page and "新旧曲线差异" in page
    # 文本框里的曲线必须是合法 JSON（前端 JSON.parse 后直接提交），不能是 Python repr
    match = re.search(r'<textarea name="new_curve"[^>]*>(.*?)</textarea>', page, re.S)
    assert json_lib.loads(html_lib.unescape(match.group(1))) == CURVE
    # 非法请求
    assert client.post("/api/m6/impacts", json={"new_curve": revised}).status_code == 422
    assert client.post("/api/m6/impacts", json={"base_run_id": rid, "new_curve": CURVE}).status_code == 422
    assert client.get("/api/m6/impacts").status_code == 422
