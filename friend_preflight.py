"""Read-only MT5 checks and local demo-account binding for a clean shared copy."""
import json
import sys
from pathlib import Path

import MetaTrader5 as mt5

root = Path(__file__).resolve().parent
config_path = root / "config.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
if config.get("execution_mode") != "DEMO_ONLY":
    raise SystemExit("Safety lock: this package must stay DEMO_ONLY.")
if not mt5.initialize():
    raise SystemExit(f"Open and sign in to MT5 first: {mt5.last_error()}")
try:
    terminal, account = mt5.terminal_info(), mt5.account_info()
    if not terminal or not terminal.connected or not account:
        raise SystemExit("MT5 is not connected to an account.")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("Safety lock: connect a DEMO account, not a live account.")
    if account.currency != "USD":
        raise SystemExit("This USD-risk version needs an MT5 demo account denominated in USD.")
    terminal_path = Path(terminal.path) / "terminal64.exe"
    if not terminal_path.is_file():
        raise SystemExit("Could not locate the running MT5 terminal64.exe.")
    missing = []
    for symbol in config["symbols"]:
        if mt5.symbol_info(symbol) is None or not mt5.symbol_select(symbol, True):
            missing.append(symbol)
    if missing:
        raise SystemExit("These exact broker symbols are unavailable: " + ", ".join(missing))
    config["terminal_path"] = str(terminal_path)
    config["required_server"] = account.server
    config["required_currency"] = account.currency
    config["required_leverage"] = int(account.leverage)
    config["required_login"] = int(account.login)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Demo verified: account {account.login}, {account.server}, balance {account.balance:.2f} USD, 1:{account.leverage}; six symbols available.")
    if terminal.tradeapi_disabled or not terminal.trade_allowed:
        print("Enable algorithmic trading and the external Python API in MT5 before starting trades.")
finally:
    mt5.shutdown()
