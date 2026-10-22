import { expect, test } from "@playwright/test";

test("connected paper UI stays read-only when profile and worker controls are unsupported", async ({ page }) => {
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
  await expect(page.getByRole("combobox", { name: "Risk seviyesi" })).toBeDisabled();
  await expect(page.getByText("Risk profili değiştirilemiyor.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Ayarlar" })).toHaveCount(0);

  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Bot Yönetimi" }).click();
  await expect(page.getByRole("button", { name: "Botu başlat" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Botu durdur" })).toBeDisabled();

  const historyResponse = await page.request.get(
    "http://127.0.0.1:8000/api/v1/market/candles?symbol=BTCUSDT&interval=1m&limit=30",
    { headers: { "X-Admin-Token": adminToken } }
  );
  expect(historyResponse.status()).toBe(200);
  expect((await historyResponse.json()).candles).toHaveLength(0);
  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Piyasa Verileri" }).click();
  await page.getByRole("button", { name: "Geçmişi Göster" }).click();
  await expect(page.getByText("Henüz kayıtlı mum verisi yok")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Gerçek alım-satım kapalı.", { exact: false })).toBeVisible();
});
