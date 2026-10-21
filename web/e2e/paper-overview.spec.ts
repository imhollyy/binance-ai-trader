import { expect, test } from "@playwright/test";

for (const width of [360, 390, 768]) {
  test(`PAPER dashboard keeps truthfully empty balances and worker status on ${width}px`, async ({ page }) => {
    test.skip(!process.env.E2E_CONNECTED, "Requires isolated Docker PostgreSQL API");
    const token = process.env.E2E_ADMIN_TOKEN;
    if (!token) throw new Error("E2E_ADMIN_TOKEN must be set");

    await page.setViewportSize({ width, height: 830 });
    await page.goto("/");
    await expect(page.getByText("Yerel API bağlantısı gerekiyor").first()).toBeVisible();

    await page.getByRole("button", { name: "API Bağlan" }).click();
    await page.getByLabel("Yerel yönetici anahtarı").fill(token);
    await page.getByRole("button", { name: "Bağlan", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("Yerel API bağlantısı kuruldu");

    const region = page.getByRole("region", { name: "PAPER sanal performans ve worker özeti" });
    await expect(region.getByText("Henüz PAPER Spot hesabı oluşturulmamış", { exact: false })).toBeVisible();
    await expect(region.getByText("Worker — Son bilinen durum")).toBeVisible();
    await expect(region.getByText("Çalışan servis doğrulanmadı.", { exact: false })).toBeVisible();
    await expect(region.getByText("Son sonuç")).toBeVisible();
    await expect(region.getByText("Kayıt yok")).toBeVisible();
    await expect(region.getByText("Gerçekleşmiş PnL (sanal)")).toHaveCount(0);
    await region.getByRole("button", { name: "Özeti Yenile" }).click();
    await expect(region.getByText("Henüz PAPER Spot hesabı oluşturulmamış", { exact: false })).toBeVisible();

    const response = await page.request.get("http://127.0.0.1:8000/api/v1/paper/dashboard", {
      headers: { "X-Admin-Token": token },
    });
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toContain("no-store");
    const payload = await response.json();
    expect(payload.initialized).toBe(false);
    expect(payload.worker.process_verified_running).toBe(false);
    expect(payload.events).toEqual([]);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
}
