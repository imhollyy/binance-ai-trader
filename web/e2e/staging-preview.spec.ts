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
    await expect(page.getByLabel("Yerel yönetici anahtarı", {exact:true})).toHaveCount(0);
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


for (const scenario of [
  {name: "dashboard unavailable", status: 503, body: {detail: "temporary outage"},
   expected: "Oturum açıldı ancak sanal kayıtlar okunamadı."},
  {name: "untrusted dashboard source", status: 200,
   body: {mode: "paper", source: "unverified_exchange",
     initialized: false, account: null, total_positions: 0, open_positions: 0, total_events: 0,
     worker: {requested_enabled: false, process_verified_running: false,
       last_status: null, last_attempt_at_ms: null, next_allowed_at_ms: null}},
   expected: "Güvenilir PAPER özeti alınamadı."},
]) {
  test("staging never shows a session after " + scenario.name, async ({page}) => {
    test.skip(
      process.env.NEXT_PUBLIC_TP_STAGING_PREVIEW !== "enabled",
      "Only for opt-in staging UI browser checks",
    );
    await page.route("https://api.staging.example/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      const headers = {
        "Access-Control-Allow-Origin": "http://127.0.0.1:3000",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,X-CSRF-Token",
        "Content-Type": "application/json",
      };
      if (route.request().method() === "OPTIONS") {
        await route.fulfill({status: 204, headers});
      } else if (path.endsWith("/auth/login")) {
        await route.fulfill({status: 200, headers,
          body: JSON.stringify({role: "viewer", csrf_token: "a".repeat(64)})});
      } else if (path.endsWith("/paper/dashboard")) {
        await route.fulfill({status: scenario.status, headers,
          body: JSON.stringify(scenario.body)});
      } else {
        await route.fulfill({status: 404, headers, body: "{}"});
      }
    });
    await page.setViewportSize({width: 360, height: 840});
    await page.goto("/");
    await page.getByLabel("Parola").fill("synthetic-only-password-not-real");
    await page.getByRole("button", {name: "Giriş yap"}).click();
    await expect(page.getByRole("heading", {name: "Güvenli oturum aç"})).toBeVisible();
    await expect(page.getByRole("button", {name: "Çıkış"})).toHaveCount(0);
    await expect(page.getByRole("status")).toContainText(scenario.expected);
    await expect(page.getByText("Kaydedilmiş nakit (sanal)")).toHaveCount(0);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
}
