"use client";

import { Clock3, FileClock, LockKeyhole, RefreshCw, Wallet } from "lucide-react";
import { useEffect, useState } from "react";
import { RealizedTradeChart } from "./RecordedPaperCharts";

type PaperAccount = {
  initial_capital_usdt: string; cash_usdt: string;
  realized_pnl_usdt: string; paid_fees_usdt: string;
};
type PaperQuote = {
  source: string; symbol: string; interval: string; observed_at_ms: number;
  reference_price: string; fee_bps: number; slippage_bps: number;
};
type PaperEvent = {
  id: number; event_type: string; created_at: string; cash_delta_usdt: string;
  realized_pnl_delta_usdt: string; fee_usdt: string; quote: PaperQuote | null;
};
type PaperPosition = {
  symbol: string; status: string; quantity: string; entry_fill_price: string;
  stop_price: string; opened_at: string;
};
type Worker = {
  requested_enabled: boolean | null;
  process_verified_running: false;
  last_status: string | null;
  last_attempt_at_ms: number | null;
  next_allowed_at_ms: number | null;
  consecutive_failures: number | null;
};
type Overview = {
  mode: "paper"; source: "persisted_paper_spot";
  initialized: boolean; account: PaperAccount | null;
  total_positions: number; open_positions: number; total_events: number;
  positions: PaperPosition[]; events: PaperEvent[]; worker: Worker;
};
type LoadState = "idle" | "loading" | "ready" | "error";

function localOrigin(raw: string): string {
  const url = new URL(raw);
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) ||
      url.username || url.password) {
    throw new Error("API yalnızca yerel localhost veya 127.0.0.1 olabilir.");
  }
  return url.origin;
}
function utcMs(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return new Date(value).toLocaleString("tr-TR", {timeZone: "UTC"}) + " UTC";
}
function utc(value: string): string {
  return new Date(value).toLocaleString("tr-TR", {timeZone: "UTC"}) + " UTC";
}
function amount(value: string): string {
  // Preserve the exact decimal sent by the ledger; no floating-point PnL math.
  return value + " USDT";
}
const names: Record<string, string> = {
  account_opened: "Sanal hesap açılışı",
  spot_buy: "Sanal alış",
  spot_sell: "Sanal satış",
};

export default function PaperDashboardOverview({connected, apiAddress, token}: {
  connected: boolean; apiAddress: string; token: string;
}) {
  const [state, setState] = useState<LoadState>("idle");
  const [snapshot, setSnapshot] = useState<Overview | null>(null);
  const [message, setMessage] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!connected) return;
    const abort = new AbortController();
    async function read() {
      setState("loading");
      setSnapshot(null);
      setMessage("");
      try {
        const response = await fetch(localOrigin(apiAddress) + "/api/v1/paper/dashboard?limit=10", {
          headers: {"X-Admin-Token": token}, cache: "no-store", signal: abort.signal,
        });
        if (!response.ok) throw new Error("PAPER gösterge verisi alınamadı (HTTP " + response.status + ").");
        const data: Overview = await response.json();
        if (data.mode !== "paper" || data.source !== "persisted_paper_spot" ||
            typeof data.initialized !== "boolean" || !data.worker ||
            data.worker.process_verified_running !== false ||
            !Array.isArray(data.events) || !Array.isArray(data.positions) ||
            (data.initialized && !data.account)) {
          throw new Error("Beklenmeyen PAPER özet yanıtı.");
        }
        if (!abort.signal.aborted) {
          setSnapshot(data); setState("ready");
        }
      } catch (reason) {
        if (!abort.signal.aborted) {
          setMessage(reason instanceof Error ? reason.message : "Özet okunamadı.");
          setState("error");
        }
      }
    }
    void read();
    return () => abort.abort();
  }, [connected, apiAddress, token, revision]);

  const latest = snapshot?.events.find(event => event.quote !== null);
  return <section className="surface paper-overview" aria-label="PAPER sanal performans ve worker özeti">
    <div className="section-title">
      <div>
        <h2>PAPER Hesap ve Performans</h2>
        <p>Yalnız veritabanına kaydedilmiş sanal işlemler. Güncel borsa bakiyesi değildir.</p>
      </div>
      <button className="button outline" onClick={() => setRevision(n => n + 1)}
        disabled={!connected || state === "loading"}>
        <RefreshCw size={15}/> {state === "loading" ? "Yükleniyor..." : "Özeti Yenile"}
      </button>
    </div>
    {!connected ? <div className="empty paper-overview-empty"><LockKeyhole size={27}/>
      <strong>Yerel API bağlantısı gerekiyor</strong>
      <span>Sanal kâr veya bakiye uydurulmaz; bağlantı kurunca kayıtlı bilgiler gösterilir.</span>
    </div> : state === "loading" ? <div className="empty paper-overview-empty" aria-live="polite">
      <RefreshCw size={27}/><strong>PAPER kayıtları okunuyor...</strong>
    </div> : state === "error" ? <div className="empty paper-overview-empty" role="alert">
      <FileClock size={27}/><strong>PAPER özetine ulaşılamadı</strong><span>{message}</span>
    </div> : state === "ready" && snapshot ? <>
      {snapshot.initialized && snapshot.account ? <>
        <div className="paper-overview-metrics">
          <div><span>Başlangıç sermayesi (sanal)</span><strong>{amount(snapshot.account.initial_capital_usdt)}</strong></div>
          <div><span>Kaydedilmiş nakit (sanal)</span><strong>{amount(snapshot.account.cash_usdt)}</strong></div>
          <div><span>Gerçekleşmiş PnL (sanal)</span><strong>{amount(snapshot.account.realized_pnl_usdt)}</strong></div>
          <div><span>Ödenen komisyon (sanal)</span><strong>{amount(snapshot.account.paid_fees_usdt)}</strong></div>
        </div>
        <p className="paper-overview-note">Açık pozisyon: {snapshot.open_positions} · Toplam pozisyon: {snapshot.total_positions} · İşlem olayı: {snapshot.total_events}.</p>
        {snapshot.positions.filter(position => position.status === "open").length > 0 &&
          <div className="paper-overview-open">
            <strong><Wallet size={16}/> Açık sanal pozisyon</strong>
            {snapshot.positions.filter(position => position.status === "open").map((position, index) =>
              <p key={index}>{position.symbol} · {position.quantity} adet · Giriş {amount(position.entry_fill_price)} · Stop varsayımı {amount(position.stop_price)}</p>
            )}
          </div>}
      </> : <div className="paper-overview-placeholder"><Wallet size={20}/>
        <span>Henüz PAPER Spot hesabı oluşturulmamış. Sanal tutar gösterilmiyor.</span>
      </div>}
      <div className="paper-overview-worker" aria-label="Worker son bilinen deneme bilgileri">
        <div className="paper-overview-heading"><Clock3 size={17}/><strong>Worker — Son bilinen durum</strong></div>
        <p>Çalışan servis doğrulanmadı. Ayardaki etkinleştirme isteği, bir worker'ın gerçekten çalıştığı anlamına gelmez.</p>
        <dl>
          <div><dt>Bot ayarı</dt><dd>{snapshot.worker.requested_enabled === null ? "Bilinmiyor" : snapshot.worker.requested_enabled ? "Etkinleştirme istenmiş" : "Kapalı"}</dd></div>
          <div><dt>Son deneme</dt><dd>{utcMs(snapshot.worker.last_attempt_at_ms)}</dd></div>
          <div><dt>Son sonuç</dt><dd>{snapshot.worker.last_status ?? "Kayıt yok"}</dd></div>
          <div><dt>Bir sonraki izinli deneme</dt><dd>{utcMs(snapshot.worker.next_allowed_at_ms)}</dd></div>
        </dl>
      </div>
      {snapshot.initialized && <RealizedTradeChart events={snapshot.events}/>}
      <h3>Son sanal işlemler ve fiyat kaynağı</h3>
      {snapshot.events.length === 0 ? <p className="paper-overview-note">Henüz işlem günlüğü kaydı bulunmuyor.</p> :
        <div className="paper-overview-events" aria-label="Son PAPER işlem olayları">
          {snapshot.events.map(event => <article key={event.id}>
            <div className="paper-overview-event-title"><strong>{names[event.event_type] ?? "Sanal muhasebe kaydı"}</strong><small>{utc(event.created_at)}</small></div>
            <span>Nakit hareketi: {amount(event.cash_delta_usdt)}</span>
            <span>Gerçekleşmiş sonuç: {amount(event.realized_pnl_delta_usdt)}</span>
            {event.quote ? <div className="paper-overview-quote">
              <strong>{event.quote.symbol} · {event.quote.interval} · {amount(event.quote.reference_price)}</strong>
              <small>Binance public ticker · Yerel gözlem: {utcMs(event.quote.observed_at_ms)}</small>
              <small>Varsayımsal komisyon {event.quote.fee_bps} bps · kayma {event.quote.slippage_bps} bps. Gerçek emir fiyatı değildir.</small>
            </div> : <small>Bu olay için public fiyat gözlemi kaydı yok; işlem fiyatı doğrulanmış olarak gösterilmez.</small>}
          </article>)}
        </div>}
      {latest?.quote && <p className="paper-overview-note">Son kayıtlı public fiyat gözlemi: {utcMs(latest.quote.observed_at_ms)}. Bu anlık piyasa fiyatı değildir.</p>}
    </> : null}
    <p className="paper-overview-disclaimer">PAPER ONLY · Açık pozisyonun gerçekleşmemiş PnL'si veya portföy özsermayesi hesaplanmaz. Worker heartbeat ve gerçek Binance emir yürütme bilgisi bulunmuyor.</p>
  </section>;
}
