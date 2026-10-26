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
    last_attempt_at_ms:null,next_allowed_at_ms:null,consecutive_failures:null},
};
function stagingOnly() {
  test.skip(process.env.NEXT_PUBLIC_TP_STAGING_PREVIEW !== "enabled",
    "Only the isolated preview browser build");
}
async function mockConsole(
  page:Page,
  role:"viewer"|"operator",
  onWrite?:(body:unknown,head:Record<string,string>)=>void,
  researchBody:unknown={available:false,source:"isolated_ai_research_store",status:"unavailable",
    server_time:"2026-10-23T00:00:00Z",predictions:[],metrics_available:false,
    metrics_reason:"research_store_not_configured"},
) {
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
    } else if(url.pathname.endsWith("/risk/profiles")){
      body={available:false,selectable:false,reason:"isolated_risk_store_not_configured",
        active_profile_version:null,active_risk_level:null,runtime_revision:null,
        bot_state:"STOPPED",schema_version:null,limits:null};
    } else if(url.pathname.endsWith("/ai/research/predictions")){
      body=researchBody;
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
    await nav(page,"Bot Yönetimi",width);
    await expect(page.getByRole("heading",{name:"Bot Yönetimi"})).toBeVisible();
    await expect(page.getByText("Son worker sonucu")).toBeVisible();
    await expect(page.getByText(/çalışan bir işlem sürecinin kanıtı değildir/)).toBeVisible();
    await expect(page.getByRole("button",{name:/botu başlat/i})).toHaveCount(0);
    await nav(page,"Risk Yönetimi",width);
    await expect(page.getByRole("heading",{name:"Risk Yönetimi"})).toBeVisible();
    await expect(page.getByText(/RiskGuard profili kullanılamıyor/)).toBeVisible();
    await expect(page.getByText(/uygulama ayarları RiskGuard profili yerine geçmez/)).toBeVisible();
    await expect(page.getByRole("button",{name:"Onaylı PAPER riskini kaydet"})).toHaveCount(0);
    await expect(page.getByLabel(/İşlem başı tahmini risk/)).toHaveCount(0);
    await nav(page,"AI Araştırma",width);
    await expect(page.getByRole("heading",{name:"AI Araştırma"})).toBeVisible();
    await expect(page.getByText("RESEARCH ONLY — NOT APPROVED").first()).toBeVisible();
    await expect(page.getByText(/Araştırma deposu kullanılamıyor/)).toBeVisible();
    await expect(page.getByText("Prospektif araştırma kaydı yok; tahmin üretilmez.")).toBeVisible();
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

test("staging cannot present legacy app settings as an independent RiskGuard profile",async ({page})=>{
  stagingOnly();
  await page.setViewportSize({width:1280,height:900});
  let settingsWrites=0;
  await mockConsole(page,"operator",()=>{settingsWrites++;});
  await login(page,"operator");
  await nav(page,"Risk Yönetimi",1280);
  await expect(page.getByRole("heading",{name:"Risk Yönetimi"})).toBeVisible();
  await expect(page.getByText(/RiskGuard profili kullanılamıyor/)).toBeVisible();
  await expect(page.getByRole("button",{name:/risk.*kaydet/i})).toHaveCount(0);
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
  expect(settingsWrites).toBe(0);
});

test("staging research receipt keeps provenance and approval status explicit",async ({page})=>{
  stagingOnly();
  const prediction={signal_id:"fixture_signal_0001",model_version:"fixture-model-v1",
    artifact_sha256:"a".repeat(64),feature_schema_version:"1.0",
    feature_sha256:"b".repeat(64),symbol:"ETHUSDT",interval:"15m",action:"NO_TRADE",
    reason_code:"fixture_only",observed_close_at_ms:1800000000000,
    received_at_ms:1800000001000,outcome_due_at_ms:1800000900000,
    probabilities:{win:0.2,loss:0.2,flat:0.6},outcome:null,evaluated_at_ms:null,
    outcome_source_sha256:null,approval_status:"RESEARCH_ONLY_NOT_APPROVED"};
  await mockConsole(page,"viewer",undefined,{available:true,source:"isolated_ai_research_store",
    status:"available",server_time:"2027-01-15T08:00:02Z",predictions:[prediction],
    metrics_available:false,metrics_reason:"no_persisted_evaluation_metrics"});
  await login(page,"viewer");
  await nav(page,"AI Araştırma",1280);
  await expect(page.getByText("ETHUSDT · 15m")).toBeVisible();
  await expect(page.getByText("NO_TRADE")).toBeVisible();
  await expect(page.getByText("fixture-model-v1 · 1.0")).toBeVisible();
  await expect(page.getByText(/flat: 0.6/)).toBeVisible();
  await expect(page.getByText("RESEARCH ONLY — NOT APPROVED").first()).toBeVisible();
  await expect(page.getByText(/no_persisted_evaluation_metrics/)).toBeVisible();
});
