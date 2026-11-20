from flask import Blueprint,request,jsonify
from app.pipeline import run
from app.errors import DomainError
from .impact_service import generate_impact, impacts_for_run
bp=Blueprint("m6",__name__)
@bp.route("/api/m6",methods=["GET","POST"])
def endpoint():
    payload=request.get_json(silent=True) if request.method=="POST" else {}
    if not isinstance(payload,dict): raise DomainError("BODY","请求必须是对象")
    raw=payload.get("text")
    if raw is not None and not isinstance(raw,str): raise DomainError("TEXT","数据必须为文本")
    return jsonify(run(raw,payload.get("params"),stop=6,persist=False))
@bp.route("/api/m6/impacts",methods=["GET","POST"])
def impacts():
    if request.method=="GET":
        base=request.args.get("run")
        if not base: raise DomainError("BODY","请提供基线批次 run")
        return jsonify({"base_run_id":base,"items":impacts_for_run(base)})
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict): raise DomainError("BODY","请求必须是对象")
    base=payload.get("base_run_id")
    if not isinstance(base,str) or not base: raise DomainError("BODY","请提供基线批次 base_run_id")
    curve=payload.get("new_curve")
    if curve is None: raise DomainError("CURVE","请提供复测后的新曲线 new_curve")
    result,created=generate_impact(base,curve)
    return jsonify({"created":created,**result}), (201 if created else 200)
