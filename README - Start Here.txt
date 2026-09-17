ATLAS FX DEMO — FRIEND COPY

1. Install MetaTrader 5 and sign in to your own USD-denominated DEMO account.
   This package refuses live accounts. Enable algorithmic trading and the
   external Python API in MT5. Keep MT5 running on the same Windows laptop.
2. Install Python 3.12 from python.org if it is not already installed.
3. Double-click "Start Demo.cmd". On the first run it installs its own Python
   packages, asks for YOUR AI API key (OpenAI by default), and checks your MT5 demo account.
   No code or configuration file editing is needed. It saves the key encrypted
   for your Windows user account; do not share the .secrets folder.
4. In the control room, press "Start everything". Confirm calendar and scorer
   health. No new trades are allowed when the calendar is unavailable.

Six exact broker symbols are required: EURUSD, USDJPY, GBPUSD, USDCHF,
USDCAD, NZDUSD. If your demo broker does not provide these exact symbols,
the setup stops instead of guessing or silently substituting instruments.
The computer and MT5 must remain awake and connected. Broker spreads,
quotes, permissions and AI quota can differ from the original account.
This is an unproven DEMO strategy, not a promise of profit.

The AI provider is set by "ai_provider" in config.json (openai or gemini).
To use a different key, double-click "Change AI Key.cmd". Never send the
encrypted key files in .secrets to another user.
