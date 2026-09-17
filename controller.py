"""Resilient local control room for every Atlas FX component."""
import json, msvcrt, os, secrets, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import storage

ROOT=Path(__file__).resolve().parent; CONFIG=json.loads((ROOT/"config.json").read_text())
PORT=int(CONFIG["controller_port"]); TOKEN=secrets.token_urlsafe(24)
COMPONENTS={"portfolio":{"script":"portfolio_bot.py","lock":"portfolio.lock","stop":"STOP"},"scorer":{"script":"watchlist_scorer.py","lock":"scorer.lock","stop":"STOP_SCORER"},"monitor":{"script":"trade_monitor.py","lock":"monitor.lock","stop":"STOP_MONITOR"}}
PROCESSES={}; DESIRED=False; STARTED={}; RESTARTS={k:0 for k in COMPONENTS}; SESSION=None

def lock_active(name):
    filename=COMPONENTS[name]["lock"]
    if not filename:return False
    path=ROOT/"runtime"/filename; path.parent.mkdir(exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return True
        msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1); return False

def running(name):
    process=PROCESSES.get(name)
    return bool(process and process.poll() is None) or lock_active(name)

def start_component(name):
    if running(name):return
    spec=COMPONENTS[name]; (ROOT/"runtime"/spec["stop"]).unlink(missing_ok=True)
    env=os.environ.copy()
    if SESSION:env["ATLAS_SESSION_ID"]=SESSION[0]; env["ATLAS_STRATEGY_VERSION"]=SESSION[1]["version"]
    log=(ROOT/"runtime"/(name+".log")).open("a",encoding="utf-8",buffering=1)
    PROCESSES[name]=subprocess.Popen([sys.executable,"-u",str(ROOT/spec["script"])],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    STARTED[name]=time.time(); storage.record_event("controller","component_started",{"component":name,"pid":PROCESSES[name].pid})
    storage.record_transition("controller", "RUNNING", "STOPPED", "component started", {"component":name,"pid":PROCESSES[name].pid})

def start_all():
    global DESIRED,SESSION
    DESIRED=True
    if SESSION is None:
        SESSION=storage.begin_session(CONFIG,{"components":list(COMPONENTS),"symbols":list(CONFIG["symbols"])})
        os.environ["ATLAS_SESSION_ID"]=SESSION[0]; os.environ["ATLAS_STRATEGY_VERSION"]=SESSION[1]["version"]
    for name in COMPONENTS:start_component(name)

def stop_all():
    global DESIRED
    DESIRED=False
    for spec in COMPONENTS.values():(ROOT/"runtime"/spec["stop"]).touch()
    storage.record_event("controller","stop_requested",{})

def read_log(name):
    try:return (ROOT/"runtime"/(name+".log")).read_text(encoding="utf-8",errors="replace").splitlines()[-30:]
    except Exception:return []

def dashboard():
    state=storage.get_state("portfolio_state",{}) or {}; reviews=storage.recent_ai_reviews(30); watch=storage.recent_watchlist(50); events=storage.recent_events(80); scored=[x for x in watch if x["status"]=="SCORED"]
    try:calendar_cache=json.loads((ROOT/"runtime"/"calendar.json").read_text())
    except Exception:calendar_cache={}
    try:news=json.loads((ROOT/"runtime"/"news_context.json").read_text())
    except Exception:news={}
    scorer_metric=storage.latest_metric("scorer","cycle")
    calendar_age=time.time()-float(calendar_cache.get("fetched",0) or 0)
    service_health={"calendar":{"ok":bool(calendar_cache.get("events")) and 0<=calendar_age<21600,"age_minutes":round(calendar_age/60) if calendar_age>=0 else None},"scorer":{"ok":bool(scorer_metric and scorer_metric["ok"] and time.time()-scorer_metric["utc"]<180),"last_cycle_utc":scorer_metric["utc"] if scorer_metric else None}}
    return {"state":state,"reviews":reviews,"watchlist":watch,"events":events,"transitions":storage.recent_transitions(100),"trades":storage.recent_trade_outcomes(50),"calendar":calendar_cache.get("events",[]),"news":news,"service_health":service_health,"metrics":storage.metric_summary(),"decision_comparison":storage.decision_comparison(),"actual_basket_breakdown":storage.actual_basket_breakdown(),"postmortems":storage.recent_postmortems(10)[::-1],"outcomes":{"scored":len(scored),"wins":sum(x["outcome"]=="WIN" for x in scored),"losses":sum(x["outcome"]=="LOSS" for x in scored),"net":round(sum(float(x["final_pnl"] or 0) for x in scored),2)}}

def payload():
    components={name:{"running":running(name),"pid":getattr(PROCESSES.get(name),"pid",None),"restarts":RESTARTS[name],"started":STARTED.get(name),"logs":read_log(name)} for name in COMPONENTS}
    return {"running":all(x["running"] for x in components.values()),"desired":DESIRED,"components":components,"config":CONFIG,"baskets":storage.recent_baskets(),"session_id":SESSION[0] if SESSION else None,"updated":time.time(),**dashboard()}

class Handler(BaseHTTPRequestHandler):
    def send_data(self,code,data,kind="application/json"):
        body=data if isinstance(data,bytes) else json.dumps(data,allow_nan=False).encode(); self.send_response(code); self.send_header("Content-Type",kind); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path=="/":
            page=(ROOT/"controller_v2.html").read_text(encoding="utf-8").replace("__TOKEN__",TOKEN)
            return self.send_data(200,page.encode(),"text/html; charset=utf-8")
        if self.path=="/api/state":return self.send_data(200,payload())
        if self.path=="/api/replay/latest":
            rows=storage.recent_ai_reviews(1,True); return self.send_data(200,rows[0] if rows else {"ok":False,"message":"No AI replay yet"})
        if self.path.startswith("/api/session/"):return self.send_data(200,storage.session_replay(self.path.rsplit("/",1)[-1]) or {"error":"Session not found"})
        self.send_data(404,{"error":"Not found"})
    def do_POST(self):
        if self.headers.get("X-Atlas-Token")!=TOKEN:return self.send_data(403,{"error":"Forbidden"})
        try:data=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
        except ValueError:return self.send_data(400,{"error":"Invalid JSON"})
        action=data.get("action")
        if action=="start":start_all()
        elif action=="stop":stop_all()
        elif action=="pause":(ROOT/"runtime"/"PAUSE").touch()
        elif action=="resume":(ROOT/"runtime"/"PAUSE").unlink(missing_ok=True)
        elif action=="shutdown":
            # An open trade must never be left without its live-trade agent.
            saved=storage.get_state("portfolio_state",{}) or {}
            if saved.get("baskets") or saved.get("profit_basket"):
                return self.send_data(409,{"error":"A trade is open; wait until it closes"})
            stop_all()
            threading.Thread(target=shutdown_when_idle,daemon=True).start()
        else:return self.send_data(400,{"error":"Unknown action"})
        self.send_data(200,{"ok":True})
    def log_message(self,*_):pass

def shutdown_when_idle():
    deadline=time.time()+90
    while time.time()<deadline and any(running(x) for x in COMPONENTS):
        time.sleep(1)
    if SESSION:storage.end_session(SESSION[0])
    storage.record_event("controller","shutdown",{})
    os._exit(0)

def supervise():
    global SESSION
    while True:
        if DESIRED:
            for name in COMPONENTS:
                process=PROCESSES.get(name)
                if process and process.poll() is not None:
                    RESTARTS[name]+=1; storage.record_event("controller","component_exited",{"component":name,"exit_code":process.returncode,"restart":RESTARTS[name]}); storage.record_transition("controller", "RECOVERING", "RUNNING", "component exited", {"component":name,"exit_code":process.returncode}); PROCESSES.pop(name,None)
                if not running(name):
                    delay=min(30,2**min(RESTARTS[name],4))
                    if time.time()-STARTED.get(name,0)>=delay:
                        try:start_component(name)
                        except Exception as exc:storage.record_event("controller","restart_failed",{"component":name,"message":str(exc)})
        elif SESSION and not any(running(x) for x in COMPONENTS):storage.end_session(SESSION[0]); SESSION=None
        time.sleep(3)

if __name__=="__main__":
    storage.initialize(); storage.migrate_legacy(); (ROOT/"runtime").mkdir(exist_ok=True)
    with (ROOT/"runtime"/"controller.lock").open("a+b") as controller_lock:
        controller_lock.seek(0)
        try:msvcrt.locking(controller_lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise SystemExit("Atlas FX controller is already running")
        threading.Thread(target=supervise,daemon=True).start(); ThreadingHTTPServer(("127.0.0.1",PORT),Handler).serve_forever()
