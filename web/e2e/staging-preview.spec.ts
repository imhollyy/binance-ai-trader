import { expect, test } from "@playwright/test";

for (const width of [360, 390, 768, 1280]) {
  test(`opt-in staging preview has no local admin token fields at ${width}px`, async ({page}) => {
    test.skip(
      process.env.NEXT_PUBLIC_TP_STAGING_PREVIEW !== "enabled",
      "Only for the opt-in HTTPS staging UI build gate",
    );
    await page.setViewportSize({width, height:840});
    await page.goto("/");
    await expect(page.getByRole("heading", {name:"TradePilot"})).toBeVisible();
    await expect(page.getByRole("heading", {name:"Güvenli oturum aç"})).toBeVisible();
    await expect(page.getByLabel("Parola")).toBeVisible();
    await expect(page.getByRole("button", {name:"Giriş yap"})).toBeVisible();
    await expect(page.getByText("Yerel yönetici anahtarı")).toHaveCount(0);
    await expect(page.getByRole("button", {name:"API Bağlan"})).toHaveCount(0);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
}

test("staging UI only renders persisted PAPER data from an explicitly mock API", async ({page})=>{
  test.skip(
    process.env.NEXT_PUBLIC_TP_STAGING_PREVIEW !== "enabled",
    "Only the staging UI fixture (not real cookie roundtrip)",
  );
  await page.route("https://api.staging.example/api/v1/**", async route => {
    const url = new URL(route.request().url());
    const headers = {
      "Access-Control-Allow-Origin":"http://127.0.0.1:3000",
      "Access-Control-Allow-Credentials":"true",
      "Access-Control-Allow-Methods":"GET,POST,OPTIONS",
      "Access-Control-Allow-Headers":"Content-Type,X-CSRF-Token",
      "Content-Type":"application/json",
    };
    if(route.request().method()==="OPTIONS"){
      await route.fulfill({status:204, headers});
      return;
    }
    const data = url.pathname.endsWith("/auth/login")
      ? {role:"viewer", csrf_token:"a".repeat(64)}
      : url.pathname.endsWith("/auth/logout")
        ? {status:"signed_out"}
        : {mode:"paper",source:"persisted_paper_spot",initialized:false,account:null,
          total_positions:0,open_positions:0,total_events:0,
          worker:{requested_enabled:false,process_verified_running:false,
            last_status:null,last_attempt_at_ms:null,next_allowed_at_ms:null}};
    await route.fulfill({status:200,headers,body:JSON.stringify(data)});
  });
  await page.goto("/");
  await page.getByLabel("Parola").fill("synthetic-only-password-not-real");
  await page.getByRole("button",{name:"Giriş yap"}).click();
  await expect(page.getByText("Henüz PAPER hesap verisi bulunmuyor")).toBeVisible();
  await expect(page.getByText("Çalışan servis doğrulanmadı.",{exact:false})).toBeVisible();
  await page.getByRole("button",{name:"Çıkış"}).click();
  await expect(page.getByRole("heading",{name:"Güvenli oturum aç"})).toBeVisible();
});
