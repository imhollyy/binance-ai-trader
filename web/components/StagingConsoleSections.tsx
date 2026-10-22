"use client";

import {useEffect, useState} from "react";
import {StoredCloseChart} from "./RecordedPaperCharts";

export type ConsoleSection = "dashboard" | "market" | "bot" | "risk" | "strategies" | "positions" | "logs";
type DetailSection = Exclude<ConsoleSection, "dashboard" | "positions">;
type SpotSymbol = "BTCUSDT" | "ETHUSDT";
type SpotInterval = "1m" | "5m" | "15m";
type Candle = {
  open_time_ms: number; close_time_ms: number;
  open: string; high: string; low: string; close: string; volume: string;
};
type MarketHistory = {
  source: "stored_public_spot"; symbol: SpotSymbol; interval: SpotInterval;
  candles: Candle[];
};
type RiskSettings = {
  mode: "paper"; market_type: "spot" | "futures"; bot_enabled: boolean;
  spot_capital_usdt: number; futures_capital_usdt: number;
  risk_per_trade_pct: number; daily_loss_limit_pct: number;
  max_drawdown_pct: number; max_open_positions: number;
  max_leverage: number; stop_loss_required: true;
  manual_approval: boolean; allowed_symbols: string[];
};
type SettingsSnapshot = {version: number; settings: RiskSettings};
type AuditLog = {events: Array<{version:number;action:string;created_at:string}>};

function isSettings(value: SettingsSnapshot): boolean {
  const r = value?.settings;
  return !!r && Number.isSafeInteger(value.version) && value.version >= 0 &&
    r.mode === "paper" && (r.market_type === "spot" || r.market_type === "futures") &&
    typeof r.bot_enabled === "boolean" && typeof r.manual_approval === "boolean" &&
    r.stop_loss_required === true &&
    Array.isArray(r.allowed_symbols) && r.allowed_symbols.every(s => typeof s === "string") &&
    [r.spot_capital_usdt, r.futures_capital_usdt, r.risk_per_trade_pct,
      r.daily_loss_limit_pct, r.max_drawdown_pct, r.max_open_positions, r.max_leverage]
      .every(n => typeof n === "number" && Number.isFinite(n));
}

function safeHistory(value: MarketHistory, symbol: SpotSymbol, interval: SpotInterval): boolean {
  return value?.source === "stored_public_spot" &&
    value.symbol === symbol && value.interval === interval &&
    Array.isArray(value.candles) && value.candles.length <= 100 &&
    value.candles.every(c => Number.isSafeInteger(c.open_time_ms) &&
      Number.isSafeInteger(c.close_time_ms) && c.open_time_ms <= c.close_time_ms &&
      [c.open,c.high,c.low,c.close,c.volume].every(p => typeof p === "string" && /^\d+(?:\.\d+)?$/.test(p)));
}
function safeAudit(value: AuditLog): boolean {
  return Array.isArray(value?.events) && value.events.length <= 30 &&
    value.events.every(e => Number.isSafeInteger(e.version) && e.version >= 0 &&
      e.action === "settings_updated" && typeof e.created_at === "string" &&
      Number.isFinite(Date.parse(e.created_at)));
}
function validNumber(value: string, min: number, max: number): boolean {
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) return false;
  const n = Number(value);
  return Number.isFinite(n) && n >= min && n <= max;
}
function utc(value: number | string): string {
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("tr-TR", {timeZone:"UTC"}) + " UTC" : "—";
}

export default function StagingConsoleSections({
  section, base, role, csrf, onExpired,
}: {
  section: DetailSection; base: string; role: string; csrf: string; onExpired: () => void;
}) {
  const [symbol, setSymbol] = useState<SpotSymbol>("BTCUSDT");
  const [interval, setInterval] = useState<SpotInterval>("1m");
  const [reload, setReload] = useState(0);
  const [market, setMarket] = useState<MarketHistory|null>(null);
  const [settings, setSettings] = useState<SettingsSnapshot|null>(null);
  const [audit, setAudit] = useState<AuditLog|null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [approval, setApproval] = useState(false);
  const [riskTrade, setRiskTrade] = useState("");
  const [riskDay, setRiskDay] = useState("");
  const [riskDrawdown, setRiskDrawdown] = useState("");

  useEffect(() => {
    if (section === "strategies") {setLoading(false); return;}
    const controller = new AbortController();
    async function load() {
      setLoading(true); setMessage("");
      setMarket(null); setSettings(null); setAudit(null);
      setApproval(false);
      const query = new URLSearchParams({symbol, interval,limit:"30"});
      const endpoint = section === "market" ? "/api/v1/market/candles?" + query.toString()
        : section === "logs" ? "/api/v1/audit?limit=30" : "/api/v1/settings";
      try {
        const response = await fetch(base + endpoint, {
          credentials:"include", cache:"no-store", signal:controller.signal,
        });
        if (response.status === 401) {
          if (!controller.signal.aborted) onExpired();
          return;
        }
        if (!response.ok) throw new Error("Kayıtlı bilgiler okunamadı (HTTP " + response.status + ").");
        const data: unknown = await response.json();
        if (controller.signal.aborted) return;
        if (section === "market") {
          const history = data as MarketHistory;
          if (!safeHistory(history,symbol,interval)) throw new Error("Piyasa verisinin kaynağı doğrulanamadı.");
          setMarket(history);
        } else if (section === "logs") {
          const events = data as AuditLog;
          if (!safeAudit(events)) throw new Error("Denetim günlüğü doğrulanamadı.");
          setAudit(events);
        } else {
          const snapshot = data as SettingsSnapshot;
          if (!isSettings(snapshot)) throw new Error("PAPER ayarları doğrulanamadı.");
          setSettings(snapshot);
          setRiskTrade(String(snapshot.settings.risk_per_trade_pct));
          setRiskDay(String(snapshot.settings.daily_loss_limit_pct));
          setRiskDrawdown(String(snapshot.settings.max_drawdown_pct));
        }
      } catch (error) {
        if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "Bağlantı hatası.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [base, section, symbol, interval, reload, onExpired]);

  const validRisks = validNumber(riskTrade, 0.05, 2) &&
    validNumber(riskDay, 0.1, 5) && validNumber(riskDrawdown, 1, 15);

  async function savePaperRisk() {
    if (saving || role !== "operator" || !approval || !validRisks || !settings ||
        settings.settings.bot_enabled || !settings.settings.manual_approval) return;
    setSaving(true); setMessage("");
    try {
      const candidate: RiskSettings = {
        ...settings.settings,
        // Safeguard: never enable worker or Futures execution from this web form.
        bot_enabled:false,
        risk_per_trade_pct:Number(riskTrade),
        daily_loss_limit_pct:Number(riskDay),
        max_drawdown_pct:Number(riskDrawdown),
      };
      const response = await fetch(base + "/api/v1/settings", {
        method:"PUT", credentials:"include", cache:"no-store",
        headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,
          "X-Paper-Settings-Approval":"PAPER_SETTINGS_ONLY"},
        body:JSON.stringify({expected_version:settings.version, settings:candidate}),
      });
      if (response.status === 401) {onExpired(); return;}
      if (response.status === 409) throw new Error("Ayar sürümü değişti. Yenile ve yeniden kontrol et.");
      if (response.status === 403) throw new Error("Operatör yetkisi veya CSRF onayı reddedildi.");
      if (response.status === 422) throw new Error("Risk sınırları reddedildi; ayar kaydedilmedi.");
      if (!response.ok) throw new Error("Ayar kaydedilemedi (HTTP " + response.status + ").");
      const updated: SettingsSnapshot = await response.json();
      if (!isSettings(updated) || updated.version !== settings.version + 1 ||
          updated.settings.bot_enabled !== false) throw new Error("Sunucu ayar doğrulaması başarısız.");
      setSettings(updated);
      setMessage("PAPER risk ayarları sunucuda kaydedildi.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Kaydetme hatası.");
    } finally {
      setApproval(false); setSaving(false);
    }
  }

  return <section className="staging-module" aria-label="Seçilen yönetim bölümü">
    <div className="staging-module-heading">
      <div>
        <span className="staging-module-kicker">TRADEPILOT · KAYITLI PAPER</span>
        <h2>{section === "market" ? "Piyasa Verileri" : section === "bot" ? "Bot Yönetimi" :
          section === "risk" ? "Risk Yönetimi" : section === "strategies" ? "Stratejiler" :
          "Ayar Olay Kayıtları"}</h2>
      </div>
      {section !== "strategies" && <button type="button" className="staging-secondary"
        disabled={loading || saving} onClick={() => setReload(v => v+1)}>Verileri yenile</button>}
    </div>
    {section === "strategies" ? <div className="staging-empty">
      SMA çaprazlama ve kapalı mum sinyal denemeleri geliştirme ortamında mevcut.
      Bu özel beta ekranında çalışan otomatik AI stratejisi veya kârlılık sonucu yok.
      Otomatik worker ve gerçek emirler kapalıdır.
    </div> : <>
      {loading && <p role="status" className="staging-muted">Kaydedilmiş veriler okunuyor…</p>}
      {message && <p role="status" className="staging-notice">{message}</p>}
      {section === "market" && <>
        <div className="staging-market-controls">
          <label>Parite
            <select value={symbol} onChange={e => setSymbol(e.target.value as SpotSymbol)}>
              <option value="BTCUSDT">BTC / USDT</option>
              <option value="ETHUSDT">ETH / USDT</option>
            </select>
          </label>
          <label>Zaman aralığı
            <select value={interval} onChange={e => setInterval(e.target.value as SpotInterval)}>
              <option value="1m">1 dakika</option>
              <option value="5m">5 dakika</option>
              <option value="15m">15 dakika</option>
            </select>
          </label>
        </div>
        <p className="staging-data-warning">Bu veriler yalnız PostgreSQL'e kaydedilmiş kapanmış mumlardır;
          güncel Binance fiyatı veya otomatik işlem sinyali değildir.</p>
        {market && <>
          <StoredCloseChart symbol={symbol} interval={interval} candles={market.candles}/>
          <div className="staging-table-wrap"><table className="staging-table">
            <thead><tr><th scope="col">Kapanış (UTC)</th><th scope="col">Açılış</th>
              <th scope="col">En yüksek</th><th scope="col">En düşük</th><th scope="col">Kapanış</th></tr></thead>
            <tbody>{market.candles.map(c => <tr key={c.open_time_ms}>
              <td>{utc(c.close_time_ms)}</td><td>{c.open}</td><td>{c.high}</td>
              <td>{c.low}</td><td>{c.close}</td>
            </tr>)}</tbody>
          </table></div>
          {market.candles.length === 0 && <p className="staging-empty">Kayıtlı mum bulunamadı; fiyat uydurulmaz.</p>}
        </>}
      </>}
      {(section === "bot" || section === "risk") && settings && <>
        <div className="staging-keyvalues">
          <div><span>İşlem modu</span><strong>PAPER · Gerçek emir yok</strong></div>
          <div><span>Bot çalışma tercihi</span><strong>{settings.settings.bot_enabled ? "Etkinleştirme istenmiş (çalışıyor kanıtı yok)" : "Kapalı"}</strong></div>
          <div><span>Manuel onay</span><strong>{settings.settings.manual_approval ? "Zorunlu" : "Kapalı (değiştirmeyin)"}</strong></div>
          <div><span>Spot sanal bütçe</span><strong>{settings.settings.spot_capital_usdt} USDT (ayar limiti)</strong></div>
          <div><span>Sanal Futures bütçe</span><strong>{settings.settings.futures_capital_usdt} USDT (işlem motoru yok)</strong></div>
          <div><span>İzinli semboller</span><strong>{settings.settings.allowed_symbols.join(", ")}</strong></div>
          <div><span>Ayar sürümü</span><strong>{settings.version}</strong></div>
        </div>
        {section === "bot" && <p className="staging-data-warning">
          Kaydedilmiş etkinleştirme tercihi çalışan bir bot veya scheduler anlamına gelmez.
          Otomatik çalıştırma bu özel beta sayfasından yapılamaz.
        </p>}
        {section === "risk" && <>
          <h3>Risk sınırları</h3>
          <div className="staging-risk-grid">
            <label>İşlem başı tahmini risk (%) <small>0,05 – 2</small>
              <input type="number" min="0.05" max="2" step="0.05" value={riskTrade}
                readOnly={role !== "operator"} onChange={e=>setRiskTrade(e.target.value)}/>
            </label>
            <label>Günlük sanal zarar sınırı (%) <small>0,1 – 5</small>
              <input type="number" min="0.1" max="5" step="0.1" value={riskDay}
                readOnly={role !== "operator"} onChange={e=>setRiskDay(e.target.value)}/>
            </label>
            <label>Maksimum sanal düşüş (%) <small>1 – 15</small>
              <input type="number" min="1" max="15" step="0.5" value={riskDrawdown}
                readOnly={role !== "operator"} onChange={e=>setRiskDrawdown(e.target.value)}/>
            </label>
          </div>
          <p className="staging-data-warning">Stop-loss zorunlu. Stop seviyeleri simülasyon
            varsayımıdır ve otomatik stop emri oluşturmaz. Açık pozisyon limiti:
            {settings.settings.max_open_positions}. Gerçekleşmiş zarar limiti güvence değildir.</p>
          {role === "operator" && <div className="staging-settings-action">
            {settings.settings.bot_enabled || !settings.settings.manual_approval
              ? <p role="alert">Bot kapalı ve manuel onay zorunlu olmadıkça web'den risk kaydı yapılamaz.</p>
              : <>
                <label><input type="checkbox" checked={approval} onChange={e=>setApproval(e.target.checked)}/>
                  Yalnız PAPER risk sınırlarını değiştirdiğimi ve otomatik bot başlatmadığımı onaylıyorum.
                </label>
                <button type="button" disabled={saving || loading || !approval || !validRisks}
                  onClick={()=>void savePaperRisk()}>{saving ? "Kaydediliyor…" : "Onaylı PAPER riskini kaydet"}</button>
              </>}
          </div>}
          {role !== "operator" && <p className="staging-muted">Görüntüleyici yalnızca okuyabilir; risk ayarı değiştiremez.</p>}
        </>}
      </>}
      {section === "logs" && audit && (audit.events.length === 0
        ? <p className="staging-empty">Henüz kayıtlı ayar değişikliği yok.</p>
        : <div className="staging-table-wrap"><table className="staging-table">
          <thead><tr><th scope="col">Sürüm</th><th scope="col">Olay</th><th scope="col">Zaman (UTC)</th></tr></thead>
          <tbody>{audit.events.map(e => <tr key={e.version}>
            <td>{e.version}</td><td>Ayarlar güncellendi</td><td>{utc(e.created_at)}</td>
          </tr>)}</tbody>
        </table></div>)}
    </>}
  </section>;
}
