# TradePilot Mobile Console

BT-002 development-only responsive panel.

Run backend locally as described by the root README. Set unique ADMIN_TOKEN and POSTGRES_PASSWORD in your untracked .env file. Then run:

    cd web
    npm install
    npm run dev

Visit http://127.0.0.1:3000 in your desktop browser, choose API connection, enter the development token, and read/write paper risk settings on http://127.0.0.1:8000.

There is no Binance private account API or trade execution. Values are simulated limits, not real balances.

Do NOT expose the API publicly; its temporary dev token is not a production login. Mobile layout can be previewed with browser responsive mode. Real phone access requires a separately secured HTTPS deployment and identity/authentication hardening.

Validation: npm run typecheck; npm run build.

Mobile-browser smoke tests: npm run test:e2e (requires npx playwright install chromium).

## Stored Spot market history

The **Piyasa Verileri** menu reads closed historical candles already stored in
PostgreSQL through the authenticated local API. The symbol and interval filters
are BTCUSDT/ETHUSDT and 1m/5m/15m. An empty database displays a clear
empty state; the dashboard does not claim live data or generate fake prices.
The market screen never sends Binance orders or authenticates to Binance.
