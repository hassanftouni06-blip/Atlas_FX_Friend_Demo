"""Live-trade agent. Reads the open trade and its chart; it can only ask the portfolio bot to close it or tighten its stop."""
from __future__ import annotations
import json, msvcrt, os, time
from datetime import datetime, timezone

import storage
from portfolio_bot import CONFIG_PATH, MAGIC, RUNTIME, ai_call, atomic_json, chart, load_json, monitor_request_path, pip_size, validate_bar_series
import MetaTrader5 as mt5

STOP = RUNTIME / "STOP_MONITOR"
SCHEMA = {"type":"object","properties":{
    "decision":{"type":"string","enum":["HOLD","CLOSE","TIGHTEN"]},
    "new_stop":{"type":"number"},
    "confidence":{"type":"number"},"reason":{"type":"string"}},
    "required":["decision","new_stop","confidence","reason"]}


def log(event, **payload):
    storage.record_event("monitor", event, payload)
    print(event + ": " + json.dumps(payload, separators=(",", ":")), flush=True)


def count_request():
    usage = storage.get_state("monitor_usage", {}) or {}
    today = datetime.now(timezone.utc).date().isoformat()
    if usage.get("date") != today:
        usage = {"date": today, "requests": 0}
    usage["requests"] = int(usage.get("requests", 0)) + 1
    storage.put_state("monitor_usage", usage)


def prompt(facts, config):
    steps = ", ".join(f"{s['close_positions']} at +${s['usd_per_position']:g} each" for s in config["scale_out"])
    return ("You are the objective live-trade agent of a demo forex desk. A multi-position trade is already open; you cannot open, add to "
            "or resize trades. Your powers: HOLD; CLOSE the whole trade now; or TIGHTEN, moving the stop closer to price (set new_stop "
            "to an exact price between the current stop and the current price; the code ignores any stop that is not tighter). "
            "The code already enforces the owner's rules on its own: one free dip is allowed; after that the trade may not lose again "
            f"(it closes near breakeven); profit is taken in steps ({steps}) and the last position runs "
            "with a trailing stop; spread blowouts and sudden bursts against the trade close it. Your job is judgement the code cannot "
            "make: close or tighten earlier when the closed candles clearly show the move failing or reversing, and HOLD while it is "
            "intact or unclear. During the free dip (phase FREE or DIPPED) the code only accepts your request if it measured danger "
            "in the last two minutes. The leader's reason for entering is in the facts; judge whether that thesis still holds. "
            "Do not invent prices. Set new_stop to 0 unless you choose TIGHTEN. Facts: " + json.dumps(facts, separators=(",", ":")))


def rows(rates):
    return [{k: float(x[k]) for k in ("open", "high", "low", "close")} for x in rates]


def open_trades():
    state = storage.get_state("portfolio_state", {}) or {}
    baskets = dict(state.get("baskets") or {})
    if state.get("profit_basket"):
        baskets[state["profit_basket"]["candidate_id"]] = state["profit_basket"]
    return list(baskets.values())


def cycle(config, basket, last_reviewed):
    if basket.get("status") != "OPEN" or basket.get("closing"):
        return last_reviewed
    phase = basket.get("phase", "FREE")
    danger = basket.get("danger") or {}
    danger_recent = bool(danger) and time.time() - float(danger.get("utc", 0)) <= 120
    # The free dip is hands-off unless the bot measured danger.
    if phase != "ARMED" and not danger_recent:
        return last_reviewed
    if not mt5.initialize(path=config["terminal_path"]):
        raise RuntimeError("MT5 connection failed: " + str(mt5.last_error()))
    try:
        symbol = basket["symbol"]
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            raise RuntimeError("Could not read open positions")
        mine = [p for p in positions if p.magic == MAGIC and p.ticket in basket["tickets"]]
        if not mine:
            return last_reviewed
        m1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, 60)
        m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 1, 36)
        if m1 is None or m5 is None or len(m1) < 20 or len(m5) < 20:
            raise RuntimeError(f"Chart history unavailable for {symbol}")
        validate_bar_series(list(m1), symbol, "M1", 240, int(config["broker_timestamp_offset_seconds"]))
        key = (basket["candidate_id"], int(m1[-1]["time"]), bool(danger_recent))
        if key == last_reviewed:
            return last_reviewed
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or info is None:
            raise RuntimeError(f"No quote for {symbol}")
        floating = sum(float(p.profit) + float(p.swap) for p in mine)
        volume = sum(float(p.volume) for p in mine)
        facts = {"symbol": symbol, "direction": basket["direction"], "phase": phase,
                 "open_positions": len(mine), "take_profits_hit": basket.get("targets_done", 0),
                 "average_entry": round(sum(float(p.price_open)*float(p.volume) for p in mine)/volume, 6),
                 "bid": float(tick.bid), "ask": float(tick.ask), "spread_pips": round((float(tick.ask)-float(tick.bid))/pip_size(info), 2),
                 "current_stops": sorted({float(p.sl) for p in mine}), "targets": sorted({float(p.tp) for p in mine}),
                 "open_profit_usd": round(floating, 2), "whole_trade_usd_incl_closed": basket.get("floating_usd"),
                 "best_usd": basket.get("peak_usd"), "worst_usd": basket.get("worst_usd"),
                 "recent_danger": danger if danger_recent else None,
                 "leader_entry_reason": (basket.get("entry_context") or {}).get("reason"),
                 "minutes_open": round((time.time()-float(basket.get("opened_utc", time.time())))/60, 1),
                 "last_15_closed_1min_candles": rows(m1[-15:])}
        count_request()
        images = [chart(rows(m1), symbol + " closed M1 (open trade)"), chart(rows(m5), symbol + " closed M5 (open trade)")]
        try:
            result, _ = ai_call("trade_monitor", prompt(facts, config), images, SCHEMA, config, symbol, config.get("monitor_model"))
        except Exception as exc:
            log("review_failed", candidate_id=basket["candidate_id"], message=str(exc)[:300])
            return key
        decision = result.get("decision")
        log("monitor_review", symbol=symbol, candidate_id=basket["candidate_id"], phase=phase, open_profit_usd=round(floating, 2),
            decision=decision, new_stop=result.get("new_stop"), confidence=result.get("confidence"), reason=str(result.get("reason", ""))[:300])
        if decision in ("CLOSE", "TIGHTEN") and float(result["confidence"]) >= float(config.get("monitor_min_confidence", 0.6)):
            if decision == "TIGHTEN" and not float(result.get("new_stop") or 0):
                return key
            atomic_json(monitor_request_path(basket["candidate_id"]), {"candidate_id": basket["candidate_id"], "utc": time.time(), "type": decision,
                                          "stop": float(result.get("new_stop") or 0), "reason": str(result.get("reason", ""))[:200],
                                          "confidence": result["confidence"]})
            log("request_sent", symbol=symbol, candidate_id=basket["candidate_id"], type=decision, new_stop=result.get("new_stop"))
        return key
    finally:
        mt5.shutdown()


def main():
    os.environ["ATLAS_STRATEGY_VERSION"] = storage.config_identity(load_json(CONFIG_PATH, {}))["version"]
    log("started")
    last_reviewed = {}
    while not STOP.exists():
        config = load_json(CONFIG_PATH, {})
        wait = 3
        if config.get("trade_monitor_enabled", True):
            trades = open_trades()
            last_reviewed = {k: v for k, v in last_reviewed.items() if k in {b["candidate_id"] for b in trades}}
            for basket in trades:
                try:
                    last_reviewed[basket["candidate_id"]] = cycle(config, basket, last_reviewed.get(basket["candidate_id"]))
                except Exception as exc:
                    log("error", candidate_id=basket.get("candidate_id"), message=str(exc)[:300])
                    wait = 10
        for _ in range(wait):
            if STOP.exists():
                break
            time.sleep(1)
    log("stopped")


if __name__ == "__main__":
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "monitor.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("Live-trade agent is already running")
        main()
