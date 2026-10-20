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
    await expect(page.getByRole("heading", { name: "Risk Yönetimi" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Ayarları Kaydet" })).toBeDisabled();
    const riskSize = await page.evaluate(() => ({
      content: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    }));
    expect(riskSize.content).toBeLessThanOrEqual(riskSize.viewport + 1);
  });
}
