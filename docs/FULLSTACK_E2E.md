# Connected UI → API → PostgreSQL E2E

The public repository CI uses **disposable** local Compose services and demo-only credentials. It starts the Paper-only API and PostgreSQL migrations, uses Chromium at 390px, connects to the dev API, edits Spot and Futures paper capital limits, saves them, verifies audit history, then checks the dashboard display.

The test does not use real Binance credentials, market balances or live trading permissions. The UI test is skipped in the normal visual-only CI run; it executes in its own isolated workflow by setting `E2E_CONNECTED=1`.

Never use the fixture admin token for a deployed installation; production user login and HTTPS remain future tasks.
