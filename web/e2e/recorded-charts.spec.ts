import { expect, test, type Page } from "@playwright/test";

const base = 1_800_000_000_000;
const risk = {
  mode: "paper", market_type: "spot", bot_enabled: false,
  spot_capital_usdt: 1000, futures_capital_usdt: 0,
  risk_per_trade_pct: 0.25, daily_loss_limit_pct: 1.5, max_drawdown_pct: 5,
  max_open_positions: 3, max_leverage: 1, stop_loss_required: true,
  manual_approval: true, allowed_symbols: ["BTCUSDT", "ETHUSDT"],
};

function candles(withGap = false) {
  const offsets = withGap ? [0, 1, 3] : [0, 1, 2];
  return offsets.map((i) => {
    const opening = base + i * 60_000;
    return {
      open_time_ms: opening, close_time_ms: opening + 59_999,
      open: "100.0", high: "110.0", low: "90.0",
      close: ["100.0", "102.0", "103.0"][Math.min(i, 2)], volume: "9.0",
    };
  }).reverse();
}

const journal = [
  { id: 3, event_type: "spot_sell", created_at: "2026-10-21T07:05:00+00:00",
    cash_delta_usdt: "102.00", realized_pnl_delta_usdt: "1.75",
    fee_usdt: "0.1", quote: null },
  { id: 2, event_type: "spot_buy", created_at: "2026-10-21T07:04:00+00:00",
    cash_delta_usdt: "-100.50", realized_pnl_delta_usdt: "0",
    fee_usdt: "0.1", quote: null },
];
const dashboard = {
  mode: "paper", source: "persisted_paper_spot", initialized: true,
  account: {initial_capital_usdt: "1000", cash_usdt: "1001.75",
    realized_pnl_usdt: "1.75", paid_fees_usdt: "0.2"},
  total_positions: 1, open_positions: 0, total_events: 3,
  positions: [], events: journal,
  worker: {requested_enabled: false, process_verified_running: false,
    last_status: "no_signal", last_attempt_at_ms: base,
    next_allowed_at_ms: base + 60_000, consecutive_failures: 0},
};

async function fixture(page: Page, opts: {gap?: boolean; empty?: boolean} = {}) {
  await page.route("http://127.0.0.1:8000/api/v1/**", async route => {
    const url = new URL(route.request().url());
    const cors = {
      "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
      "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
      "Access-Control-Allow-Headers": "X-Admin-Token, Content-Type",
      "Cache-Control": "no-store",
    };
    if (route.request().method() === "OPTIONS") {
      await route.fulfill({status: 204, headers: cors});
      return;
    }
    const body = url.pathname === "/api/v1/settings"
      ? { version: 1, settings: risk }
      : url.pathname === "/api/v1/paper/dashboard"
        ? opts.empty ? {...dashboard, initialized:false, account:null,
            positions:[], events:[], total_positions:0, total_events:0}
          : dashboard
        : url.pathname === "/api/v1/market/candles"
          ? {source:"stored_public_spot", symbol: "BTCUSDT", interval: "1m",
            candles: opts.empty ? [] : candles(opts.gap)}
          : {events:[]};
    await route.fulfill({status: 200, contentType:"application/json",
      headers:cors, body:JSON.stringify(body)});
  });
}

async function connect(page: Page) {
  await page.goto("/");
  await page.getByRole("button",{name:"API Bağlan"}).click();
  await page.getByLabel("Yerel yönetici anahtarı").fill("test-only-mock-token");
  await page.getByRole("button",{name:"Bağlan",exact:true}).click();
  await expect(page.getByRole("status")).toContainText("Yerel API bağlantısı kuruldu");
}
async function navigate(page: Page, name:string, width:number) {
  if (width <= 780) await page.getByRole("button",{name:"Menüyü aç"}).click();
  await page.getByRole("button",{name}).click();
}

for (const width of [360, 390, 768, 1280]) {
  test(`PAPER historical and realized charts use only fixture database records at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width,height:850});
    await fixture(page);
    await connect(page);
    const realized = page.getByTestId("realized-trade-chart");
    await expect(realized).toBeVisible();
    await expect(realized.getByText("1.75 USDT")).toBeVisible();
    await expect(realized.getByText("kümülatif PnL değil", {exact:false})).toBeVisible();
    await expect(page.getByText("Çalışan servis doğrulanmadı.",{exact:false})).toBeVisible();
    await navigate(page,"Piyasa Verileri",width);
    await page.getByRole("button",{name:"Geçmişi Göster"}).click();
    const spot=page.getByTestId("spot-close-chart");
    await expect(spot).toBeVisible();
    await expect(spot.getByText("100.0 USDT")).toBeVisible();
    await expect(spot.getByText("103.0 USDT")).toBeVisible();
    await expect(spot.getByRole("img")).toHaveCount(1);
    const overflow = await page.evaluate(()=>
      document.documentElement.scrollWidth-document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await info.attach(`paper-chart-${width}.png`, {
      body: await page.screenshot({fullPage:true}), contentType:"image/png",
    });
  });
}

test("missing closed candle never draws a fabricated connecting price line",async ({page})=>{
  await fixture(page,{gap:true});
  await connect(page);
  await navigate(page,"Piyasa Verileri",1280);
  await page.getByRole("button",{name:"Geçmişi Göster"}).click();
  await expect(page.getByTestId("spot-chart-empty")).toBeVisible();
  await expect(page.getByTestId("spot-close-chart")).toHaveCount(0);
  await expect(page.getByText("Eksik aralıklar birleştirilmez.",{exact:false})).toBeVisible();
});

test("empty persisted data never produces price or realized-PnL graphs",async ({page})=>{
  await fixture(page,{empty:true});
  await connect(page);
  await expect(page.getByTestId("realized-trade-chart")).toHaveCount(0);
  await navigate(page,"Piyasa Verileri",1280);
  await page.getByRole("button",{name:"Geçmişi Göster"}).click();
  await expect(page.getByTestId("spot-close-chart")).toHaveCount(0);
  await expect(page.getByText("Henüz kayıtlı mum verisi yok")).toBeVisible();
});
