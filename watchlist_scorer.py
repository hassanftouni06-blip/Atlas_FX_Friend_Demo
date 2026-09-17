"""Scores every skipped candidate after its horizon using real MT5 prices."""
from __future__ import annotations
import json, math, msvcrt, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
WORKSPACE=ROOT.parent.parent
VENDOR=WORKSPACE/"work"/"python_packages"
if VENDOR.exists():sys.path.insert(0,str(VENDOR))
import MetaTrader5 as mt5
import storage

CONFIG=json.loads((ROOT/"config.json").read_text(encoding="utf-8"))
STOP=ROOT/"runtime"/"STOP_SCORER"

def score(item):
    symbol=item["symbol"]; start=float(item["signal_utc"]); end=start+int(item["horizon_minutes"])*60
    rates=mt5.copy_rates_range(symbol,mt5.TIMEFRAME_M1,datetime.fromtimestamp(start,timezone.utc),datetime.fromtimestamp(end,timezone.utc))
    if rates is None or len(rates)<2:return False
    info=mt5.symbol_info(symbol); entry=float(item["reference_price"] or rates[0]["open"]); direction=item["direction"]
    if not info or direction not in ("BUY","SELL"):return False
    # MT5 candles are bid prices. A short closes at ask; use each candle's
    # recorded spread rather than treating its bid close as a free short exit.
    ask=lambda r,field: float(r[field])+max(0,float(r["spread"]))*float(info.point)
    if direction=="BUY":
        final=float(rates[-1]["close"])
        best=max(float(r["high"]) for r in rates)
        worst=min(float(r["low"]) for r in rates)
    else:
        final=ask(rates[-1],"close")
        best=min(ask(r,"low") for r in rates)
        worst=max(ask(r,"high") for r in rates)
    kind=mt5.ORDER_TYPE_BUY if direction=="BUY" else mt5.ORDER_TYPE_SELL
    pnl=mt5.order_calc_profit(kind,symbol,float(CONFIG["lot_size"]),entry,final)
    mfe_pnl=mt5.order_calc_profit(kind,symbol,float(CONFIG["lot_size"]),entry,best)
    mae_pnl=mt5.order_calc_profit(kind,symbol,float(CONFIG["lot_size"]),entry,worst)
    if any(x is None or not math.isfinite(float(x)) for x in (pnl,mfe_pnl,mae_pnl)):return False
    outcome="WIN" if pnl>0 else "LOSS" if pnl<0 else "FLAT"
    storage.score_watchlist(item["candidate_id"],outcome,round(float(pnl),2),round(max(0,float(mfe_pnl)),2),round(max(0,-float(mae_pnl)),2),{"scoring_version":2,"bars":len(rates),"entry":entry,"final":final,"evaluated_utc":storage.utc_iso(),"method":"60m hypothetical 0.01-lot exit at bid/ask bar close; commission, swap and slippage excluded"})
    storage.record_event("scorer","watchlist_scored",{"symbol":symbol,"candidate_id":item["candidate_id"],"outcome":outcome,"pnl":round(float(pnl),2)})
    return True

def sync_closed_trades():
    start=datetime.now(timezone.utc).timestamp()-30*86400
    # Deal times use the broker clock, which runs ahead of UTC; an end of "now" would hide recent deals.
    end=datetime.now(timezone.utc).timestamp()+int(CONFIG["broker_timestamp_offset_seconds"])+3600
    history=mt5.history_deals_get(datetime.fromtimestamp(start,timezone.utc),datetime.fromtimestamp(end,timezone.utc))
    active=mt5.positions_get()
    if history is None or active is None: raise RuntimeError('Broker history/positions unavailable')
    deals=list(history)
    active_ids={p.identifier for p in active}
    links={identifier:b for b in storage.recent_baskets(10000) for identifier in b.get('identifiers',[])}
    entries={d.position_id:d for d in deals if d.magic==26091502 and d.entry==mt5.DEAL_ENTRY_IN}
    for deal in deals:
        # Manual closes carry magic 0, so ownership comes from the bot's entry deal.
        if deal.position_id not in entries or deal.entry not in (mt5.DEAL_ENTRY_OUT,mt5.DEAL_ENTRY_OUT_BY):continue
        if deal.position_id in active_ids: continue
        rows=[d for d in deals if d.position_id==deal.position_id]
        exits=[d for d in rows if d.entry in (mt5.DEAL_ENTRY_OUT,mt5.DEAL_ENTRY_OUT_BY)]
        if deal.ticket!=max(exits,key=lambda d:(d.time,d.ticket)).ticket: continue
        opened=entries.get(deal.position_id); direction="BUY" if opened and opened.type==mt5.DEAL_TYPE_BUY else "SELL" if opened else None
        net=sum(float(d.profit)+float(d.commission)+float(d.swap)+float(d.fee) for d in rows)
        mfe=mae=None
        if opened:
            rates=mt5.copy_rates_range(deal.symbol,mt5.TIMEFRAME_M1,datetime.fromtimestamp(opened.time,timezone.utc),datetime.fromtimestamp(deal.time,timezone.utc))
            if rates is not None and len(rates):
                entry=float(opened.price); kind=mt5.ORDER_TYPE_BUY if direction=="BUY" else mt5.ORDER_TYPE_SELL
                best=max(float(r["high"]) for r in rates) if direction=="BUY" else min(float(r["low"]) for r in rates); worst=min(float(r["low"]) for r in rates) if direction=="BUY" else max(float(r["high"]) for r in rates)
                best_pnl=mt5.order_calc_profit(kind,deal.symbol,float(opened.volume),entry,best); worst_pnl=mt5.order_calc_profit(kind,deal.symbol,float(opened.volume),entry,worst)
                mfe=round(max(0,float(best_pnl or 0)),2); mae=round(max(0,-float(worst_pnl or 0)),2)
        storage.record_trade_outcome({"position_id":deal.position_id,"symbol":deal.symbol,"closed":float(deal.time),"pnl":round(net,2),"direction":direction,"opened":float(opened.time) if opened else None,"mfe":mfe,"mae":mae,"deal_ticket":deal.ticket,"candidate_id":links.get(deal.position_id,{}).get('candidate_id'),"strategy_version":links.get(deal.position_id,{}).get('strategy_version')})

def main():
    os.environ["ATLAS_STRATEGY_VERSION"]=storage.config_identity(CONFIG)["version"]
    storage.record_event("scorer","started",{})
    while not STOP.exists():
        started=time.perf_counter(); ok=True; count=0
        try:
            if not mt5.initialize(path=CONFIG["terminal_path"]): raise RuntimeError(str(mt5.last_error()))
            sync_closed_trades()
            now=time.time()
            for item in storage.pending_watchlist():
                if now>=float(item["signal_utc"])+int(item["horizon_minutes"])*60 and score(item):count+=1
        except Exception as exc:
            ok=False; storage.record_event("scorer","error",{"message":str(exc)})
        finally: mt5.shutdown()
        storage.record_metric("scorer","cycle",(time.perf_counter()-started)*1000,ok,{"scored":count})
        print(f"scorer cycle: {'healthy' if ok else 'error'}; newly scored={count}",flush=True)
        for _ in range(30):
            if STOP.exists():break
            time.sleep(2)
    storage.record_event("scorer","stopped",{})

if __name__=="__main__":
    with (ROOT/"runtime"/"scorer.lock").open("a+b") as lock:
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise SystemExit("Outcome scorer is already running")
        main()
