"""Durable, versioned SQLite storage for the demo FX portfolio."""
from __future__ import annotations

import hashlib, json, os, sqlite3, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "runtime" / "atlas_fx.db"

def utc_iso(stamp=None):
    return datetime.fromtimestamp(stamp or time.time(), timezone.utc).isoformat(timespec="seconds")

def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=10000")
    try:
        yield db; db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        db.close()

def initialize():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS baskets(candidate_id TEXT PRIMARY KEY,updated REAL NOT NULL,status TEXT NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS config_versions(config_hash TEXT PRIMARY KEY,version TEXT NOT NULL,created REAL NOT NULL,config_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY,started REAL NOT NULL,ended REAL,status TEXT NOT NULL,strategy_version TEXT,config_hash TEXT,details_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,utc REAL NOT NULL,source TEXT NOT NULL,event TEXT NOT NULL,session_id TEXT,strategy_version TEXT,symbol TEXT,payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_time ON events(utc DESC);
        CREATE TABLE IF NOT EXISTS state_transitions(id INTEGER PRIMARY KEY AUTOINCREMENT,utc REAL NOT NULL,component TEXT NOT NULL,from_state TEXT,to_state TEXT NOT NULL,reason TEXT,session_id TEXT,strategy_version TEXT,payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS transitions_time ON state_transitions(utc DESC);
        CREATE TABLE IF NOT EXISTS ai_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,utc REAL NOT NULL,candidate_id TEXT,symbol TEXT,model TEXT,approved INTEGER,session_id TEXT,strategy_version TEXT,payload_json TEXT NOT NULL,replay_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reviews_time ON ai_reviews(utc DESC);
        CREATE TABLE IF NOT EXISTS watchlist(id INTEGER PRIMARY KEY AUTOINCREMENT,candidate_id TEXT NOT NULL UNIQUE,signal_utc REAL NOT NULL,evaluated_utc REAL,symbol TEXT NOT NULL,direction TEXT NOT NULL,timeframe TEXT,reference_price REAL,horizon_minutes INTEGER NOT NULL,status TEXT NOT NULL,outcome TEXT NOT NULL,final_pnl REAL,mfe REAL,mae REAL,reason TEXT,session_id TEXT,strategy_version TEXT,payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS watch_status ON watchlist(status,signal_utc);
        CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY AUTOINCREMENT,utc REAL NOT NULL,component TEXT NOT NULL,name TEXT NOT NULL,duration_ms REAL NOT NULL,ok INTEGER NOT NULL,session_id TEXT,strategy_version TEXT,details_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS metrics_time ON metrics(name,utc DESC);
        CREATE TABLE IF NOT EXISTS trade_outcomes(position_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,direction TEXT,opened REAL,closed REAL,pnl REAL,mfe REAL,mae REAL,candidate_id TEXT,strategy_version TEXT,payload_json TEXT NOT NULL);
        """)

def context():
    return os.environ.get("ATLAS_SESSION_ID", "manual"), os.environ.get("ATLAS_STRATEGY_VERSION", "unversioned")

def config_identity(config):
    canonical=_json(config); digest=hashlib.sha256(canonical.encode()).hexdigest()
    version=str(config.get("strategy_version","1.0.0"))+"+"+digest[:8]
    with connect() as db: db.execute("INSERT OR IGNORE INTO config_versions VALUES(?,?,?,?)",(digest,version,time.time(),canonical))
    return {"version":version,"config_hash":digest}

def put_state(key,value):
    with connect() as db: db.execute("INSERT INTO kv VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated",(key,_json(value),time.time()))

def get_state(key,default=None):
    with connect() as db: row=db.execute("SELECT value FROM kv WHERE key=?",(key,)).fetchone()
    return json.loads(row["value"]) if row else default

def save_basket(basket):
    with connect() as db:
        db.execute("INSERT INTO baskets VALUES(?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET updated=excluded.updated,status=excluded.status,payload_json=excluded.payload_json",(basket['candidate_id'],time.time(),basket.get('status','OPEN'),_json(basket)))

def recent_baskets(limit=50):
    with connect() as db:
        rows=db.execute('SELECT payload_json FROM baskets ORDER BY updated DESC LIMIT ?',(limit,)).fetchall()
    return [json.loads(r[0]) for r in rows]

def begin_session(config,details=None):
    ident=config_identity(config); sid=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")+uuid.uuid4().hex[:8]
    with connect() as db: db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",(sid,time.time(),None,"RUNNING",ident["version"],ident["config_hash"],_json(details or {})))
    return sid,ident

def end_session(sid,status="STOPPED",details=None):
    with connect() as db: db.execute("UPDATE sessions SET ended=?,status=?,details_json=? WHERE session_id=?",(time.time(),status,_json(details or {}),sid))

def record_event(source,event,payload=None,timestamp=None):
    sid,ver=context(); payload=payload or {}
    with connect() as db: db.execute("INSERT INTO events(utc,source,event,session_id,strategy_version,symbol,payload_json) VALUES(?,?,?,?,?,?,?)",(timestamp or time.time(),source,event,sid,ver,payload.get("symbol"),_json(payload)))

def record_transition(component, to_state, from_state=None, reason=None, payload=None):
    """Persist a lifecycle transition for diagnostics and exact replay."""
    sid, ver = context(); payload = payload or {}
    with connect() as db:
        db.execute("INSERT INTO state_transitions(utc,component,from_state,to_state,reason,session_id,strategy_version,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                   (time.time(), component, from_state, to_state, reason, sid, ver, _json(payload)))

def recent_transitions(limit=100):
    with connect() as db:
        rows = db.execute("SELECT * FROM state_transitions ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) | {"payload": json.loads(r["payload_json"]), "utc_iso": utc_iso(r["utc"])} for r in rows]

def recent_events(limit=100):
    with connect() as db: rows=db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",(int(limit),)).fetchall()
    return [{**json.loads(r["payload_json"]),"utc":utc_iso(r["utc"]),"event":r["event"],"source":r["source"],"strategy_version":r["strategy_version"]} for r in rows]

def record_ai_review(result,replay):
    sid,ver=context()
    with connect() as db: db.execute("INSERT INTO ai_reviews(utc,candidate_id,symbol,model,approved,session_id,strategy_version,payload_json,replay_json) VALUES(?,?,?,?,?,?,?,?,?)",(time.time(),result.get("candidate_id"),result.get("symbol"),result.get("model"),int(bool(result.get("approved"))),sid,ver,_json(result),_json(replay)))

def recent_ai_reviews(limit=50,replay=False):
    field="replay_json" if replay else "payload_json"
    with connect() as db: rows=db.execute(f"SELECT {field} FROM ai_reviews ORDER BY id DESC LIMIT ?",(int(limit),)).fetchall()
    return [json.loads(r[field]) for r in rows]

def add_watchlist(item):
    cid=item.get("candidate_id") or hashlib.sha256(_json(item).encode()).hexdigest(); sid,ver=context()
    stamp=item.get("signal_utc",time.time())
    if isinstance(stamp,str): stamp=datetime.fromisoformat(stamp.replace("Z","+00:00")).timestamp()
    with connect() as db: db.execute("""INSERT OR IGNORE INTO watchlist(candidate_id,signal_utc,symbol,direction,timeframe,reference_price,horizon_minutes,status,outcome,reason,session_id,strategy_version,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(cid,float(stamp),item["symbol"],item.get("direction","NONE"),item.get("timeframe","M15"),item.get("reference_price"),int(item.get("horizon_minutes",60)),"PENDING","PENDING",item.get("reason"),sid,ver,_json(item)))

def pending_watchlist(limit=100):
    with connect() as db: rows=db.execute("SELECT * FROM watchlist WHERE status='PENDING' ORDER BY signal_utc LIMIT ?",(int(limit),)).fetchall()
    return [dict(r)|{"payload":json.loads(r["payload_json"])} for r in rows]

def score_watchlist(cid,outcome,pnl,mfe,mae,details=None):
    with connect() as db:
        row=db.execute("SELECT payload_json FROM watchlist WHERE candidate_id=?",(cid,)).fetchone(); payload=json.loads(row[0]) if row else {}; payload["score_details"]=details or {}
        db.execute("UPDATE watchlist SET evaluated_utc=?,status='SCORED',outcome=?,final_pnl=?,mfe=?,mae=?,payload_json=? WHERE candidate_id=?",(time.time(),outcome,pnl,mfe,mae,_json(payload),cid))

def recent_watchlist(limit=100):
    with connect() as db: rows=db.execute("SELECT * FROM watchlist ORDER BY id DESC LIMIT ?",(int(limit),)).fetchall()
    return [{k:r[k] for k in r.keys() if k!="payload_json"}|{"payload":json.loads(r["payload_json"])} for r in rows]

def decision_comparison(limit=5000):
    """Equal-horizon, hypothetical candidate outcomes; never actual basket P&L."""
    from zoneinfo import ZoneInfo
    with connect() as db:
        rows=db.execute("""SELECT w.signal_utc,w.symbol,w.outcome,w.final_pnl,w.payload_json,a.approved
            FROM watchlist w JOIN ai_reviews a ON a.id=(
                SELECT MAX(id) FROM ai_reviews WHERE candidate_id=w.candidate_id)
            WHERE w.status='SCORED' ORDER BY w.id DESC LIMIT ?""",(int(limit),)).fetchall()
    groups={"decision":{},"pair":{},"spread":{},"hour_local":{},"volatility":{},"shadow_experiment":{}}
    def add(section,key,decision,pnl):
        bucket=groups[section].setdefault(key,{"approved":{"count":0,"wins":0,"pnl":0.0},"vetoed":{"count":0,"wins":0,"pnl":0.0}})[decision]
        bucket["count"]+=1; bucket["wins"]+=pnl>0; bucket["pnl"]+=pnl
    for row in rows:
        payload=json.loads(row["payload_json"])
        if (payload.get("score_details") or {}).get("scoring_version")!=2:
            continue
        decision="approved" if row["approved"] else "vetoed"
        pnl=float(row["final_pnl"] or 0)
        hour=datetime.fromtimestamp(row["signal_utc"],ZoneInfo("Asia/Beirut")).strftime("%H:00")
        spread=payload.get("spread_pips")
        spread_bucket="unknown" if spread is None else "≤1 pip" if float(spread)<=1 else "1–2 pips" if float(spread)<=2 else ">2 pips"
        experiment=payload.get("research_experiment") or {}
        experiment_bucket=("would keep" if experiment.get("would_keep") else "would skip") if experiment.get("name") and experiment.get("name")!="none" else "not recorded"
        for section,key in (("decision","all"),("pair",row["symbol"]),("spread",spread_bucket),("hour_local",hour),("volatility",payload.get("volatility_regime") or "unknown"),("shadow_experiment",experiment_bucket)):
            add(section,key,decision,pnl)
    for section in groups.values():
        for cohorts in section.values():
            for bucket in cohorts.values():
                n=bucket["count"]
                bucket["pnl"]=round(bucket["pnl"],2)
                bucket["average_pnl"]=round(bucket["pnl"]/n,2) if n else None
                bucket["win_rate"]=round(bucket["wins"]/n,3) if n else None
    sample=(groups["decision"].get("all") or {}).get("approved",{}).get("count",0)+(groups["decision"].get("all") or {}).get("vetoed",{}).get("count",0)
    return {"sample":sample,"horizon_minutes":60,"unit_lots":.01,"limitations":"Only new version-2 hypothetical fixed-horizon bid/ask results are shown. No commission, swap, slippage, basket exit, or causal adjustment. Small cohorts are descriptive only.","groups":groups}

def actual_basket_breakdown(limit=5000):
    """Closed, broker-reconciled basket P&L by conditions known at entry."""
    from zoneinfo import ZoneInfo
    with connect() as db:
        rows=db.execute("""SELECT b.payload_json,a.replay_json FROM baskets b
            LEFT JOIN ai_reviews a ON a.id=(SELECT MAX(id) FROM ai_reviews WHERE candidate_id=b.candidate_id)
            ORDER BY b.updated DESC LIMIT ?""",(int(limit),)).fetchall()
    groups={"pair":{},"spread":{},"hour_local":{},"volatility":{}}
    count=0
    for row in rows:
        basket=json.loads(row["payload_json"])
        if basket.get("status")!="CLOSED" or basket.get("realized_net_usd") is None:continue
        candidate=(json.loads(row["replay_json"]).get("candidate",{}) if row["replay_json"] else {})
        pnl=float(basket["realized_net_usd"]); count+=1
        opened=float(basket.get("opened_utc") or 0)
        hour=datetime.fromtimestamp(opened,ZoneInfo("Asia/Beirut")).strftime("%H:00") if opened else "unknown"
        spread=candidate.get("spread_pips")
        spread_bucket="unknown" if spread is None else "≤1 pip" if float(spread)<=1 else "1–2 pips" if float(spread)<=2 else ">2 pips"
        for section,key in (("pair",basket.get("symbol") or "unknown"),("spread",spread_bucket),("hour_local",hour),("volatility",candidate.get("volatility_regime") or "unknown")):
            item=groups[section].setdefault(key,{"count":0,"wins":0,"net":0.0})
            item["count"]+=1;item["wins"]+=pnl>0;item["net"]+=pnl
    for section in groups.values():
        for item in section.values():
            item["net"]=round(item["net"],2)
            item["average_net"]=round(item["net"]/item["count"],2)
            item["win_rate"]=round(item["wins"]/item["count"],3)
    return {"sample":count,"groups":groups,"limitations":"Realized broker-reconciled basket net. Conditions absent from earlier stored candidates are shown as unknown; small groups are descriptive only."}

def record_metric(component,name,duration_ms,ok=True,details=None):
    sid,ver=context()
    with connect() as db: db.execute("INSERT INTO metrics(utc,component,name,duration_ms,ok,session_id,strategy_version,details_json) VALUES(?,?,?,?,?,?,?,?)",(time.time(),component,name,float(duration_ms),int(bool(ok)),sid,ver,_json(details or {})))

def metric_summary(hours=24):
    with connect() as db: rows=db.execute("SELECT component,name,duration_ms,ok FROM metrics WHERE utc>=? ORDER BY id",(time.time()-hours*3600,)).fetchall()
    groups={}
    for r in rows:
        g=groups.setdefault(r["component"]+"."+r["name"],{"values":[],"failures":0}); g["values"].append(float(r["duration_ms"])); g["failures"]+=not bool(r["ok"])
    out={}
    for key,g in groups.items():
        values=sorted(g["values"]); pick=lambda p: values[min(len(values)-1,round((len(values)-1)*p))]
        out[key]={"count":len(values),"latest_ms":g["values"][-1],"p50_ms":pick(.5),"p95_ms":pick(.95),"failures":g["failures"]}
    return out

def latest_metric(component,name):
    with connect() as db:
        row=db.execute("SELECT utc,ok,details_json FROM metrics WHERE component=? AND name=? ORDER BY id DESC LIMIT 1",(component,name)).fetchone()
    return {"utc":row["utc"],"ok":bool(row["ok"]),"details":json.loads(row["details_json"])} if row else None

def recent_sessions(limit=20):
    with connect() as db: rows=db.execute("SELECT * FROM sessions ORDER BY started DESC LIMIT ?",(int(limit),)).fetchall()
    return [dict(r)|{"started_iso":utc_iso(r["started"]),"ended_iso":utc_iso(r["ended"]) if r["ended"] else None} for r in rows]

def session_replay(sid):
    with connect() as db:
        session=db.execute("SELECT * FROM sessions WHERE session_id=?",(sid,)).fetchone()
        if not session:return None
        cfg=db.execute("SELECT config_json FROM config_versions WHERE config_hash=?",(session["config_hash"],)).fetchone(); events=db.execute("SELECT * FROM events WHERE session_id=? ORDER BY id",(sid,)).fetchall(); reviews=db.execute("SELECT replay_json FROM ai_reviews WHERE session_id=? ORDER BY id",(sid,)).fetchall()
        transitions=db.execute("SELECT * FROM state_transitions WHERE session_id=? ORDER BY id",(sid,)).fetchall()
    return {"session":dict(session),"configuration":json.loads(cfg[0]) if cfg else None,"events":[{**json.loads(r["payload_json"]),"utc":utc_iso(r["utc"]),"source":r["source"],"event":r["event"]} for r in events],"transitions":[{**dict(r),"payload":json.loads(r["payload_json"]),"utc_iso":utc_iso(r["utc"])} for r in transitions],"reviews":[json.loads(r[0]) for r in reviews]}

def record_trade_outcome(item):
    item=dict(item)
    original_version=item.get('strategy_version')
    with connect() as db: db.execute("""INSERT INTO trade_outcomes(position_id,symbol,direction,opened,closed,pnl,mfe,mae,candidate_id,strategy_version,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(position_id) DO UPDATE SET closed=excluded.closed,pnl=excluded.pnl,payload_json=excluded.payload_json""",(str(item["position_id"]),item["symbol"],item.get("direction"),item.get("opened"),item.get("closed"),item.get("pnl"),item.get("mfe"),item.get("mae"),item.get("candidate_id"),context()[1],_json(item)))
    with connect() as db:
        db.execute('UPDATE trade_outcomes SET candidate_id=COALESCE(?,candidate_id),strategy_version=COALESCE(?,strategy_version),mfe=?,mae=? WHERE position_id=?',(item.get('candidate_id'),original_version,item.get('mfe'),item.get('mae'),str(item['position_id'])))

def recent_trade_outcomes(limit=50):
    with connect() as db: rows=db.execute("SELECT * FROM trade_outcomes ORDER BY closed DESC LIMIT ?",(int(limit),)).fetchall()
    return [dict(r)|{"payload":json.loads(r["payload_json"])} for r in rows]

def outcome_memory(limit=30):
    with connect() as db:
        watch=db.execute("SELECT symbol,direction,outcome,final_pnl,mfe,mae,reason FROM watchlist WHERE status='SCORED' ORDER BY evaluated_utc DESC LIMIT ?",(int(limit),)).fetchall()
        trades=db.execute("SELECT symbol,direction,pnl,mfe,mae,closed FROM trade_outcomes ORDER BY closed DESC LIMIT ?",(int(limit),)).fetchall()
    return {"scored_vetoes":[dict(r) for r in watch],"actual_trades":[dict(r) for r in trades]}

def add_postmortem(report, keep=200):
    reports=(get_state("postmortems",[]) or [])+[report]
    put_state("postmortems",reports[-keep:])

def recent_postmortems(limit=10):
    return (get_state("postmortems",[]) or [])[-int(limit):]

def migrate_legacy():
    if (get_state("legacy_migration",{}) or {}).get("complete"):return
    path=ROOT/"journal"/"events.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
            try:
                row=json.loads(line); stamp=datetime.fromisoformat(row.pop("utc").replace("Z","+00:00")).timestamp(); event=row.pop("event","legacy_event")
                record_event("legacy",event,row,stamp)
            except Exception:continue
    put_state("legacy_migration",{"complete":True,"utc":utc_iso()})

initialize()
