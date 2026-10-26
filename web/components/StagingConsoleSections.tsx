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
type RiskProfileView = {
  available: boolean; selectable: boolean; reason: string;
  active_profile_version: number|null; active_risk_level: number|null;
  runtime_revision: number|null; bot_state: string;
  schema_version: string|null; limits: Record<string, unknown>|null;
};
type ResearchPrediction = {
  signal_id: string; model_version: string; artifact_sha256: string;
  feature_schema_version: string; feature_sha256: string;
  symbol: string; interval: string; action: string; reason_code: string;
  observed_close_at_ms: number; received_at_ms: number; outcome_due_at_ms: number;
  probabilities: Record<string,string|number>|null; outcome: string|null;
  evaluated_at_ms: number|null; outcome_source_sha256: string|null;
  approval_status: string;
};
type ResearchView = {
  available: boolean; source: string; status: string; server_time: string;
  predictions: ResearchPrediction[]; metrics_available: boolean; metrics_reason: string;
};
type AuditLog = {events: Array<{version:number;action:string;created_at:string}>};
type WorkerRecord = {
  requested_enabled: boolean|null; process_verified_running: false;
  last_status: string|null; last_attempt_at_ms: number|null;
  next_allowed_at_ms: number|null; consecutive_failures: number|null;
};
type WorkerEnvelope = {mode:"paper"; source:"persisted_paper_spot"; worker:WorkerRecord};
const safeTime = (v:unknown):boolean =>
  v === null || (typeof v === "number" && Number.isSafeInteger(v) && v >= 0);
function safeWorkerEnvelope(value:WorkerEnvelope):boolean {
  const w = value?.worker;
  return value?.mode === "paper" && value.source === "persisted_paper_spot" &&
    !!w && w.process_verified_running === false &&
    (w.requested_enabled === null || typeof w.requested_enabled === "boolean") &&
    (w.last_status === null || (typeof w.last_status === "string" &&
      ["disabled","ready","no_signal","paper_recorded","source_blocked",
        "rate_limited","error"].includes(w.last_status))) &&
    safeTime(w.last_attempt_at_ms) && safeTime(w.next_allowed_at_ms) &&
    safeTime(w.consecutive_failures);
}

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
function safeResearch(value: ResearchView): boolean {
  return typeof value?.available === "boolean" &&
    value.source === "isolated_ai_research_store" &&
    value.status === (value.available ? "available" : "unavailable") &&
    Number.isFinite(Date.parse(value.server_time)) &&
    typeof value.metrics_available === "boolean" && typeof value.metrics_reason === "string" &&
    Array.isArray(value.predictions) && value.predictions.length <= 30 &&
    value.predictions.every(row => typeof row.signal_id === "string" &&
      typeof row.model_version === "string" && typeof row.artifact_sha256 === "string" &&
      /^[0-9a-f]{64}$/.test(row.artifact_sha256) &&
      typeof row.feature_schema_version === "string" &&
      /^[0-9a-f]{64}$/.test(row.feature_sha256) &&
      typeof row.reason_code === "string" &&
      ["BTCUSDT","ETHUSDT"].includes(row.symbol) &&
      ["1m","5m","15m"].includes(row.interval) &&
      ["BUY","SELL","HOLD","NO_TRADE"].includes(row.action) &&
      [row.observed_close_at_ms,row.received_at_ms,row.outcome_due_at_ms]
        .every(time => Number.isSafeInteger(time) && time >= 0) &&
      (row.evaluated_at_ms === null ||
        (Number.isSafeInteger(row.evaluated_at_ms) && row.evaluated_at_ms >= 0)) &&
      (row.outcome === null || typeof row.outcome === "string") &&
      (row.outcome_source_sha256 === null || /^[0-9a-f]{64}$/.test(row.outcome_source_sha256)) &&
      (row.probabilities === null || (typeof row.probabilities === "object" &&
        ["win","loss","flat"].every(key => {
          const probability = row.probabilities?.[key];
          const parsed = typeof probability === "string" || typeof probability === "number"
            ? Number(probability) : Number.NaN;
          return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1;
        }))) &&
      row.approval_status === "RESEARCH_ONLY_NOT_APPROVED");
}
function utc(value: number | string): string {
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("tr-TR", {timeZone:"UTC"}) + " UTC" : "—";
}

export default function StagingConsoleSections({
  section, base, onExpired,
}: {
  section: DetailSection; base: string; onExpired: () => void;
}) {
  const [symbol, setSymbol] = useState<SpotSymbol>("BTCUSDT");
  const [interval, setInterval] = useState<SpotInterval>("1m");
  const [reload, setReload] = useState(0);
  const [market, setMarket] = useState<MarketHistory|null>(null);
  const [settings, setSettings] = useState<SettingsSnapshot|null>(null);
  const [riskProfile, setRiskProfile] = useState<RiskProfileView|null>(null);
  const [research, setResearch] = useState<ResearchView|null>(null);
  const [audit, setAudit] = useState<AuditLog|null>(null);
  const [worker, setWorker] = useState<WorkerRecord|null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      setLoading(true); setMessage("");
      setMarket(null); setSettings(null); setAudit(null); setWorker(null);
      setRiskProfile(null); setResearch(null);
      const query = new URLSearchParams({symbol, interval,limit:"30"});
      const endpoint = section === "market" ? "/api/v1/market/candles?" + query.toString()
        : section === "logs" ? "/api/v1/audit?limit=30"
        : section === "strategies" ? "/api/v1/ai/research/predictions?limit=30"
        : "/api/v1/settings";
      try {
        const response = await fetch(base + (section === "risk"
          ? "/api/v1/risk/profiles" : endpoint), {
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
        } else if (section === "risk") {
          const profile = data as RiskProfileView;
          if (typeof profile.available !== "boolean" ||
              typeof profile.selectable !== "boolean" || typeof profile.reason !== "string" ||
              (profile.active_profile_version !== null &&
                !Number.isSafeInteger(profile.active_profile_version)) ||
              (profile.active_risk_level !== null &&
                (!Number.isSafeInteger(profile.active_risk_level) ||
                  profile.active_risk_level < 1 || profile.active_risk_level > 5)) ||
              (profile.limits !== null && typeof profile.limits !== "object")) {
            throw new Error("Bağımsız RiskGuard profili doğrulanamadı.");
          }
          setRiskProfile(profile);
        } else if (section === "strategies") {
          const view = data as ResearchView;
          if (!safeResearch(view)) throw new Error("AI araştırma veri sözleşmesi doğrulanamadı.");
          setResearch(view);
        } else {
          const snapshot = data as SettingsSnapshot;
          if (!isSettings(snapshot)) throw new Error("PAPER ayarları doğrulanamadı.");
          setSettings(snapshot);
          if (section === "bot") {
            const latest = await fetch(base + "/api/v1/paper/dashboard?limit=1", {
              credentials:"include",cache:"no-store",signal:controller.signal,
            });
            if (latest.status === 401) {if (!controller.signal.aborted) onExpired(); return;}
            if (!latest.ok) throw new Error("Son PAPER worker kaydı okunamadı.");
            const workerResult = await latest.json() as WorkerEnvelope;
            if (!safeWorkerEnvelope(workerResult)) throw new Error("Worker kaydı doğrulanamadı.");
            if (controller.signal.aborted) return;
            setWorker(workerResult.worker);
          }
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

  return <section className="staging-module" aria-label="Seçilen yönetim bölümü">
    <div className="staging-module-heading">
      <div>
        <span className="staging-module-kicker">TRADEPILOT · KAYITLI PAPER</span>
      <h2>{section === "market" ? "Piyasa Verileri" : section === "bot" ? "Bot Yönetimi" :
          section === "risk" ? "Risk Yönetimi" : section === "strategies" ? "AI Araştırma" :
          "Ayar Olay Kayıtları"}</h2>
      </div>
      {section !== "strategies" && <button type="button" className="staging-secondary"
        disabled={loading} onClick={() => setReload(v => v+1)}>Verileri yenile</button>}
    </div>
    {section === "strategies" ? <>
      <p className="ai-research-badge">RESEARCH ONLY — NOT APPROVED</p>
      <p>Prospektif kayıtlar yalnız araştırmadır; işlem sinyali veya onay değildir.</p>
      {loading && <p role="status" className="staging-muted">AI araştırma kayıtları okunuyor…</p>}
      {message && <p role="status" className="staging-notice">{message}</p>}
      {research && <>
        <p role="status" className="staging-muted">{research.available
          ? `Kaynak: ${research.source} · Sunucu zamanı: ${utc(research.server_time)}`
          : `Araştırma deposu kullanılamıyor (${research.metrics_reason}); kayıt gösterilmedi.`}</p>
        <p>Kalibrasyon/Brier/log loss ölçümü: {research.metrics_available
          ? "kayıtlı değerlendirme mevcut" : `mevcut değil (${research.metrics_reason})`}</p>
        {research.predictions.length === 0
          ? <p className="staging-empty">Prospektif araştırma kaydı yok; tahmin üretilmez.</p>
          : <div className="staging-table-wrap"><table className="staging-table">
            <thead><tr><th scope="col">Varlık / aralık</th><th scope="col">Yön</th>
              <th scope="col">Model / şema</th><th scope="col">Gözlem / kayıt (UTC)</th>
              <th scope="col">Olasılıklar</th><th scope="col">Prospektif sonuç</th>
              <th scope="col">Durum</th></tr></thead>
            <tbody>{research.predictions.map(row => <tr key={row.signal_id}>
              <td>{row.symbol} · {row.interval}</td><td>{row.action}</td>
              <td>{row.model_version} · {row.feature_schema_version}</td>
              <td>{utc(row.observed_close_at_ms)} · {utc(row.received_at_ms)}
                <small> Veri yaşı: {Math.max(0, Math.floor((Date.parse(research.server_time) - row.received_at_ms) / 1000))} sn</small></td>
              <td>{row.probabilities
                ? Object.entries(row.probabilities).map(([key,value]) => `${key}: ${value}`).join(" · ")
                : "kaydedilmemiş"} · kalibrasyon doğrulanmadı</td>
              <td>{row.outcome ?? "beklemede"} · vade {utc(row.outcome_due_at_ms)}</td>
              <td>RESEARCH ONLY — NOT APPROVED</td>
            </tr>)}</tbody></table></div>}
      </>}
      <button type="button" className="staging-secondary" disabled={loading}
        onClick={() => setReload(v => v+1)}>Araştırma kayıtlarını yenile</button>
    </> : <>
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
      {section === "risk" && riskProfile && <>
        <h3>Bağımsız RiskGuard profili</h3>
        {riskProfile.available ? riskProfile.active_profile_version === null
          ? <p role="status" className="staging-empty">RiskGuard deposu erişilebilir, ancak kullanıcı onaylı etkin profil yok. Seviyeler 1–5 seçilemez.</p>
          : <div className="staging-keyvalues">
            <div><span>Risk seviyesi</span><strong>{riskProfile.active_risk_level ?? "—"}</strong></div>
            <div><span>Profil sürümü</span><strong>{riskProfile.active_profile_version}</strong></div>
            <div><span>Şema sürümü</span><strong>{riskProfile.schema_version ?? "—"}</strong></div>
            <div><span>Bot durumu</span><strong>{riskProfile.bot_state}</strong></div>
          </div>
          : <p role="status" className="staging-empty">Bağımsız RiskGuard profili kullanılamıyor ({riskProfile.reason}). Risk seviyesi seçimi kapalı; uygulama ayarları RiskGuard profili yerine geçmez.</p>}
        <p className="staging-data-warning">Bu özel önizleme RiskGuard limitlerini etkinleştirmez veya değiştirmez. Profil yönetimi izole RiskGuard deposu, kullanıcı onayı ve backend sürüm denetimi doğrulanınca açılabilir.</p>
      </>}
      {section === "bot" && settings && <>
        <div className="staging-keyvalues">
          <div><span>İşlem modu</span><strong>PAPER · Gerçek emir yok</strong></div>
          <div><span>Bot çalışma tercihi</span><strong>{settings.settings.bot_enabled ? "Etkinleştirme istenmiş (çalışıyor kanıtı yok)" : "Kapalı"}</strong></div>
          <div><span>Manuel onay</span><strong>{settings.settings.manual_approval ? "Zorunlu" : "Kapalı (değiştirmeyin)"}</strong></div>
          <div><span>Spot sanal bütçe</span><strong>{settings.settings.spot_capital_usdt} USDT (ayar limiti)</strong></div>
          <div><span>Sanal Futures bütçe</span><strong>{settings.settings.futures_capital_usdt} USDT (işlem motoru yok)</strong></div>
          <div><span>İzinli semboller</span><strong>{settings.settings.allowed_symbols.join(", ")}</strong></div>
          <div><span>Ayar sürümü</span><strong>{settings.version}</strong></div>
        </div>
        {section === "bot" && <>
          <h3>Otomatik PAPER — son kayıtlı deneme</h3>
          <div className="staging-keyvalues">
            <div><span>Son worker sonucu</span><strong>{worker?.last_status ?? "Kayıt yok"}</strong></div>
            <div><span>Son deneme (UTC)</span><strong>{worker?.last_attempt_at_ms
              ? utc(worker.last_attempt_at_ms) : "Kayıt yok"}</strong></div>
            <div><span>Bir sonraki izinli deneme</span><strong>{worker?.next_allowed_at_ms
              ? utc(worker.next_allowed_at_ms) : "Kayıt yok"}</strong></div>
            <div><span>Ardışık kaynak hataları</span><strong>{worker?.consecutive_failures ?? 0}</strong></div>
          </div>
          <p className="staging-data-warning">Kaydedilmiş worker sonucu veya etkinleştirme
            tercihi, çalışan bir işlem sürecinin kanıtı değildir. Bu ekranda
            otomatik bot başlatma veya gerçek Binance emir yetkisi bulunmaz.
            İlk otomatik PAPER denemesi yalnızca yerel bilgisayarda ayrı onayla başlatılır.</p>
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
