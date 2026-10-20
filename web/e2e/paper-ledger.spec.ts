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
