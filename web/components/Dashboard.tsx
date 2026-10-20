"use client";

import {
  Activity, BarChart3, Bot, CheckCircle2, ChevronRight, CirclePause,
  Eye, EyeOff, FileClock, Layers3, LockKeyhole, Menu, Save,
  Settings2, ShieldCheck, ShieldAlert, Wallet, X
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";

type Section = "dashboard" | "market" | "bot" | "risk" | "strategies" | "positions" | "logs";
type Market = "spot" | "futures";
type Risk = {
  mode: "paper"; market_type: Market; bot_enabled: boolean;
  spot_capital_usdt: number; futures_capital_usdt: number;
  risk_per_trade_pct: number; daily_loss_limit_pct: number;
  max_drawdown_pct: number; max_open_positions: number;
  max_leverage: number; stop_loss_required: true;
  manual_approval: boolean; allowed_symbols: string[];
};
type Snapshot = { version: number; settings: Risk };
type SpotSymbol = "BTCUSDT" | "ETHUSDT";
type SpotInterval = "1m" | "5m" | "15m";
type StoredSpotCandle = {
  open_time_ms: number; close_time_ms: number; open: string; high: string;
  low: string; close: string; volume: string;
};
type StoredCandleHistory = {
  source: "stored_public_spot"; symbol: SpotSymbol; interval: SpotInterval;
  candles: StoredSpotCandle[];
};
type AuditEvent = { version: number; action: string; created_at: string };
const DEFAULT: Risk = {
  mode: "paper", market_type: "spot", bot_enabled: false,
  spot_capital_usdt: 1000, futures_capital_usdt: 0,
  risk_per_trade_pct: 0.25, daily_loss_limit_pct: 1.5,
  max_drawdown_pct: 5, max_open_positions: 3,
  max_leverage: 1, stop_loss_required: true,
  manual_approval: true, allowed_symbols: ["BTCUSDT", "ETHUSDT"]
};
function localApiAddress(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) || url.username || url.password) {
    throw new Error("API adresi yalnızca yerel localhost veya 127.0.0.1 olabilir.");
  }
  return url.origin;
}
const nav = [
  {key: "dashboard", title: "Genel Bakış", Icon: BarChart3},
  {key: "market", title: "Piyasa Verileri", Icon: Activity},
  {key: "bot", title: "Bot Yönetimi", Icon: Bot},
  {key: "risk", title: "Risk Yönetimi", Icon: ShieldCheck},
  {key: "strategies", title: "Stratejiler", Icon: Layers3},
  {key: "positions", title: "Pozisyonlar", Icon: Wallet},
  {key: "logs", title: "Olay Kayıtları", Icon: FileClock}
] as const;

function Row({label, desc, children}:{label:string;desc?:string;children:ReactNode}) {
  return <div className="setting-row"><div><strong>{label}</strong>{desc && <small>{desc}</small>}</div><div className="control">{children}</div></div>;
}
function NumberBox({label, value, onChange, min, max, step=1, unit=""}:{
  label:string;value:number;onChange:(n:number)=>void;min:number;max:number;step?:number;unit?:string;
}) {
  return <div className="num-box"><input aria-label={label} type="number" inputMode="decimal"
    min={min} max={max} step={step} value={value}
    onChange={event=>onChange(Number(event.target.value))}/><span>{unit}</span></div>;
}
function Switch({value,onChange,label,disabled=false}:{value:boolean;onChange:(v:boolean)=>void;label:string;disabled?:boolean}) {
  return <button className={"switch " + (value?"on":"")} role="switch" type="button"
    aria-checked={value} aria-label={label} disabled={disabled} onClick={()=>onChange(!value)}>
    <span/></button>;
}

export default function Dashboard() {
  const [section,setSection] = useState<Section>("dashboard");
  const [drawer,setDrawer] = useState(false);
  const [dialog,setDialog] = useState(false);
  const dialogRef = useRef<HTMLElement>(null);
  const dialogOpenerRef = useRef<HTMLElement | null>(null);
  const [secret,setSecret] = useState("");
  const [visible,setVisible] = useState(false);
  const [address,setAddress] = useState("http://127.0.0.1:8000");
  const [connected,setConnected] = useState(false);
  const [version,setVersion] = useState(0);
  const [risk,setRisk] = useState<Risk>(DEFAULT);
  const [notice,setNotice] = useState("");
  const [busy,setBusy] = useState(false);
  const [auditBusy,setAuditBusy] = useState(false);
  const [auditEvents,setAuditEvents] = useState<AuditEvent[]>([]);
  const [spotSymbol,setSpotSymbol] = useState<SpotSymbol>("BTCUSDT");
  const [spotInterval,setSpotInterval] = useState<SpotInterval>("1m");
  const [storedCandles,setStoredCandles] = useState<StoredSpotCandle[]>([]);
  const [marketLoad,setMarketLoad] = useState<"idle"|"loading"|"ready"|"error">("idle");

  useEffect(() => {
    if (!dialog) return;
    dialogRef.current?.focus();
    return () => dialogOpenerRef.current?.focus();
  }, [dialog]);

  function openDialog(opener: HTMLElement) {
    dialogOpenerRef.current = opener;
    setDialog(true);
  }

  function handleDialogKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      setDialog(false);
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]'
    ) ?? []);
    if (!controls.length) {
      event.preventDefault();
      return;
    }
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function change<K extends keyof Risk>(key:K, value:Risk[K]) {
    setRisk(current => ({...current,[key]:value}));
  }
  function market(next:Market) {
    setRisk(current=>({...current,market_type:next,bot_enabled:false,max_leverage:next==="spot"?1:current.max_leverage}));
  }
  async function connect() {
    if (!secret.trim()){setNotice("Yerel ADMIN_TOKEN girilmedi.");return;}
    setBusy(true);setNotice("");
    try {
      const response=await fetch(localApiAddress(address)+"/api/v1/settings",{
        headers:{"X-Admin-Token":secret},cache:"no-store"
      });
      if(!response.ok)throw new Error("Bağlantı reddedildi: HTTP "+response.status);
      const data:Snapshot=await response.json();
      setRisk(data.settings);setVersion(data.version);setConnected(true);setDialog(false);
      setNotice("Yerel API bağlantısı kuruldu. Sadece paper ayarları düzenlenebilir.");
    }catch(error){
      setConnected(false);
      setNotice(error instanceof Error?error.message:"API bağlantısı kurulamadı.");
    }finally{setBusy(false);}
  }
  async function loadAudit(){
    if(!connected)return;
    setAuditBusy(true);setNotice("");
    try{
      const response=await fetch(localApiAddress(address)+"/api/v1/audit?limit=30",{
        headers:{"X-Admin-Token":secret},cache:"no-store"
      });
      if(!response.ok)throw new Error("Olay geçmişi okunamadı: HTTP "+response.status);
      const data:{events:AuditEvent[]}=await response.json();
      setAuditEvents(data.events);
    }catch(error){
      setAuditEvents([]);
      setNotice(error instanceof Error?error.message:"Olay geçmişi alınamadı.");
    }finally{setAuditBusy(false);}
  }
  async function loadStoredSpot(
    symbol: SpotSymbol = spotSymbol,
    interval: SpotInterval = spotInterval
  ){
    if(!connected){setStoredCandles([]);setMarketLoad("idle");return;}
    setMarketLoad("loading");setNotice("");
    try{
      const query=new URLSearchParams({symbol,interval,limit:"30"});
      const response=await fetch(
        localApiAddress(address)+"/api/v1/market/candles?"+query.toString(),
        {headers:{"X-Admin-Token":secret},cache:"no-store"}
      );
      if(!response.ok)throw new Error("Kayıtlı veriler okunamadı (HTTP "+response.status+").");
      const payload:StoredCandleHistory=await response.json();
      if(payload.source!=="stored_public_spot" || !Array.isArray(payload.candles)){
        throw new Error("Beklenmeyen piyasa veri biçimi.");
      }
      setStoredCandles(payload.candles);
      setMarketLoad("ready");
    }catch(error){
      setStoredCandles([]);setMarketLoad("error");
      setNotice(error instanceof Error?error.message:"Piyasa verisi okunamadı.");
    }
  }
  async function save(){
    if(!connected){setDialog(true);return;}
    setBusy(true);setNotice("");
    try{
      const response=await fetch(localApiAddress(address)+"/api/v1/settings",{
        method:"PUT",
        headers:{"Content-Type":"application/json","X-Admin-Token":secret},
        body:JSON.stringify({expected_version:version,settings:risk})
      });
      if(!response.ok){
        if(response.status===409)throw new Error("Ayar sürümü değişti. Yeniden bağlanıp güncel değerleri yükle.");
        if(response.status===422)throw new Error("Risk ayarlarından biri güvenli aralık dışında.");
        throw new Error("Kaydedilemedi: HTTP "+response.status);
      }
      const data:Snapshot=await response.json();
      setRisk(data.settings);setVersion(data.version);setNotice("Paper yapılandırması veritabanına kaydedildi.");
    }catch(error){setNotice(error instanceof Error?error.message:"Kaydetme hatası.");}
    finally{setBusy(false);}
  }
  const activeTitle=nav.find(item=>item.key===section)?.title || "Genel Bakış";
  const saveButton=<button className="button primary" disabled={!connected||busy} onClick={save}>
    <Save size={16}/>{busy?"Kaydediliyor":"Ayarları Kaydet"}</button>;
  return <div className="shell">
    {drawer&&<button className="scrim" aria-label="Menüyü kapat" onClick={()=>setDrawer(false)}/>}
    <aside inert={dialog} className={"sidebar "+(drawer?"opened":"")}>
      <div className="identity">
        <div className="logo"><Activity size={23}/></div><div><strong>TradePilot</strong><small>AI Trading Console</small></div>
        <button className="mobile-close" onClick={()=>setDrawer(false)} aria-label="Kapat"><X size={20}/></button>
      </div>
      <div className="nav-label">YÖNETİM</div>
      <nav aria-label="Ana menü">{nav.map(item=>
        <button key={item.key} className={"nav-item "+(section===item.key?"active":"")}
          onClick={()=>{setSection(item.key);setDrawer(false);setNotice("");if(item.key==="logs"){void loadAudit();}if(item.key==="market"){void loadStoredSpot();}}}>
          <item.Icon size={18}/><span>{item.title}</span><ChevronRight size={14}/></button>
      )}</nav>
      <div className="sidebar-footer"><LockKeyhole size={16}/> PAPER ONLY<br/> Canlı emir özelliği yok.</div>
    </aside>
    <main inert={dialog} className="main">
      <header className="toolbar">
        <div className="toolbar-left"><button className="hamburger" onClick={()=>setDrawer(true)} aria-label="Menüyü aç">
          <Menu size={23}/></button><span>TradePilot</span><ChevronRight size={14}/><strong>{activeTitle}</strong></div>
        <div className="toolbar-right"><span className="pill">● PAPER MODE</span>
          <button className="connect-button" aria-label={connected?"Yerel API Bağlı":"API Bağlan"} onClick={event=>openDialog(event.currentTarget)}><Settings2 size={17}/>
          <span>{connected?"Yerel API Bağlı":"API Bağlan"}</span></button></div>
      </header>
      <div className="content">
        <div className="pretitle">GÜVENLİ GELİŞTİRME ORTAMI</div>
        <div className="page-title"><div><h1>{activeTitle}</h1>
          <p>{section==="dashboard"?"Trading altyapısını tek noktadan yönet.":"Yalnızca sanal risk ayarları ve bot tercihleri."}</p></div>
          <span className="build-info">SPRINT 02 · DEV</span></div>
        {notice&&<div className="notice" role="status"><ShieldAlert size={17}/><span>{notice}</span>
          <button aria-label="Bildirimi kapat" onClick={()=>setNotice("")}><X size={16}/></button></div>}
        <div className="warning"><ShieldAlert size={19}/><div><strong>Gerçek alım-satım kapalı.</strong> Bu paneldeki
          tutarlar sanal limitlerdir. Binance hesabı, emir yürütme veya aktif worker henüz bulunmuyor.</div></div>
        {section==="dashboard"&&<>
          <div className="stats">
            <article className="stat"><span>Sanal Spot Bütçesi</span><strong>{risk.spot_capital_usdt.toLocaleString("tr-TR")} USDT</strong><small>Tanımlı simülasyon limiti</small></article>
            <article className="stat"><span>Sanal Futures Bütçesi</span><strong>{risk.futures_capital_usdt.toLocaleString("tr-TR")} USDT</strong><small>Gerçek kaldıraç kullanılmaz</small></article>
            <article className="stat"><span>İşlem Başına Risk</span><strong>%{risk.risk_per_trade_pct.toLocaleString("tr-TR")}</strong><small>Risk kuralı</small></article>
            <article className="stat"><span>Bot Durumu</span><strong>Çalışmıyor</strong><small>Worker henüz geliştirilmedi</small></article>
          </div>
          <div className="dashboard-grid">
            <section className="surface performance"><h2>Performans Özeti</h2>
              <div className="empty"><BarChart3 size={32}/><strong>İşlem verisi henüz yok</strong>
              <span>Paper motoru geliştirildikten sonra gerçek zamanlı simülasyon sonuçları gösterilecek.</span></div></section>
            <section className="surface"><h2>Güvenlik Kontrolleri</h2><div className="safety-list">
              <p><CheckCircle2 size={18}/> Canlı emir yürütme kapalı</p>
              <p><CheckCircle2 size={18}/> Stop-loss zorunlu</p>
              <p><CheckCircle2 size={18}/> Risk üst sınırları mevcut</p>
              <p><CirclePause size={18}/> Binance hesabı bağlı değil</p>
              <p><CirclePause size={18}/> Worker aktif değil</p>
            </div><button className="button outline" onClick={()=>setSection("risk")}>Riskleri Gör <ChevronRight size={16}/></button></section>
          </div>
        </>}
        {section==="market"&&<section className="surface market-surface">
          <div className="section-title market-title">
            <div><h2>Kayıtlı Spot Piyasa Geçmişi</h2>
              <p>Binance'in herkese açık verilerinden saklanan son kapanmış mumlar.
                Anlık fiyat veya işlem sinyali değildir.</p></div>
            <button className="button outline" onClick={()=>{void loadStoredSpot();}}
              disabled={!connected||marketLoad==="loading"}>
              {marketLoad==="loading"?"Yükleniyor...":"Verileri Yenile"}</button>
          </div>
          <div className="market-filters">
            <label>Parite
              <select aria-label="Spot paritesi" value={spotSymbol}
                onChange={e=>{
                  setSpotSymbol(e.target.value as SpotSymbol);
                  setStoredCandles([]);setMarketLoad("idle");
                }}>
                <option value="BTCUSDT">BTC / USDT</option>
                <option value="ETHUSDT">ETH / USDT</option>
              </select>
            </label>
            <label>Zaman Aralığı
              <select aria-label="Mum zaman aralığı" value={spotInterval}
                onChange={e=>{
                  setSpotInterval(e.target.value as SpotInterval);
                  setStoredCandles([]);setMarketLoad("idle");
                }}>
                <option value="1m">1 dakika</option>
                <option value="5m">5 dakika</option>
                <option value="15m">15 dakika</option>
              </select>
            </label>
            <button className="button primary" disabled={!connected||marketLoad==="loading"}
              onClick={()=>{void loadStoredSpot();}}>Geçmişi Göster</button>
          </div>
          <div className="market-disclaimer">
            <ShieldCheck size={16}/> Sadece PostgreSQL'e kaydedilen kapanmış mumlar.
            Yeni veri otomatik indirilmez; kayıtlar gecikmiş olabilir.
          </div>
          {!connected?<div className="empty market-empty"><LockKeyhole size={30}/>
            <strong>Yerel API bağlantısı gerekiyor</strong>
            <span>Piyasa geçmişi yüklenmedi. Örnek veya uydurma fiyat gösterilmiyor.</span>
          </div>:marketLoad==="loading"?<div className="empty market-empty">
            <strong>Kayıtlı veriler okunuyor...</strong>
          </div>:marketLoad==="ready"&&storedCandles.length===0?
            <div className="empty market-empty"><BarChart3 size={30}/>
              <strong>Henüz kayıtlı mum verisi yok</strong>
              <span>Veri içe alımı çalıştırılmadıkça burada piyasa fiyatı görünmez.</span>
            </div>:marketLoad==="ready"?
            <div className="market-history">
              <div className="market-summary">
                <div><span>Son kayıtlı kapanış</span>
                  <strong>{storedCandles[0].close} USDT</strong>
                  <small>Güncel piyasa fiyatı değildir.</small></div>
                <div><span>Son kayıt zamanı</span>
                  <strong className="market-time">{new Date(storedCandles[0].close_time_ms).toLocaleString("tr-TR",{timeZone:"UTC"})} UTC</strong>
                  <small>{storedCandles.length} kayıt gösteriliyor</small></div>
              </div>
              <div className="market-record-list" aria-label="Kayıtlı mum geçmişi">
                {storedCandles.map(row=><article className="market-record" key={row.open_time_ms}>
                  <div className="record-top">
                    <strong>{new Date(row.close_time_ms).toLocaleString("tr-TR",{timeZone:"UTC"})} UTC</strong>
                    <span>{spotSymbol} · {spotInterval}</span></div>
                  <div className="record-values">
                    <div><small>Açılış</small><strong>{row.open}</strong></div>
                    <div><small>Kapanış</small><strong>{row.close}</strong></div>
                    <div><small>En yüksek</small><strong>{row.high}</strong></div>
                    <div><small>En düşük</small><strong>{row.low}</strong></div>
                    <div><small>Hacim</small><strong>{row.volume}</strong></div>
                  </div></article>)}
              </div>
            </div>:<div className="empty market-empty"><BarChart3 size={30}/>
              <strong>Henüz piyasa geçmişi yüklenmedi</strong>
              <span>Bağlantı kurup pariteyi seçtikten sonra Geçmişi Göster'e dokun.</span>
            </div>}
        </section>}
        {section==="bot"&&<section className="surface form">
          <div className="section-title"><div><h2>Bot Yapılandırması</h2><p>Bu ayarlar tek başına işlem başlatmaz.</p></div>{saveButton}</div>
          <h3>Hedef Piyasa</h3><div className="segmented" role="group" aria-label="Piyasa tercihi">
            <button aria-pressed={risk.market_type==="spot"} className={risk.market_type==="spot"?"selected":""} onClick={()=>market("spot")}>Spot</button>
            <button aria-pressed={risk.market_type==="futures"} className={risk.market_type==="futures"?"selected":""} onClick={()=>market("futures")}>Futures (Sanal)</button>
          </div>
          <Row label="Botu etkinleştirme isteği" desc="Worker yok; sadece gelecekte uygulanmak üzere saklanır.">
            <Switch label="Bot etkinleşme tercihi" value={risk.bot_enabled} onChange={v=>change("bot_enabled",v)}/></Row>
          <Row label="Manuel onay tercihi" desc="Varsayılan açık.">
            <Switch label="Manuel işlem onayı" value={risk.manual_approval} onChange={v=>change("manual_approval",v)}/></Row>
          <Row label="Pariteler" desc="Büyük harflerle, virgülle ayır.">
            <input className="text-field" aria-label="İşlem pariteleri" value={risk.allowed_symbols.join(", ")}
              onChange={e=>change("allowed_symbols",e.target.value.split(",").map(s=>s.trim().toUpperCase()).filter(Boolean))}/></Row>
        </section>}
        {section==="risk"&&<section className="surface form">
          <div className="section-title"><div><h2>Risk Yönetimi</h2><p>Spot ve Futures bütçelerini ayrı yönet.</p></div>{saveButton}</div>
          <h3>Sanal Sermaye</h3>
          <div className="field-grid">
            <Row label="Spot bütçesi" desc="0 – 1.000.000 USDT"><NumberBox label="Spot bütçesi (USDT)" unit="USDT" value={risk.spot_capital_usdt} min={0} max={1000000} onChange={v=>change("spot_capital_usdt",v)}/></Row>
            <Row label="Futures bütçesi" desc="0 – 1.000.000 USDT"><NumberBox label="Futures bütçesi (USDT)" unit="USDT" value={risk.futures_capital_usdt} min={0} max={1000000} onChange={v=>change("futures_capital_usdt",v)}/></Row>
          </div>
          <h3>Risk Limitleri</h3><div className="field-grid">
            <Row label="İşlem başına risk" desc="%0,05 – %2"><NumberBox label="İşlem başına risk (%)" unit="%" step={0.05} min={0.05} max={2} value={risk.risk_per_trade_pct} onChange={v=>change("risk_per_trade_pct",v)}/></Row>
            <Row label="Günlük zarar sınırı" desc="%0,1 – %5"><NumberBox label="Günlük zarar sınırı (%)" unit="%" step={0.1} min={0.1} max={5} value={risk.daily_loss_limit_pct} onChange={v=>change("daily_loss_limit_pct",v)}/></Row>
            <Row label="Maksimum düşüş" desc="%1 – %15"><NumberBox label="Maksimum düşüş (%)" unit="%" min={1} max={15} value={risk.max_drawdown_pct} onChange={v=>change("max_drawdown_pct",v)}/></Row>
            <Row label="Açık pozisyon sayısı" desc="1 – 10"><NumberBox label="Açık pozisyon sayısı" min={1} max={10} value={risk.max_open_positions} onChange={v=>change("max_open_positions",v)}/></Row>
            <Row label="Maksimum kaldıraç" desc="Spot: 1×, sanal Futures: 1–5×"><NumberBox label="Maksimum kaldıraç (×)" unit="×" min={1} max={risk.market_type==="spot"?1:5} value={risk.max_leverage} onChange={v=>change("max_leverage",v)}/></Row>
          </div>
          <Row label="Stop-loss zorunlu" desc="Güvenlik gereği kapatılamaz."><Switch label="Stop-loss zorunlu" value={true} onChange={()=>{}} disabled/></Row>
        </section>}
        {section==="logs"&&<section className="surface form">
          <div className="section-title"><div><h2>Kaydedilmiş Ayar Değişiklikleri</h2>
            <p>Veritabanındaki işlem geçmişi; gerçek piyasa işlemi değildir.</p></div>
            <button className="button outline" disabled={!connected||auditBusy}
              onClick={()=>{void loadAudit();}}>{auditBusy?"Yükleniyor...":"Geçmişi Yenile"}</button>
          </div>
          {!connected?<div className="empty audit-empty"><FileClock size={30}/>
            <strong>Yerel API bağlantısı gerekiyor</strong>
            <span>Olay kayıtları yüklenmedi. Sahte veri gösterilmiyor.</span></div>:
           auditEvents.length===0?<div className="empty audit-empty"><FileClock size={30}/>
            <strong>Gösterilecek kayıt bulunmuyor</strong>
            <span>Henüz ayar değişikliği yok veya yeni bir kayıt alınamadı.</span></div>:
           <div className="audit-list" aria-label="Ayar değişiklik geçmişi">
              {auditEvents.map(entry=><div className="audit-entry" key={entry.version}>
                <span className="audit-symbol"><FileClock size={17}/></span>
                <div><strong>Ayarlar güncellendi</strong>
                  <small>Sürüm {entry.version} · {new Date(entry.created_at).toLocaleString("tr-TR")}</small></div>
                <span className="audit-status">Kaydedildi</span>
              </div>)}
            </div>}
        </section>}
        {(section==="strategies"||section==="positions")&&<section className="surface empty-page">
          <FileClock size={34}/><h2>Bu modül henüz geliştirme aşamasında</h2>
          <p>İşlem ve performans verileri uydurulmaz. Sonraki paketlerde açılacak.</p>
          <button className="button outline" onClick={()=>setSection("dashboard")}>Genel Bakışa Dön</button>
        </section>}
        <footer>TradePilot · Local Development · Gerçek işlemler kapalı</footer>
      </div>
    </main>
    {dialog&&<div className="modal-layer"><button className="modal-scrim" tabIndex={-1} aria-hidden="true"
      onClick={()=>setDialog(false)}/><section ref={dialogRef} tabIndex={-1} onKeyDown={handleDialogKeyDown} className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-heading">
      <div className="modal-heading"><div className="modal-icon"><LockKeyhole size={23}/></div>
        <button onClick={()=>setDialog(false)} aria-label="Kapat"><X size={20}/></button></div>
      <h2 id="modal-heading">Yerel API Bağlantısı</h2>
      <p>Sadece geliştirme amaçlı. Anahtar tarayıcı depolamasına kaydedilmez.</p>
      <label>API adresi<input value={address} onChange={e=>setAddress(e.target.value)} autoComplete="off"/></label>
      <label>Yerel yönetici anahtarı<div className="secret"><input type={visible?"text":"password"}
        value={secret} onChange={e=>setSecret(e.target.value)} autoComplete="off" spellCheck={false}/>
        <button aria-label={visible?"Gizle":"Göster"} onClick={()=>setVisible(!visible)}>{visible?<EyeOff size={19}/>:<Eye size={19}/>}</button></div></label>
      <button className="button primary full" disabled={busy} onClick={connect}>{busy?"Bağlanıyor":"Bağlan"} <ChevronRight size={17}/></button>
      <small>Bu bağlantıyı herkese açık internette kullanma.</small>
    </section></div>}
  </div>;
}
