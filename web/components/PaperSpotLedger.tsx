"use client";

import { FileClock, LockKeyhole, RefreshCw, Wallet } from "lucide-react";
import { useEffect, useState } from "react";

type LedgerAccount = {
  mode: "paper"; market_type: "spot"; settings_version: number;
  initial_capital_usdt: string; cash_usdt: string;
  realized_pnl_usdt: string; paid_fees_usdt: string; created_at: string;
};
type LedgerPosition = {
  trade_id: string; symbol: string; status: "open" | "closed";
  quantity: string; entry_fill_price: string; entry_notional_usdt: string;
  entry_fee_usdt: string; stop_price: string; estimated_stop_loss_usdt: string;
  exit_fill_price: string | null; exit_fee_usdt: string | null;
  net_proceeds_usdt: string | null; realized_pnl_usdt: string | null;
  gross_realized_pnl_usdt: string | null;
  opened_at: string; closed_at: string | null;
};
type LedgerEvent = {
  id: number; event_type: string; position_id: number | null;
  cash_delta_usdt: string; balance_after_usdt: string;
  fee_usdt: string; realized_pnl_delta_usdt: string;
  quantity_delta: string; created_at: string;
  observed_quote: {
    source: string; symbol: string; interval: string; observed_at_ms: number;
    reference_price: string; fee_bps: number; slippage_bps: number;
    spread_bps: number | null;
  } | null;
};
type LedgerSnapshot = {
  mode: "paper"; market_type: "spot"; source: "persisted_paper_spot";
  initialized: boolean; account: LedgerAccount | null;
  total_positions: number; open_positions: number; total_events: number;
  positions: LedgerPosition[]; events: LedgerEvent[];
};
type LoadState = "idle" | "loading" | "ready" | "error";

function localOrigin(raw: string): string {
  const url = new URL(raw);
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) ||
      url.username || url.password) {
    throw new Error("Yalnızca yerel API adresi kullanılabilir.");
  }
  return url.origin;
}

function amount(value: string | null): string {
  return value === null ? "—" : value + " USDT";
}

function time(value: string | null): string {
  return value ? new Date(value).toLocaleString("tr-TR", { timeZone: "UTC" }) + " UTC" : "—";
}

const eventLabels: Record<string, string> = {
  account_opened: "Sanal hesap açılışı",
  spot_buy: "Sanal Spot alış",
  spot_sell: "Sanal Spot kapanış",
};

export default function PaperSpotLedger({ connected, apiAddress, token }: {
  connected: boolean; apiAddress: string; token: string;
}) {
  const [state, setState] = useState<LoadState>("idle");
  const [data, setData] = useState<LedgerSnapshot | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (!connected) {
      setState("idle");
      setData(null);
      return;
    }
    const controller = new AbortController();
    async function read() {
      setState("loading");
      setError("");
      setData(null);
      try {
        const response = await fetch(localOrigin(apiAddress) + "/api/v1/paper/spot", {
          headers: { "X-Admin-Token": token },
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Sanal hesap okunamadı (HTTP " + response.status + ").");
        const payload: LedgerSnapshot = await response.json();
        if (payload.mode !== "paper" || payload.market_type !== "spot" ||
            payload.source !== "persisted_paper_spot" ||
            typeof payload.initialized !== "boolean" ||
            !Array.isArray(payload.positions) || !Array.isArray(payload.events) ||
            (payload.initialized && !payload.account)) {
          throw new Error("Beklenmeyen PAPER hesap yanıtı.");
        }
        if (!controller.signal.aborted) {
          setData(payload);
          setState("ready");
        }
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Sanal hesap okunamadı.");
          setState("error");
        }
      }
    }
    void read();
    return () => controller.abort();
  }, [connected, apiAddress, token, refresh]);

  return <section className="surface paper-ledger" aria-label="PAPER Spot hesap ve pozisyonlar">
    <div className="section-title">
      <div>
        <h2>Kalıcı PAPER Spot Defteri</h2>
        <p>Yalnızca kaydedilmiş simülasyon verileri · alış/satış işlemi yapılamaz.</p>
      </div>
      <button className="button outline" disabled={!connected || state === "loading"}
        onClick={() => setRefresh(value => value + 1)}>
        <RefreshCw size={15}/> {state === "loading" ? "Yükleniyor..." : "Hesabı Yenile"}
      </button>
    </div>
    <p className="paper-ledger-warning">
      PAPER / SIMULATION · Sanal nakit gerçek Binance bakiyesi değildir. Canlı fiyat, toplam
      portföy değeri ve gerçekleşmemiş kâr/zarar hesaplanmaz. Stop ve ücretler varsayımsaldır.
    </p>
    {!connected ? <div className="empty audit-empty"><LockKeyhole size={30}/>
      <strong>Yerel API bağlantısı gerekiyor</strong>
      <span>Sanal hesap görüntülenmedi. Gerçek veya örnek bakiye gösterilmiyor.</span>
    </div> : state === "loading" ? <div className="empty audit-empty" role="status">
      <RefreshCw size={29}/><strong>Sanal hesap okunuyor...</strong>
    </div> : state === "error" ? <div className="empty audit-empty" role="alert">
      <FileClock size={29}/><strong>Hesap okunamadı</strong><span>{error}</span>
    </div> : state === "ready" && data && !data.initialized ?
      <div className="empty audit-empty"><Wallet size={30}/>
        <strong>Henüz PAPER Spot hesabı oluşturulmamış</strong>
        <span>Veritabanında kayıt bulunmadı. Bu ekran otomatik sanal bakiye veya işlem oluşturmaz.</span>
      </div> : state === "ready" && data?.account ? <>
      <div className="paper-ledger-stats">
        <article><span>Başlangıç Sermayesi (Sanal)</span><strong>{amount(data.account.initial_capital_usdt)}</strong></article>
        <article><span>Kayıtlı Nakit (Sanal)</span><strong>{amount(data.account.cash_usdt)}</strong></article>
        <article><span>Gerçekleşmiş PnL (Sanal)</span><strong>{amount(data.account.realized_pnl_usdt)}</strong></article>
        <article><span>Ödenmiş Komisyon (Sanal)</span><strong>{amount(data.account.paid_fees_usdt)}</strong></article>
      </div>
      <p className="paper-ledger-caption">Açık pozisyon: {data.open_positions} · Toplam pozisyon: {data.total_positions} ·
        Günlük olayı: {data.total_events} · Oluşturulma: {time(data.account.created_at)}</p>
      <h3>Pozisyon Geçmişi</h3>
      {data.positions.length === 0 ? <p className="paper-ledger-caption">Henüz sanal pozisyon bulunmuyor.</p> :
        <div className="paper-ledger-list" aria-label="Sanal Spot pozisyonları">
          {data.positions.map(item => <article className="paper-ledger-record" key={item.trade_id}>
            <div className="paper-ledger-record-title">
              <strong>{item.symbol} · {item.trade_id}</strong>
              <span>{item.status === "open" ? "Açık (Sanal)" : "Kapalı (Sanal)"}</span>
            </div>
            <dl>
              <div><dt>Miktar</dt><dd>{item.quantity}</dd></div>
              <div><dt>Giriş fiyatı (Sanal)</dt><dd>{amount(item.entry_fill_price)}</dd></div>
              <div><dt>Giriş komisyonu</dt><dd>{amount(item.entry_fee_usdt)}</dd></div>
              <div><dt>Tahmini stop</dt><dd>{amount(item.stop_price)}</dd></div>
              <div><dt>Tahmini stop zararı</dt><dd>{amount(item.estimated_stop_loss_usdt)}</dd></div>
              <div><dt>Çıkış fiyatı</dt><dd>{amount(item.exit_fill_price)}</dd></div>
              <div><dt>Çıkış komisyonu</dt><dd>{amount(item.exit_fee_usdt)}</dd></div>
              <div><dt>Brüt gerçekleşmiş PnL (sanal)</dt><dd>{amount(item.gross_realized_pnl_usdt)}</dd></div>
              <div><dt>Net gerçekleşmiş PnL (sanal)</dt><dd>{amount(item.realized_pnl_usdt)}</dd></div>
              <div><dt>Açılış / kapanış</dt><dd>{time(item.opened_at)} / {time(item.closed_at)}</dd></div>
            </dl>
          </article>)}
        </div>}
      {data.total_positions > data.positions.length &&
        <p className="paper-ledger-caption">Yalnızca son {data.positions.length} pozisyon listeleniyor.</p>}
      <h3>Sanal İşlem Günlüğü</h3>
      {data.events.length === 0 ? <p className="paper-ledger-caption">Henüz kayıtlı günlük olayı yok.</p> :
        <div className="paper-ledger-list" aria-label="Sanal işlem günlüğü">
          {data.events.map(event => <article className="paper-ledger-event" key={event.id}>
            <div><strong>{eventLabels[event.event_type] || "Sanal muhasebe olayı"}</strong>
              <small>{time(event.created_at)}</small></div>
            <div><span>Nakit değişimi: {amount(event.cash_delta_usdt)}</span>
              <span>Miktar değişimi: {event.quantity_delta}</span>
              <span>Son nakit: {amount(event.balance_after_usdt)}</span>
              <span>Komisyon: {amount(event.fee_usdt)}</span>
              <span>Realize net PnL: {amount(event.realized_pnl_delta_usdt)}</span></div>
            {event.observed_quote ? <small>
              Kayıtlı public gözlem: {event.observed_quote.symbol} · {event.observed_quote.reference_price} USDT · {time(new Date(event.observed_quote.observed_at_ms).toISOString())}. Varsayımsal ücret {event.observed_quote.fee_bps} bps, kayma {event.observed_quote.slippage_bps} bps, spread {event.observed_quote.spread_bps === null ? "bilinmiyor" : `${event.observed_quote.spread_bps} bps`}; gerçek fill değildir.
            </small> : <small>Bu olay için public fiyat veya maliyet kanıtı kaydedilmemiş; spread bilinmiyor.</small>}
          </article>)}
        </div>}
      {data.total_events > data.events.length &&
        <p className="paper-ledger-caption">Yalnızca son {data.events.length} olay listeleniyor.</p>}
    </> : null}
  </section>;
}
