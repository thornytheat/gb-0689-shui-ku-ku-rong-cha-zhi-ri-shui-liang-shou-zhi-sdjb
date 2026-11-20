# 库容曲线局部复测的复核影响判定（纯规则，不触碰数据库与框架）。
# 历史水文值（水位/流量/测次）不变，仅库容换算所用曲线发生修订；
# 因此逐日用同一批 flows 在新旧曲线上重算日账，比较库容变化与核定结果。
from decimal import Decimal as D
from hashlib import sha256
import json
from app.errors import DomainError
from app.modules.m4_storage.rules import interpolate
from app.modules.m6_review.rules import evaluate

# 日切与整段积分容许 1e-9 m³ 以内的十进制除法尾数（与 DATA-DICTIONARY 一致）。
EPS = D('0.000000001')


def curve_fingerprint(curve):
    """曲线内容指纹：同样的曲线修订永远得到同一标识，用于幂等去重。"""
    body = json.dumps([[str(h), str(v)] for h, v in curve], ensure_ascii=False, sort_keys=True)
    return sha256(body.encode('utf-8')).hexdigest()[:20]


def _volume_at(level, curve):
    try:
        return interpolate(level, curve), None
    except DomainError as exc:
        return None, exc.message


def curve_diff(old_curve, new_curve, levels):
    """新旧曲线差异依据。

    返回 (segments, range_notes)：
    - segments：按两条曲线节点并集切出的整条变化区间，含端点、新旧库容及差值，
      不按观测水位切碎，便于复核人员直接读出复测改动的区段；
    - range_notes：某条曲线无法覆盖给定水位时的范围说明。
    """
    notes = []
    for level in sorted(set(levels)):
        old_v, old_err = _volume_at(level, old_curve)
        new_v, new_err = _volume_at(level, new_curve)
        if old_err or new_err:
            which = '旧曲线越界: %s' % old_err if old_err else '新曲线越界: %s' % new_err
            notes.append({'level': str(level), 'reason': which})
    lo = min(old_curve[0][0], new_curve[0][0])
    hi = max(old_curve[-1][0], new_curve[-1][0])
    cuts = sorted(set([lo, hi]
                      + [h for h, _ in old_curve if lo <= h <= hi]
                      + [h for h, _ in new_curve if lo <= h <= hi]))
    segments = []
    for ha, hb in zip(cuts, cuts[1:]):
        mid = (ha + hb) / 2
        old_v, old_err = _volume_at(mid, old_curve)
        new_v, new_err = _volume_at(mid, new_curve)
        if old_err or new_err:
            continue
        delta = new_v - old_v
        if abs(delta) > EPS:
            segments.append({'level_from': str(ha), 'level_to': str(hb),
                             'old_volume': str(old_v), 'new_volume': str(new_v),
                             'delta_volume': str(delta)})
    return segments, notes


def day_boundary_levels(flows):
    """收集每个 (测站, 日期) 桶实际参与库容换算的水位（段端点与日界点）。"""
    from datetime import datetime, timedelta, timezone
    zone = timezone(timedelta(hours=8))
    buckets = {}
    for part in flows:
        left, right = part['left'], part['right']
        start, end = part['start'], part['end']
        duration = D(str((end - start).total_seconds()))
        cursor = start

        def at(t, key):
            return left[key] + (right[key] - left[key]) * D(str((t - start).total_seconds())) / duration

        while cursor < end:
            local = cursor.astimezone(zone)
            midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
            stop = min(end, midnight)
            key = (part['station'], local.date().isoformat())
            seen = buckets.setdefault(key, set())
            seen.add(at(cursor, 'level'))
            seen.add(at(stop, 'level'))
            cursor = stop
    return {key: sorted(vals) for key, vals in buckets.items()}


def _basis_for_day(day_levels, segments, notes):
    """该日水位实际落在哪几个变化区间上，作为重开依据的最小充分集。

    复用 build_impact 已算好的整段曲线差异，只做区间相交筛选，不重复计算。
    """
    hit = []
    for seg in segments:
        lo, hi = D(seg['level_from']), D(seg['level_to'])
        if any(lo - EPS <= level <= hi + EPS for level in day_levels):
            hit.append(seg)
    day_notes = [n for n in notes if D(n['level']) in day_levels]
    return {'changed_segments': hit, 'range_notes': day_notes}


def assess_day(old_row, new_row, basis):
    """单个日期的影响判定。"""
    old_change = D(old_row['storage_change'])
    if new_row is None:
        affected, reason = True, '新曲线无法覆盖该日水位，日账不能成立'
        new_change = new_residual = None
    else:
        new_change = D(new_row['storage_change'])
        new_residual = D(new_row['residual'])
        if abs(new_change - old_change) > EPS:
            affected, reason = True, '新旧曲线在该日水位上的库容插值不同，库容变化发生改变'
        else:
            affected, reason = False, '该日水位所在区间新旧曲线库容一致'
    old_review = old_row.get('review') or '待复核'
    # 原核定为通过的日期一旦受影响必须重开；其余受影响日期同样不得继续按已通过处理。
    return {
        'station': old_row['station'],
        'date': old_row['date'],
        'affected': affected,
        'need_reopen': bool(affected and old_review == '通过'),
        'reason': reason,
        'old_review': old_review,
        'old_state': old_row['state'],
        'old_code': old_row['code'],
        'new_state': new_row['state'] if new_row else '无法换算',
        'new_code': new_row['code'] if new_row else 'CURVE',
        'old_storage_change': str(old_change),
        'new_storage_change': str(new_change) if new_change is not None else None,
        'storage_delta': str(new_change - old_change) if new_change is not None else None,
        'old_residual': str(old_row['residual']),
        'new_residual': str(new_residual) if new_residual is not None else None,
        'curve_basis': basis,
    }


def _budgets_with_range_guard(flows, curve, interpolate_fn):
    """与 day_budget 同一日切口径，但单桶换算越界时登记而非整体抛错。

    新曲线覆盖不到历史水位时，受影响日期仍要进入清单并标记无法换算，
    而不是让整次修订分析失败。流量积分不依赖曲线，照常累计。
    """
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone
    zone = timezone(timedelta(hours=8))
    buckets = defaultdict(lambda: {'in_m3': D(0), 'out_m3': D(0), 'storage_change': D(0),
                                   'coverage_hours': D(0)})
    failed = {}
    for part in flows:
        left, right = part['left'], part['right']
        start, end = part['start'], part['end']
        duration = D(str((end - start).total_seconds()))
        cursor = start

        def at(t, key):
            return left[key] + (right[key] - left[key]) * D(str((t - start).total_seconds())) / duration

        while cursor < end:
            local = cursor.astimezone(zone)
            midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
            stop = min(end, midnight)
            seconds = D(str((stop - cursor).total_seconds()))
            key = (part['station'], local.date().isoformat())
            b = buckets[key]
            b['in_m3'] += (at(cursor, 'inflow') + at(stop, 'inflow')) / 2 * seconds
            b['out_m3'] += (at(cursor, 'outflow') + at(stop, 'outflow')) / 2 * seconds
            try:
                b['storage_change'] += interpolate_fn(at(stop, 'level'), curve) - interpolate_fn(at(cursor, 'level'), curve)
            except DomainError as exc:
                failed[key] = exc.message
            b['coverage_hours'] += seconds / 3600
            cursor = stop
    result = []
    for (station, day), values in sorted(buckets.items()):
        if (station, day) in failed:
            continue
        result.append({'station': station, 'date': day, **values,
                       'residual': values['storage_change'] - values['in_m3'] + values['out_m3'],
                       'state': '完整' if values['coverage_hours'] == 24 else '缺测'})
    return result, failed


def build_impact(base_run, new_curve, interpolate_fn):
    """对一次曲线修订生成逐日期复核影响清单（纯计算；失败由调用方保证旧清单不被覆盖）。"""
    old_curve = [tuple(map(D, point)) for point in base_run['curve']]
    new_curve = [tuple(map(D, point)) for point in new_curve]
    if new_curve == old_curve:
        raise DomainError('CURVE', '新曲线与原曲线完全相同，没有修订内容')
    flows = base_run['flows']
    old_rows = {(r['station'], r['date']): r for r in base_run['review_rows']}
    boundaries = day_boundary_levels(flows)
    all_levels = sorted({level for vals in boundaries.values() for level in vals})
    segments, notes = curve_diff(old_curve, new_curve, all_levels)
    # 同一批历史水文值（flows 不变）在新曲线上重算日账，裁决口径与 M6 完全一致。
    new_budgets, failed_buckets = _budgets_with_range_guard(flows, new_curve, interpolate_fn)
    new_review = {(r['station'], r['date']): r
                  for r in evaluate(new_budgets, D(str(base_run['params']['tolerance_m3'])))}
    days = []
    for key, old_row in sorted(old_rows.items()):
        day_levels = boundaries.get(key, [])
        basis = _basis_for_day(day_levels, segments, notes)
        if key in failed_buckets:
            basis = {'changed_segments': basis['changed_segments'],
                     'range_notes': basis['range_notes'] + [
                         {'level': str(lv), 'reason': '新曲线越界: %s' % failed_buckets[key]}
                         for lv in day_levels
                         if not (new_curve[0][0] <= lv <= new_curve[-1][0])]}
            new_row = None
        else:
            new_row = new_review.get(key)
        days.append(assess_day(old_row, new_row, basis))
    return {
        'base_run_id': base_run['manifest']['run_id'],
        'old_curve_fingerprint': curve_fingerprint(old_curve),
        'new_curve_fingerprint': curve_fingerprint(new_curve),
        'old_curve': [[str(h), str(v)] for h, v in old_curve],
        'new_curve': [[str(h), str(v)] for h, v in new_curve],
        'curve_diff': {'changed_segments': segments, 'range_notes': notes},
        'affected_dates': sum(1 for d in days if d['affected']),
        'reopen_dates': sum(1 for d in days if d['need_reopen']),
        'days': days,
    }
