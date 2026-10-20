import { expect, test } from "@playwright/test";

test("connected paper risk settings persist from mobile panel into PostgreSQL", async ({ page }) => {
  test.skip(!process.env.E2E_CONNECTED, "Requires isolated Docker backend");
  const adminToken = process.env.E2E_ADMIN_TOKEN;
  if (!adminToken) throw new Error("E2E_ADMIN_TOKEN must be set");

  await page.setViewportSize({ width: 390, height: 820 });
  await page.goto("/");
  await page.getByRole("button", { name: "API Bağlan" }).click();
  await page.getByLabel("Yerel yönetici anahtarı").fill(adminToken);
  await page.getByRole("button", { name: "Bağlan", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Yerel API bağlantısı kuruldu");

  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Risk Yönetimi" }).click();
  const budgets = page.getByRole("spinbutton", { name: "Miktar USDT" });
  await budgets.nth(0).fill("2345");
  await budgets.nth(1).fill("150");
  await page.getByRole("button", { name: "Ayarları Kaydet" }).click();
  await expect(page.getByRole("status")).toContainText("veritabanına kaydedildi");

  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Olay Kayıtları" }).click();
  await expect(page.getByText("Sürüm 1", { exact: false })).toBeVisible();

  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Genel Bakış" }).click();
  await expect(page.getByText("2.345 USDT")).toBeVisible();
  await expect(page.getByText("150 USDT")).toBeVisible();

  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Piyasa Verileri" }).click();
  const historyResponse = await page.request.get(
    "http://127.0.0.1:8000/api/v1/market/candles?symbol=BTCUSDT&interval=1m&limit=30",
    { headers: { "X-Admin-Token": adminToken } }
  );
  expect(historyResponse.status()).toBe(200);
  expect((await historyResponse.json()).candles).toHaveLength(0);
  await page.getByRole("button", { name: "Geçmişi Göster" }).click();
  await expect(page.getByText("Henüz kayıtlı mum verisi yok")).toBeVisible();

  await page.reload();
  await expect(page.getByText("Gerçek alım-satım kapalı.", { exact: false })).toBeVisible();
});
