import { expect, test } from "@playwright/test";

for (const width of [360, 390, 768, 1280]) {
  test("paper panel is usable at " + width + "px", async ({ page }) => {
    await page.setViewportSize({ width, height: 820 });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Genel Bakış" })).toBeVisible();
    const size = await page.evaluate(() => ({
      content: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    }));
    expect(size.content).toBeLessThanOrEqual(size.viewport + 1);
    if (width <= 780) {
      await page.getByRole("button", { name: "Menüyü aç" }).click();
    }
    await page.getByRole("button", { name: "Risk Yönetimi" }).click();
    await expect(page.getByRole("heading", { name: "Risk Yönetimi", level: 1 })).toBeVisible();
    await expect(page.getByRole("button", { name: "Ayarları Kaydet" })).toBeDisabled();
    const riskSize = await page.evaluate(() => ({
      content: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    }));
    expect(riskSize.content).toBeLessThanOrEqual(riskSize.viewport + 1);
  });
}

test("audit panel requires connected API and shows no fabricated events", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/");
  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Olay Kayıtları" }).click();
  await expect(page.getByRole("heading", { name: "Olay Kayıtları", level: 1 })).toBeVisible();
  await expect(page.getByText("Yerel API bağlantısı gerekiyor")).toBeVisible();
  await expect(page.getByRole("button", { name: "Geçmişi Yenile" })).toBeDisabled();
});

test("mobile historical market page never invents live prices", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/");
  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Piyasa Verileri" }).click();
  await expect(page.getByRole("heading", { name: "Piyasa Verileri", level: 1 })).toBeVisible();
  await expect(page.getByText("Yerel API bağlantısı gerekiyor")).toBeVisible();
  await expect(page.getByRole("button", { name: "Geçmişi Göster" })).toBeDisabled();
  await page.getByRole("combobox", { name: "Spot paritesi" }).selectOption("ETHUSDT");
  const layout = await page.evaluate(() => ({
    content: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(layout.content).toBeLessThanOrEqual(layout.viewport + 1);
});
