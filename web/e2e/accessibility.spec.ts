import { expect, test } from "@playwright/test";

for (const width of [360, 390, 768, 1280]) {
  test(`keyboard and labels remain accessible at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 820 });
    await page.goto("/");

    const opener = page.getByRole("button", { name: "API Bağlan" });
    await opener.focus();
    await expect(opener).toHaveCSS("outline-style", "solid");
    await opener.click();

    const dialog = page.getByRole("dialog", { name: "Yerel API Bağlantısı" });
    await expect(dialog).toBeVisible();
    await expect(dialog).toBeFocused();
    await expect(page.locator("main")).toHaveAttribute("inert", "");
    await page.keyboard.press("Shift+Tab");
    await expect(dialog.getByRole("button", { name: "Bağlan", exact: true })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(dialog.getByRole("button", { name: "Kapat" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();

    if (width <= 780) await page.getByRole("button", { name: "Menüyü aç" }).click();
    await page.getByRole("button", { name: "Risk Yönetimi" }).click();
    const labels = [
      "Spot bütçesi (USDT)", "Futures bütçesi (USDT)", "İşlem başına risk (%)",
      "Günlük zarar sınırı (%)", "Maksimum düşüş (%)", "Açık pozisyon sayısı",
      "Maksimum kaldıraç (×)",
    ];
    for (const label of labels) {
      await expect(page.getByRole("spinbutton", { name: label, exact: true })).toHaveCount(1);
    }
    const riskSize = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(riskSize).toBeLessThanOrEqual(1);

    if (width <= 780) await page.getByRole("button", { name: "Menüyü aç" }).click();
    await page.getByRole("button", { name: "Bot Yönetimi" }).click();
    const spot = page.getByRole("button", { name: "Spot", exact: true });
    const futures = page.getByRole("button", { name: "Futures (Sanal)" });
    await expect(spot).toHaveAttribute("aria-pressed", "true");
    await expect(futures).toHaveAttribute("aria-pressed", "false");
    await futures.click();
    await expect(spot).toHaveAttribute("aria-pressed", "false");
    await expect(futures).toHaveAttribute("aria-pressed", "true");
    const botSize = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(botSize).toBeLessThanOrEqual(1);
  });
}

test("reduced motion removes decorative transitions", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 820 });
  await page.goto("/");
  await page.getByRole("button", { name: "Menüyü aç" }).click();
  await page.getByRole("button", { name: "Bot Yönetimi" }).click();
  const duration = await page.locator(".switch span").first().evaluate(
    element => getComputedStyle(element).transitionDuration
  );
  expect(duration).toBe("1e-05s");
});
