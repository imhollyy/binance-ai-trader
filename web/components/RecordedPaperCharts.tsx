"use client";

type ClosedCandle = {
  open_time_ms: number; close_time_ms: number; open: string;
  high: string; low: string; close: string; volume: string;
};
type RealizedEvent = {
  id: number; event_type: string; created_at: string;
  realized_pnl_delta_usdt: string;
};
type SpotInterval = "1m" | "5m" | "15m";

const INTERVAL_MS: Record<SpotInterval, number> = {
  "1m": 60_000, "5m": 300_000, "15m": 900_000,
};
const DECIMAL = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;

function visualNumber(value: string): number | null {
  if (!DECIMAL.test(value)) return null;
  const converted = Number(value);
  return Number.isFinite(converted) ? converted : null;
}
function utc(ms: number): string {
  return new Date(ms).toLocaleString("tr-TR", {
    timeZone: "UTC", day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit",
  }) + " UTC";
}

/** Derived exclusively from persisted, consecutive and fully closed Spot candles. */
export function StoredCloseChart({candles, symbol, interval}: {
  candles: ClosedCandle[]; symbol: string; interval: SpotInterval;
}) {
  const step = INTERVAL_MS[interval];
  const rows = [...candles].sort((a, b) => a.open_time_ms - b.open_time_ms);
  const values = rows.map(item => visualNumber(item.close));
  const valid = rows.length >= 2 && rows.length <= 100 &&
    rows.every((c, i) => {
      const close = values[i];
      const high = visualNumber(c.high);
      const low = visualNumber(c.low);
      return Number.isSafeInteger(c.open_time_ms) && Number.isSafeInteger(c.close_time_ms) &&
        c.close_time_ms - c.open_time_ms === step - 1 &&
        c.open_time_ms % step === 0 &&
        (i === 0 || rows[i - 1].close_time_ms + 1 === c.open_time_ms) &&
        close !== null && close > 0 && high !== null && low !== null &&
        low > 0 && low <= close && close <= high;
    });
  if (!valid) {
    return <div className="recorded-chart-empty" data-testid="spot-chart-empty">
      En az iki ardışık, geçerli kapanmış mum olmadan grafik çizilmez. Eksik aralıklar birleştirilmez.
    </div>;
  }

  const closes = values as number[];
  const floor = Math.min(...closes);
  const ceiling = Math.max(...closes);
  const range = ceiling - floor;
  const points = closes.map((price, i) => {
    const x = 28 + i * 544 / (closes.length - 1);
    const y = range === 0 ? 112 : 191 - (price - floor) / range * 158;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return <figure className="recorded-chart" data-testid="spot-close-chart">
    <figcaption><strong>{symbol} · {interval} · Kayıtlı kapanış grafiği</strong>
      <span>{closes.length} kapanmış mum · geçmiş veri, canlı fiyat değil</span></figcaption>
    <svg className="recorded-chart-svg" viewBox="0 0 600 226" role="img"
      aria-label={`${symbol} için ${closes.length} kapanmış mumun geçmiş kapanış fiyatı çizgisi`}>
      <title>{symbol} — kayıtlı Spot mum kapanışları</title>
      <desc>Ardışık kayıtların karşılaştırmalı grafik görünümü. Tam fiyat değerleri aşağıda metin olarak verilir.</desc>
      {[33, 112, 191].map(y => <line key={y} x1="28" x2="572" y1={y} y2={y}
        stroke="currentColor" opacity="0.12" strokeDasharray="4 7"/>)}
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="3"
        strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx="28" cy={points.split(" ")[0].split(",")[1]} r="4.5" fill="currentColor"/>
      <circle cx="572" cy={points.split(" ").at(-1)?.split(",")[1]} r="4.5" fill="currentColor"/>
    </svg>
    <div className="recorded-chart-ends">
      <div><span>İlk kapanış · {utc(rows[0].close_time_ms)}</span>
        <strong>{rows[0].close} USDT</strong></div>
      <div><span>Son kapanış · {utc(rows[rows.length-1].close_time_ms)}</span>
        <strong>{rows[rows.length-1].close} USDT</strong></div>
    </div>
    <small>Çizgi yaklaşık görselleştirmedir; sayısal kayıtlar aynen korunur.
      Süreklilik denetlenir, eksik mum varsa grafik gösterilmez.</small>
  </figure>;
}

/** Signed individual realized SELL events, never a fabricated total-equity curve. */
export function RealizedTradeChart({events}: {events: RealizedEvent[]}) {
  const sells = [...events].filter(event => event.event_type === "spot_sell")
    .sort((a, b) => a.id - b.id);
  const parsed = sells.map(event => visualNumber(event.realized_pnl_delta_usdt));
  if (sells.length === 0) {
    return <div className="recorded-chart-empty" data-testid="realized-chart-empty">
      Henüz gerçekleşmiş sanal satış yok. Performans grafiği oluşturulmadı.
    </div>;
  }
  if (parsed.some(value => value === null) || new Set(sells.map(e => e.id)).size !== sells.length) {
    return <div className="recorded-chart-empty" data-testid="realized-chart-invalid">
      İşlem tutarları doğrulanamadığı için grafik gösterilemiyor.
    </div>;
  }
  const numbers = parsed as number[];
  const scale = Math.max(...numbers.map(v => Math.abs(v)));
  return <figure className="recorded-chart recorded-realized" data-testid="realized-trade-chart">
    <figcaption><strong>Kaydedilmiş sanal satış sonuçları</strong>
      <span>Yalnızca gösterilen son {sells.length} satış · kümülatif PnL değil</span></figcaption>
    <div className="realized-chart-rows" role="list" aria-label="Gerçekleşmiş sanal satış sonuçları">
      {sells.map((event, i) => {
        const value = numbers[i];
        const percent = scale === 0 ? 0 : Math.abs(value) / scale * 100;
        return <div key={event.id} role="listitem" className="realized-chart-row">
          <span className="realized-chart-label">
            {new Date(event.created_at).toLocaleString("tr-TR", {timeZone: "UTC"})} UTC
          </span>
          <div className="realized-chart-axis" aria-hidden="true">
            <span className="realized-bar-negative"
              style={{width: value < 0 ? `${percent/2}%` : "0%"}}/>
            <span className="realized-chart-middle"/>
            <span className="realized-bar-positive"
              style={{width: value > 0 ? `${percent/2}%` : "0%"}}/>
          </div>
          <strong className="realized-chart-value">{event.realized_pnl_delta_usdt} USDT</strong>
        </div>;
      })}
    </div>
    <small>Sanal muhasebedeki bireysel gerçekleşmiş PnL kayıtlarıdır.
      Ölçek görseldir; açık pozisyon ve varsayımsal portföy özsermayesi dahil değildir.</small>
  </figure>;
}
