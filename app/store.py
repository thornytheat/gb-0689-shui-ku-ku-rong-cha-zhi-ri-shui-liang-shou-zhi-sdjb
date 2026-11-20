import os,sqlite3,json
from pathlib import Path
from app.errors import DomainError
def connection():
    path=Path(os.environ.get("DATA_DIR","data"));path.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path/"runs.sqlite",timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核')")
    db.execute("CREATE TABLE IF NOT EXISTS curve_impacts (base_run_id TEXT NOT NULL,new_curve_fingerprint TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL,UNIQUE(base_run_id,new_curve_fingerprint))")
    return db
def save(result):
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES(?,?)",(result["manifest"]["run_id"],json.dumps(result,ensure_ascii=False)))
    return fetch(result["manifest"]["run_id"])
def fetch(run_id):
    with connection() as db: row=db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","批次不存在",404)
    result=json.loads(row["payload"]);result["version"]=row["version"];result["review"]=row["review"]
    return result
def history():
    with connection() as db: rows=db.execute("SELECT id,version,review FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]
def review(run_id,version,decision):
    if decision not in ("通过","退回"): raise DomainError("REVIEW","请选择通过或退回")
    if not isinstance(version,int): raise DomainError("STALE","版本必须为整数")
    with connection() as db:
        cur=db.execute("UPDATE runs SET review=?,version=version+1 WHERE id=? AND version=?",(decision,run_id,version))
        if cur.rowcount!=1: raise DomainError("STALE","复核版本已更新",409)
    return fetch(run_id)
def save_impact(payload):
    """幂等保存曲线修订影响清单。

    同一 (基线批次, 新曲线指纹) 重跑只返回既有清单，不重复生成；
    payload 必须在调用前完整算好，本函数仅执行单行原子写入，
    计算阶段抛错时旧版清单原样保留。
    """
    base=payload["base_run_id"];fp=payload["new_curve_fingerprint"]
    with connection() as db:
        existing=db.execute("SELECT payload FROM curve_impacts WHERE base_run_id=? AND new_curve_fingerprint=?",(base,fp)).fetchone()
        if existing is not None:return json.loads(existing["payload"])
        try:
            db.execute("INSERT INTO curve_impacts(base_run_id,new_curve_fingerprint,payload,created) VALUES(?,?,?,?)",
                       (base,fp,json.dumps(payload,ensure_ascii=False),payload["generated_at"]))
        except sqlite3.IntegrityError:
            # 并发的同曲线修订：赢家已写入，复用其清单，绝不重复生成。
            existing=db.execute("SELECT payload FROM curve_impacts WHERE base_run_id=? AND new_curve_fingerprint=?",(base,fp)).fetchone()
            return json.loads(existing["payload"])
        row=db.execute("SELECT payload FROM curve_impacts WHERE base_run_id=? AND new_curve_fingerprint=?",(base,fp)).fetchone()
    return json.loads(row["payload"])
def fetch_impact(base_run_id,fingerprint):
    with connection() as db:
        row=db.execute("SELECT payload FROM curve_impacts WHERE base_run_id=? AND new_curve_fingerprint=?",(base_run_id,fingerprint)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","该曲线修订的影响清单不存在",404)
    return json.loads(row["payload"])
def list_impacts(base_run_id=None):
    with connection() as db:
        if base_run_id is None:
            rows=db.execute("SELECT payload FROM curve_impacts ORDER BY rowid DESC LIMIT 100").fetchall()
        else:
            rows=db.execute("SELECT payload FROM curve_impacts WHERE base_run_id=? ORDER BY rowid DESC",(base_run_id,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]
