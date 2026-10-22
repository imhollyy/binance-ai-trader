import {expect, test, type Page} from "@playwright/test";

const ORIGIN = "https://api.staging.example";
const headers = {
  "Access-Control-Allow-Origin":"http://127.0.0.1:3000",
  "Access-Control-Allow-Credentials":"true",
  "Access-Control-Allow-Methods":"GET,POST,PUT,OPTIONS",
  "Access-Control-Allow-Headers":"Content-Type,X-CSRF-Token,X-Paper-Settings-Approval",
  "Content-Type":"application/json",
};
const settings = {
  mode:"paper",market_type:"spot",bot_enabled:false,spot_capital_usdt:1000,
  futures_capital_usdt:0,risk_per_trade_pct:0.25,daily_loss_limit_pct:1.5,
  max_drawdown_pct:5,max_open_positions:3,max_leverage:1,stop_loss_required:true,
  manual_approval:true,allowed_symbols:["BTCUSDT","ETHUSDT"],
};
const dashboard = {
  mode:"paper",source:"persisted_paper_spot",initialized:true,
  account:{initial_capital_usdt:"1000.000000000000000000",cash_usdt:"1000.000000000000000000",
    realized_pnl_usdt:"0.000000000000000000",paid_fees_usdt:"0.000000000000000000"},
  total_positions:0,open_positions:0,total_events:1,positions:[],
  events:[{id:1,event_type:"account_opened",created_at:"2026-10-22T08:00:00Z",
    cash_delta_usdt:"1000.000000000000000000",realized_pnl_delta_usdt:"0.000000000000000000",
    fee_usdt:"0.000000000000000000",quote:null}],
  worker:{requested_enabled:false,process_verified_running:false,last_status:null,
    last_attempt_at_ms:null,next_allowed_at_ms:null},
};
function stagingOnly() {
  test.skip(process.env.NEXT_PUBLIC_TP_STAGING_PREVIEW !== "enabled",
    "Only the isolated preview browser build");
}
async function mockConsole(page:Page, role:"viewer"|"operator", onWrite?:(body:unknown,head:Record<string,string>)=>void) {
  await page.route(ORIGIN + "/api/v1/**", async route=>{
    const request=route.request();
    const url=new URL(request.url());
    if(request.method()==="OPTIONS"){
      await route.fulfill({status:204,headers}); return;
    }
    let status=200;
    let body:unknown;
    if(url.pathname.endsWith("/auth/login")){
      body={role,csrf_token:"a".repeat(64)};
    } else if(url.pathname.endsWith("/auth/logout")){
      body={status:"signed_out"};
    } else if(url.pathname.endsWith("/paper/dashboard")){
      body=dashboard;
    } else if(url.pathname.endsWith("/market/candles")){
      body={source:"stored_public_spot",symbol:url.searchParams.get("symbol")??"BTCUSDT",
        interval:url.searchParams.get("interval")??"1m",candles:[]};
    } else if(url.pathname.endsWith("/settings")){
      if(request.method()==="PUT"){
        if(role!=="operator") {status=403;body={detail:"Not authorized"};}
        else {
          const posted=JSON.parse(request.postData()??"{}");
          onWrite?.(posted,request.headers());
          body={version:2,settings:posted.settings};
        }
      } else body={version:1,settings};
    } else if(url.pathname.endsWith("/audit")){
      body={events:[{version:1,action:"settings_updated",created_at:"2026-10-22T08:00:00Z"}]};
    } else {status=404;body={detail:"Not Found"};}
    await route.fulfill({status,headers,body:JSON.stringify(body)});
  });
}
async function login(page:Page, role:"viewer"|"operator") {
  await page.goto("/");
  await page.getByLabel("Kullanıcı rolü").selectOption(role);
  await page.getByLabel("Parola").fill("synthetic-password-never-a-secret");
  await page.getByRole("button",{name:"Giriş yap"}).click();
  await expect(page.getByRole("button",{name:"Çıkış"})).toBeVisible();
}
async function nav(page:Page,name:string,width:number){
  if(width <= 800) await page.getByRole("button",{name:"Yönetim menüsü"}).click();
  await page.getByRole("navigation",{name:"TradePilot bölümleri"}).getByRole("button",{name}).click();
}

for(const width of [360,390,768,1280]){
  test("BT-009H mobile and desktop secure console at "+width+"px",async ({page})=>{
    stagingOnly();
    await page.setViewportSize({width,height:840});
    await mockConsole(page,"viewer");
    await login(page,"viewer");
    if(width <= 800) await page.getByRole("button",{name:"Yönetim menüsü"}).click();
    const menu=page.getByRole("navigation",{name:"TradePilot bölümleri"});
    await expect(menu.getByRole("button")).toHaveCount(7);
    await expect(page.getByLabel("Yerel yönetici anahtarı",{exact:true})).toHaveCount(0);
    await expect(page.getByRole("button",{name:"API Bağlan"})).toHaveCount(0);
    await menu.getByRole("button",{name:"Piyasa Verileri"}).click();
    await expect(page.getByRole("heading",{name:"Piyasa Verileri"})).toBeVisible();
    await expect(page.getByText("Kayıtlı mum bulunamadı")).toBeVisible();
    await nav(page,"Risk Yönetimi",width);
    await expect(page.getByRole("heading",{name:"Risk Yönetimi"})).toBeVisible();
    await expect(page.getByText("Bot çalışma tercihi")).toBeVisible();
    await expect(page.getByRole("button",{name:"Onaylı PAPER riskini kaydet"})).toHaveCount(0);
    await nav(page,"Olay Kayıtları",width);
    await expect(page.getByRole("heading",{name:"Ayar Olay Kayıtları"})).toBeVisible();
    await expect(page.getByText("Ayarlar güncellendi")).toBeVisible();
    await expect(page.getByText("Sanal hesap açıldı")).toBeVisible();
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.getByRole("button",{name:"Çıkış"}).click();
    await expect(page.getByRole("heading",{name:"Güvenli oturum aç"})).toBeVisible();
  });
}

test("BT-009H operator uses explicit approval, version and CSRF for paper risk only",async ({page})=>{
  stagingOnly();
  await page.setViewportSize({width:1280,height:900});
  let writes=0;
  await mockConsole(page,"operator",(body,head)=>{
    writes++;
    const data=body as {expected_version:number;settings:typeof settings};
    expect(data.expected_version).toBe(1);
    expect(data.settings.risk_per_trade_pct).toBe(0.35);
    expect(data.settings.bot_enabled).toBe(false);
    expect(data.settings.manual_approval).toBe(true);
    expect(data.settings.mode).toBe("paper");
    expect(head["x-csrf-token"]).toBe("a".repeat(64));
    expect(head["x-paper-settings-approval"]).toBe("PAPER_SETTINGS_ONLY");
    expect(head["x-admin-token"]).toBeUndefined();
  });
  await login(page,"operator");
  await nav(page,"Risk Yönetimi",1280);
  await expect(page.getByRole("heading",{name:"Risk Yönetimi"})).toBeVisible();
  const button=page.getByRole("button",{name:"Onaylı PAPER riskini kaydet"});
  await expect(button).toBeDisabled();
  await page.getByRole("spinbutton",{name:/İşlem başı tahmini risk/}).fill("0.35");
  await expect(button).toBeDisabled();
  expect(writes).toBe(0);
  await page.getByRole("checkbox",{name:/Yalnız PAPER risk sınırlarını/}).check();
  await expect(button).toBeEnabled();
  await button.click();
  await expect(page.getByRole("status")).toContainText("PAPER risk ayarları sunucuda kaydedildi.");
  expect(writes).toBe(1);
  await expect(page.getByRole("checkbox",{name:/Yalnız PAPER risk sınırlarını/})).not.toBeChecked();
});
