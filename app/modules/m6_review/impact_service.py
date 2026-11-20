# 曲线复测复核影响服务：校验输入 -> 还原持久化类型 -> 纯规则计算 -> 幂等落库。
# 计算与落库严格分离：build_impact 全部完成前不写库，失败时旧版清单保留。
from datetime import datetime, timezone
from decimal import Decimal as D
from app import store
from app.errors import DomainError
from app.modules.m2_curve.rules import validate_curve
from app.modules.m4_storage.service import level_to_volume
from .impact_rules import build_impact, curve_fingerprint


def _rehydrate_flows(flows):
    """持久化 payload 里时间与数值均为字符串，这里还原为 day_budget 需要的原生类型。"""
    restored = []
    for part in flows:
        left = dict(part['left'])
        right = dict(part['right'])
        for side in (left, right):
            side['time'] = datetime.fromisoformat(side['time'])
            for key in ('level', 'inflow', 'outflow'):
                side[key] = D(str(side[key]))
        restored.append({**part,
                         'start': datetime.fromisoformat(part['start']),
                         'end': datetime.fromisoformat(part['end']),
                         'in_m3': D(str(part['in_m3'])),
                         'out_m3': D(str(part['out_m3'])),
                         'left': left, 'right': right,
                         'station': part['station']})
    return restored


def _rehydrate_base(base):
    """让已保存批次恢复成 build_impact 可直接消费的内存形态。

    批次级复核结论（runs.review）下推到每条日账，作为“原核定结果”参与重开判定；
    待复核/退回批次的日账没有已通过效力，即使受影响也不产生重开标记。
    """
    ctx = dict(base)
    ctx['curve'] = [[D(str(h)), D(str(v))] for h, v in base['curve']]
    ctx['flows'] = _rehydrate_flows(base['flows'])
    run_review = base.get('review') or '待复核'
    ctx['review_rows'] = [{**r,
                           'review': run_review,
                           'residual': D(str(r['residual'])),
                           'storage_change': D(str(r['storage_change']))}
                          for r in base['review_rows']]
    return ctx


def generate_impact(base_run_id, new_curve_points):
    """对已保存批次执行一次曲线修订影响分析，返回 (影响清单, 是否首次生成)。"""
    base = store.fetch(base_run_id)
    if "review_rows" not in base or "flows" not in base:
        raise DomainError("STATE", "基线批次缺少日账或积分结果，无法分析曲线影响")
    if not isinstance(new_curve_points, list):
        raise DomainError("CURVE", "新曲线必须是 [水位,库容] 点列表")
    new_curve = validate_curve(new_curve_points)
    fingerprint = curve_fingerprint(new_curve)
    # 先查后算：同一曲线修订的重跑直接复用既有清单，绝不重复生成。
    try:
        return store.fetch_impact(base_run_id, fingerprint), False
    except DomainError as exc:
        if exc.code != "NOT_FOUND":
            raise
    # 纯计算；任何异常在此抛出，store 尚未被触碰，旧版清单保持不变。
    payload = build_impact(_rehydrate_base(base), new_curve, level_to_volume)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return store.save_impact(payload), True


def impacts_for_run(base_run_id):
    return store.list_impacts(base_run_id)
