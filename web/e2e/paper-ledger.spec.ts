import { expect, test } from "@playwright/test";

for (const width of [360, 390, 768, 1280]) {
  test(`paper positions are explicitly disconnected at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 820 });
    await page.goto("/");
    if (width <= 780) await page.getByRole("button", { name: "Menüyü aç" }).click();
    await page.getByRole("button", { name: "Pozisyonlar" }).click();
    await expect(page.getByRole("heading", { name: "Kalıcı PAPER Spot Defteri" })).toBeVisible();
    await expect(page.getByText("Yerel API bağlantısı gerekiyor")).toBeVisible();
    await expect(page.getByRole("button", { name: "Hesabı Yenile" })).toBeDisabled();
    await expect(page.getByText("Canlı fiyat, toplam", { exact: false })).toBeVisible();
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
}

test("connected paper ledger displays empty persisted account without invented cash", async ({ page }) => {
  test.skip(!process.env.E2E_CONNECTED, "Requires isolated Docker backend");
  const token = process.env.E2E_ADMIN_TOKEN;
  if (!token) throw new Error("E2E_ADMIN_TOKEN must be set");

  await page.setViewportSize({ width: 390, height: 820 });
  await page.goto("/");
  await page.getByRole("button", { name: "API Bağlan" }).click();
  await page.getByLabel("Yerel yönetici anahtarı").fill(token);
  await page.getByRole("button", { name: "Bağlan", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Yerel API bağlantısı kuruldu");
  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Pozisyonlar" }).click();
  await expect(page.getByText("Henüz PAPER Spot hesabı oluşturulmamış")).toBeVisible();
  await expect(page.getByText("Başlangıç Sermayesi (Sanal)")).toHaveCount(0);
  await expect(page.getByText("Kayıtlı Nakit (Sanal)")).toHaveCount(0);
  await page.getByRole("button", { name: "Hesabı Yenile" }).click();
  await expect(page.getByText("Henüz PAPER Spot hesabı oluşturulmamış")).toBeVisible();
  const result = await page.request.get("http://127.0.0.1:8000/api/v1/paper/spot", {
    headers: { "X-Admin-Token": token },
  });
  expect(result.status()).toBe(200);
  expect((await result.json()).initialized).toBe(false);
});

test("ledger displays exact persisted gross/net values and recorded costs without estimating spread", async ({ page }) => {
  const origin = "http://127.0.0.1:8000";
  const cors = { "Access-Control-Allow-Origin": "http://127.0.0.1:3000", "Access-Control-Allow-Headers": "X-Admin-Token, Content-Type", "Cache-Control": "no-store" };
  const settings = { mode: "paper", market_type: "spot", bot_enabled: false,
    spot_capital_usdt: 1000, futures_capital_usdt: 0, risk_per_trade_pct: 0.25,
    daily_loss_limit_pct: 1.5, max_drawdown_pct: 5, max_open_positions: 3,
    max_leverage: 1, stop_loss_required: true, manual_approval: true, allowed_symbols: ["BTCUSDT"] };
  await page.route(`${origin}/api/v1/**`, async route => {
    const url = new URL(route.request().url());
    const body = url.pathname === "/api/v1/settings"
      ? { version: 7, settings }
      : url.pathname === "/api/v1/paper/spot"
        ? { mode: "paper", market_type: "spot", source: "persisted_paper_spot", initialized: true,
            account: { mode: "paper", market_type: "spot", settings_version: 7,
              initial_capital_usdt: "1000.00", cash_usdt: "899.70", realized_pnl_usdt: "-13.00",
              paid_fees_usdt: "3.00", created_at: "2026-10-23T00:00:00Z" },
            total_positions: 1, open_positions: 0, total_events: 1,
            positions: [{ trade_id: "paper-trade-1", symbol: "BTCUSDT", status: "closed",
              quantity: "1.00000000", entry_fill_price: "100.00", entry_notional_usdt: "100.00",
              entry_fee_usdt: "1.00", stop_price: "95.00", estimated_stop_loss_usdt: "5.10",
              exit_fill_price: "99.00", exit_fee_usdt: "2.00", net_proceeds_usdt: "98.80",
              realized_pnl_usdt: "-13.00", gross_realized_pnl_usdt: "-10.00",
              opened_at: "2026-10-22T00:00:00Z", closed_at: "2026-10-23T00:00:00Z" }],
            events: [{ id: 1, event_type: "spot_sell", position_id: 1, cash_delta_usdt: "98.80",
              balance_after_usdt: "899.70", fee_usdt: "2.00", realized_pnl_delta_usdt: "-13.00",
              quantity_delta: "-1.00000000", created_at: "2026-10-23T00:00:00Z",
              observed_quote: { source: "binance_public_spot_ticker_price", symbol: "BTCUSDT",
                interval: "1m", observed_at_ms: 1792713600000, reference_price: "99.00",
                fee_bps: 10, slippage_bps: 5, spread_bps: null } }] }
        : { mode: "paper", source: "persisted_paper_spot", initialized: false,
            account: null, total_positions: 0, open_positions: 0, total_events: 0,
            positions: [], events: [], worker: { requested_enabled: false,
              process_verified_running: false, last_status: null, last_attempt_at_ms: null,
              next_allowed_at_ms: null, consecutive_failures: 0 } };
    await route.fulfill({ status: 200, contentType: "application/json", headers: cors, body: JSON.stringify(body) });
  });
  await page.setViewportSize({ width: 390, height: 820 });
  await page.goto("/");
  await page.getByRole("button", { name: "API Bağlan" }).click();
  await page.getByLabel("Yerel yönetici anahtarı").fill("test-only-local-token");
  await page.getByRole("button", { name: "Bağlan", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Yerel API bağlantısı kuruldu");
  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Pozisyonlar" }).click();
  const trade = page.locator(".paper-ledger-record");
  await expect(page.getByText("Brüt gerçekleşmiş PnL (sanal)")).toBeVisible();
  await expect(trade.getByText("-10.00 USDT", { exact: true })).toBeVisible();
  await expect(page.getByText("Net gerçekleşmiş PnL (sanal)")).toBeVisible();
  await expect(trade.getByText("-13.00 USDT", { exact: true })).toBeVisible();
  await expect(page.getByText(/spread bilinmiyor/)).toBeVisible();
});
