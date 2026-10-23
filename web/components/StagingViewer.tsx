"use client";

import { useCallback, useState } from "react";
import StagingConsoleSections, {type ConsoleSection} from "./StagingConsoleSections";
import { Activity, LockKeyhole, LogOut, RefreshCw, ShieldCheck, Menu, BarChart3, Bot, Wallet, History, Layers3, ShieldAlert } from "lucide-react";

const consoleMenu = [
  {id:"dashboard",label:"Genel Bakış",Icon:BarChart3},
  {id:"market",label:"Piyasa Verileri",Icon:Activity},
  {id:"bot",label:"Bot Yönetimi",Icon:Bot},
  {id:"risk",label:"Risk Yönetimi",Icon:ShieldCheck},
  {id:"strategies",label:"AI Araştırma",Icon:Layers3},
  {id:"positions",label:"Pozisyonlar",Icon:Wallet},
  {id:"logs",label:"Olay Kayıtları",Icon:History},
] as const;

type Summary = {
  mode: "paper"; source: "persisted_paper_spot";
  initialized: boolean;
  account: null | {
    initial_capital_usdt: string; cash_usdt: string;
    realized_pnl_usdt: string; paid_fees_usdt: string;
  };
  total_positions: number; open_positions: number; total_events: number;
  positions: Array<{
    symbol: string; status: "open" | "closed";
    quantity: string; entry_fill_price: string; stop_price: string;
    opened_at: string;
  }>;
  events: Array<{
    id: number; event_type: "account_opened" | "spot_buy" | "spot_sell";
    created_at: string; cash_delta_usdt: string;
    realized_pnl_delta_usdt: string; fee_usdt: string;
    quote: null | {
      source: string; symbol: string; interval: string;
      observed_at_ms: number; reference_price: string;
      fee_bps: number; slippage_bps: number;
    };
  }>;
  worker: {
    requested_enabled: boolean | null;
    process_verified_running: false;
    last_status: string | null;
    last_attempt_at_ms: number | null;
    next_allowed_at_ms: number | null;
  };
};
type LoggedIn = { role: string; csrf: string };
type PendingManual = {
  request_id: string; action: "buy" | "sell"; symbol: "BTCUSDT" | "ETHUSDT";
  budget_usdt: string | null; stop_distance_pct: string | null;
};
function urlFromEnvironment(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" || !url.hostname.includes(".") ||
        url.username || url.password || url.search || url.hash ||
        url.pathname !== "/") return null;
    return url.origin;
  } catch {
    return null;
  }
}

function verifiedPaperSnapshot(view: Summary): Summary {
  if (view.mode !== "paper" || view.source !== "persisted_paper_spot" ||
      !view.worker || view.worker.process_verified_running !== false ||
      (view.initialized && !view.account) ||
      !Array.isArray(view.positions) || !Array.isArray(view.events) ||
      view.positions.length > 10 || view.events.length > 10 ||
      !Number.isSafeInteger(view.total_positions) || view.total_positions < 0 ||
      !Number.isSafeInteger(view.total_events) || view.total_events < 0 ||
      !Number.isSafeInteger(view.open_positions) || view.open_positions < 0 ||
      view.open_positions > view.total_positions ||
      view.positions.some(p => !["open", "closed"].includes(p.status)) ||
      view.events.some(e => !["account_opened", "spot_buy", "spot_sell"].includes(e.event_type))) {
    throw new Error("Güvenilir PAPER özeti alınamadı.");
  }
  return view;
}

// Only format trusted PAPER Decimal strings at the UI boundary.
// BigInt avoids floating-point rounding and never changes stored precision.
function formatPaperUsdt(raw: string): string {
  const parts = /^(-?)(\d+)(?:\.(\d+))?$/.exec(raw);
  if (!parts) return "—";
  const negative = parts[1] === "-";
  const integer = parts[2];
  const fraction = (parts[3] ?? "").padEnd(3, "0");
  const hundred = BigInt(100);
  const cents = BigInt(integer) * hundred +
    BigInt(fraction.slice(0, 2)) + (fraction[2] >= "5" ? BigInt(1) : BigInt(0));
  const whole = (cents / hundred).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const decimals = (cents % hundred).toString().padStart(2, "0");
  return (negative && cents !== BigInt(0) ? "-" : "") + whole + "," + decimals;
}

// Keep quantity exact as supplied by the persisted Decimal API.
function paperQuantity(raw: string): string {
  return /^\d+(?:\.\d+)?$/.test(raw)
    ? raw.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "")
    : "—";
}

function recordedAt(iso: string): string {
  const timestamp = Date.parse(iso);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString("tr-TR", {timeZone:"UTC"}) + " UTC"
    : "—";
}

function eventTitle(type: Summary["events"][number]["event_type"]): string {
  return type === "account_opened" ? "Sanal hesap açıldı"
    : type === "spot_buy" ? "Sanal Spot alış" : "Sanal Spot satış";
}

function timeUTC(value: number | null): string {
  return value === null ? "—" :
    new Date(value).toLocaleString("tr-TR", {timeZone:"UTC"}) + " UTC";
}

export default function StagingViewer({apiOrigin}: {apiOrigin?: string}) {
  const base = urlFromEnvironment(apiOrigin);
  const [role, setRole] = useState<"viewer"|"operator">("viewer");
  const [password, setPassword] = useState("");
  const [session, setSession] = useState<LoggedIn|null>(null);
  const [snapshot, setSnapshot] = useState<Summary|null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [manualSymbol, setManualSymbol] = useState<"BTCUSDT"|"ETHUSDT">("BTCUSDT");
  const [manualBudget, setManualBudget] = useState("50");
  const [manualStop, setManualStop] = useState("2");
  const [pendingManual, setPendingManual] = useState<PendingManual|null>(null);
  const [manualConfirmed, setManualConfirmed] = useState(false);
  const [page, setPage] = useState<ConsoleSection>("dashboard");
  const [navOpen, setNavOpen] = useState(false);
  const onSessionExpired = useCallback(() => {
    setSession(null); setSnapshot(null); setPendingManual(null);
    setManualConfirmed(false); setNotice("Oturum süresi doldu; yeniden giriş yap.");
  }, []);
  function navigate(target: ConsoleSection) {
    setPage(target); setNavOpen(false); setPendingManual(null);
    setManualConfirmed(false); setNotice("");
  }

  async function refresh() {
    if (!base) return;
    setBusy(true);
    setNotice("");
    try {
      const response = await fetch(base + "/api/v1/paper/dashboard", {
        credentials: "include", cache: "no-store",
      });
      if (!response.ok) throw new Error("Yetkili PAPER verisi okunamadı ("+response.status+").");
      const view = verifiedPaperSnapshot(await response.json() as Summary);
      setSnapshot(view);
    } catch (error) {
      setSnapshot(null);
      setNotice(error instanceof Error ? error.message : "Bağlantı hatası.");
      if (error instanceof Error && error.message.includes("401")) setSession(null);
    } finally {
      setBusy(false);
    }
  }
  async function signIn(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!base || !password.trim()) return;
    setBusy(true);
    setNotice("");
    try {
      const response = await fetch(base + "/api/v1/auth/login", {
        method:"POST", credentials:"include", cache:"no-store",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({username:role,password}),
      });
      if (!response.ok) throw new Error(
        response.status === 429 ? "Çok sayıda deneme. Giriş geçici olarak kısıtlandı." :
        "Giriş başarısız. Kullanıcı bilgilerini kontrol et."
      );
      const json: {role?:string;csrf_token?:string} = await response.json();
      if (json.role !== role || typeof json.csrf_token !== "string" ||
          !/^[a-f0-9]{64}$/i.test(json.csrf_token))
        throw new Error("Geçersiz oturum yanıtı.");
      const dashboard = await fetch(base + "/api/v1/paper/dashboard", {
        credentials:"include",cache:"no-store",
      });
      if (!dashboard.ok) throw new Error("Oturum açıldı ancak sanal kayıtlar okunamadı.");
      const view = verifiedPaperSnapshot(await dashboard.json() as Summary);
      // Commit both UI states only after server login AND PAPER snapshot validation.
      setSnapshot(view);
      setSession({role:json.role,csrf:json.csrf_token});
      setPassword("");
    } catch (error) {
      setSnapshot(null);
      setSession(null);
      setNotice(error instanceof Error ? error.message : "Oturum açılamadı.");
    } finally {
      setBusy(false);
    }
  }
  async function signOut() {
    if (!base || !session) return;
    setBusy(true);
    try {
      const response = await fetch(base + "/api/v1/auth/logout", {
        method:"POST",credentials:"include",cache:"no-store",
        headers:{"Content-Type":"application/json","X-CSRF-Token":session.csrf},
        body:"{}",
      });
      if (response.status === 401) {
        // Older staging API versions may return 401 for a session that has
        // already expired. This is not a live session; clear local UI state.
        setSession(null);
        setSnapshot(null);
        setNotice("Oturum süresi dolmuş. Yeniden giriş yapabilirsin.");
        return;
      }
      if (!response.ok) {
        setNotice(response.status === 403
          ? "Çıkış güvenlik doğrulaması tarafından reddedildi (HTTP 403). Oturum henüz doğrulanarak kapatılmadı."
          : "Oturum kapatma doğrulanamadı (HTTP " + response.status + "). Tekrar deneyebilirsin.");
        return;
      }
      setSession(null);
      setSnapshot(null);
      setPendingManual(null);
      setManualConfirmed(false);
      setNotice("Oturum güvenli biçimde kapatıldı.");
    } catch {
      setNotice("Oturum kapatma isteği iletilemedi.");
    } finally { setBusy(false); }
  }

  function prepareManual(action: "buy" | "sell", activeSymbol?: string) {
    if (busy || pendingManual || !snapshot?.initialized || session?.role !== "operator" ||
        snapshot.worker.requested_enabled !== false) return;
    const symbol = action === "sell" ? activeSymbol : manualSymbol;
    if (symbol !== "BTCUSDT" && symbol !== "ETHUSDT") return;
    if (action === "buy" && (!["25","50","100"].includes(manualBudget) ||
                            !["1","2"].includes(manualStop))) return;
    setPendingManual({
      action, symbol, request_id: crypto.randomUUID(),
      budget_usdt: action === "buy" ? manualBudget : null,
      stop_distance_pct: action === "buy" ? manualStop : null,
    });
    setManualConfirmed(false);
    setNotice("");
  }

  async function commitManual() {
    if (!base || !pendingManual || !manualConfirmed || !session ||
        session.role !== "operator" || busy) return;
    setBusy(true);
    setNotice("");
    let completed = false;
    try {
      const response = await fetch(base + "/api/v1/paper/manual", {
        method: "POST", credentials: "include", cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": session.csrf,
          "X-Paper-Settings-Approval": "PAPER_SETTINGS_ONLY",
        },
        body: JSON.stringify({
          ...pendingManual, confirmation: "CONFIRM_MANUAL_PAPER_ONLY",
        }),
      });
      if (!response.ok) {
        throw new Error(response.status === 429
          ? "Kamusal fiyat kaynağı hız sınırında. Yeni istek göndermeden önce bekle."
          : response.status === 401 ? "Operatör oturumu sona erdi; yeniden giriş yap."
          : response.status === 403 ? "Operatör yetkisi veya CSRF onayı reddedildi."
          : response.status === 409 ? "PAPER risk/pozisyon koruması isteği reddetti. Kaydı yenile."
          : "Manuel PAPER isteği doğrulanamadı (HTTP " + response.status + ").");
      }
      const receipt: {mode?:string;result?:string;exchange_order_sent?:boolean} =
        await response.json();
      if (receipt.mode !== "paper" || receipt.result !== "paper_recorded" ||
          receipt.exchange_order_sent !== false) throw new Error("Güvenilir PAPER işlem yanıtı yok.");
      completed = true;
      setPendingManual(null);
      setManualConfirmed(false);
    } catch (err) {
      setNotice((err instanceof Error ? err.message : "Bağlantı hatası.") +
        " Yanıt belirsizse aynı istekle yeniden dene veya önce işlem geçmişini kontrol et.");
    } finally {
      setBusy(false);
    }
    if (completed) {
      await refresh();
      setNotice("Manuel PAPER simülasyonu kaydedildi; kayıtlı bakiye ve olaylar yenilendi.");
    }
  }

  return <main className={"staging-shell" + (session ? " staging-shell-auth" : "")}>
    <div className={"staging-panel" + (session ? " staging-panel-auth" : "")}>
      <header className="staging-identity"><span><Activity size={22}/></span>
        <div><h1>TradePilot</h1><p>Güvenli PAPER test önizlemesi</p></div>
      </header>
      <div className="staging-warning"><ShieldCheck size={20}/>
        Bu yalnız PAPER test ekranıdır. Gerçek Binance emirleri, API anahtarı,
        otomatik çalışan bot doğrulaması ve canlı portföy değeri içermez.</div>
      {!base ? <div role="alert" className="staging-error">
        Önizleme API adresi güvenli HTTPS olarak yapılandırılmamış. Giriş kapalı.
      </div> : !session ? <form onSubmit={event=>void signIn(event)} className="staging-form">
        <h2>Güvenli oturum aç</h2>
        <label>Kullanıcı rolü
          <select value={role} disabled={busy} onChange={e=>setRole(e.target.value as "viewer"|"operator")}>
            <option value="viewer">Görüntüleyici</option>
            <option value="operator">Operatör (manuel PAPER onaylı)</option>
          </select>
        </label>
        <label>Parola
          <input type="password" autoComplete="current-password" value={password}
            onChange={e=>setPassword(e.target.value)} disabled={busy} minLength={1} maxLength={256} required/>
        </label>
        <button type="submit" disabled={busy}><LockKeyhole size={16}/>
          {busy?"Kontrol ediliyor...":"Giriş yap"}</button>
      </form> : <div className="staging-workspace">
      <button className="staging-menu-toggle" aria-expanded={navOpen} aria-controls="staging-console-nav"
        onClick={()=>setNavOpen(value=>!value)}><Menu size={18}/> Yönetim menüsü</button>
      <aside className={"staging-console-nav" + (navOpen ? " staging-nav-open" : "")} id="staging-console-nav">
        <div className="staging-console-caption">PAPER YÖNETİM</div>
        <nav aria-label="TradePilot bölümleri">{consoleMenu.map(item=><button
          type="button" key={item.id} aria-current={page===item.id ? "page" : undefined}
          className={page===item.id ? "selected" : ""}
          onClick={()=>navigate(item.id)}><item.Icon size={17}/><span>{item.label}</span></button>)}</nav>
        <p className="staging-console-foot"><ShieldAlert size={14}/> Yalnız sanal işlemler · Canlı emirler kapalı</p>
      </aside>
      <div className="staging-details">
        <div className="staging-actions">
          <strong>Oturum açık · {session.role === "viewer"?"Görüntüleyici":"Operatör"}</strong>
          <button onClick={()=>void refresh()} disabled={busy}><RefreshCw size={16}/> Yenile</button>
          <button onClick={()=>void signOut()} disabled={busy}><LogOut size={16}/> Çıkış</button>
        </div>
        {(page === "market" || page === "bot" || page === "risk" || page === "strategies") && base &&
          <StagingConsoleSections section={page} base={base} onExpired={onSessionExpired}/>}
        {page !== "market" && page !== "bot" && page !== "risk" && page !== "strategies" &&
          (!snapshot ? <p>Kayıtlı PAPER özeti henüz yüklenmedi.</p> : <>
          {page === "logs" && base && <StagingConsoleSections section="logs" base={base}
            onExpired={onSessionExpired}/>}

          {page === "dashboard" && (snapshot.initialized && snapshot.account ? <div className="staging-metrics">
            <article><span>Başlangıç (sanal)</span><strong>{formatPaperUsdt(snapshot.account.initial_capital_usdt)} USDT</strong></article>
            <article><span>Kaydedilmiş nakit (sanal)</span><strong>{formatPaperUsdt(snapshot.account.cash_usdt)} USDT</strong></article>
            <article><span>Gerçekleşmiş PnL (sanal)</span><strong>{formatPaperUsdt(snapshot.account.realized_pnl_usdt)} USDT</strong></article>
            <article><span>Varsayımsal komisyon</span><strong>{formatPaperUsdt(snapshot.account.paid_fees_usdt)} USDT</strong></article>
          </div> : <p>Henüz PAPER hesap verisi bulunmuyor; tutar uydurulmaz.</p>)}
          {(page === "dashboard" || page === "positions") && session.role === "operator" && snapshot.initialized && snapshot.account &&
            <section className="staging-manual" aria-labelledby="staging-manual-title">
              <h2 id="staging-manual-title">Manuel PAPER Spot simülasyonu</h2>
              <p>Gerçek Binance emri gönderilmez. Sunucu işlem sırasında yeni kamusal fiyat doğrular;
                ekrandaki fiyat canlı emir fiyatı veya garanti edilen fill değildir.
                Otomatik bot kapalı kalır.</p>
              {snapshot.worker.requested_enabled !== false
                ? <p role="alert">Bot ayarı kapalı ve manuel onay zorunlu değilse simülasyon engellenir.</p>
                : !pendingManual ? <div className="staging-manual-form">
                  {!snapshot.open_positions && <>
                    <label>Sanal piyasa
                      <select value={manualSymbol} disabled={busy}
                        onChange={e=>setManualSymbol(e.target.value as "BTCUSDT"|"ETHUSDT")}>
                        <option value="BTCUSDT">BTC/USDT</option>
                        <option value="ETHUSDT">ETH/USDT</option>
                      </select>
                    </label>
                    <label>En fazla harcanacak sanal USDT
                      <select value={manualBudget} disabled={busy} onChange={e=>setManualBudget(e.target.value)}>
                        <option value="25">25 USDT</option><option value="50">50 USDT</option>
                        <option value="100">100 USDT</option>
                      </select>
                    </label>
                    <label>Varsayımsal stop uzaklığı
                      <select value={manualStop} disabled={busy} onChange={e=>setManualStop(e.target.value)}>
                        <option value="1">%1</option><option value="2">%2</option>
                      </select>
                    </label>
                    <button type="button" disabled={busy}
                      onClick={()=>prepareManual("buy")}>Sanal alış isteğini gözden geçir</button>
                  </>}
                  {snapshot.positions.filter(p=>p.status==="open").map(p=>
                    <button key={p.symbol} type="button" disabled={busy}
                      onClick={()=>prepareManual("sell",p.symbol)}>
                      {p.symbol} açık PAPER pozisyonunu kapatmayı gözden geçir
                    </button>
                  )}
                </div> : <div className="staging-manual-review">
                  <strong>{pendingManual.action === "buy" ? "Sanal alış onayı" : "Sanal pozisyon kapatma onayı"}</strong>
                  <p>{pendingManual.symbol} · {pendingManual.action === "buy"
                    ? "En çok " + pendingManual.budget_usdt + " USDT · Stop %" + pendingManual.stop_distance_pct
                    : "Mevcut açık pozisyonun tamamı"}</p>
                  <p>Kamusal fiyat yeniden doğrulanacak. Simülasyon sonrası varsayımsal komisyon,
                    slippage ve piyasa değişikliği nakit sonucunu etkileyebilir.</p>
                  <label className="staging-confirm-check">
                    <input type="checkbox" checked={manualConfirmed} disabled={busy}
                      onChange={e=>setManualConfirmed(e.target.checked)}/>
                    Bunun yalnızca sanal bir PAPER işlemi olduğunu anladım ve açıkça onaylıyorum.
                  </label>
                  <div className="staging-manual-buttons">
                    <button type="button" disabled={busy || !manualConfirmed}
                      onClick={()=>void commitManual()}>
                      {busy ? "Doğrulanıyor..." : "Manuel PAPER işlemini onayla"}
                    </button>
                    <button type="button" disabled={busy}
                      onClick={()=>{setPendingManual(null);setManualConfirmed(false);}}>
                      İsteği iptal et
                    </button>
                  </div>
                  <p className="staging-muted">Bağlantı koparsa aynı istek kimliğiyle yeniden dene;
                    yeni istek oluşturmadan önce işlem geçmişini kontrol et.</p>
                </div>}
            </section>}
          {(page === "dashboard" || page === "positions") && <section className="staging-history" aria-labelledby="staging-positions-title">
            <div className="staging-section-heading">
              <h2 id="staging-positions-title">PAPER pozisyonları</h2>
              <span>Toplam {snapshot.total_positions} · Açık {snapshot.open_positions}</span>
            </div>
            {snapshot.positions.length === 0
              ? <p className="staging-empty">Henüz sanal pozisyon kaydı yok. Otomatik işlem başlatılmadı.</p>
              : <div className="staging-records">
                {snapshot.positions.map((p, index) => <article key={p.symbol+"-"+p.opened_at+"-"+index} className="staging-record">
                  <div className="staging-record-heading">
                    <strong>{p.symbol}</strong>
                    <span>{p.status === "open" ? "Açık" : "Kapalı"} · PAPER Spot</span>
                  </div>
                  <p>Miktar: {paperQuantity(p.quantity)} · Kaydedilmiş giriş: {formatPaperUsdt(p.entry_fill_price)} USDT</p>
                  <p>Kaydedilmiş stop seviyesi: {formatPaperUsdt(p.stop_price)} USDT</p>
                  <time dateTime={p.opened_at}>{recordedAt(p.opened_at)}</time>
                </article>)}
              </div>}
            {snapshot.total_positions > snapshot.positions.length &&
              <p className="staging-muted">Yalnızca son {snapshot.positions.length} pozisyon gösteriliyor.</p>}
          </section>}
          {(page === "dashboard" || page === "logs") && <section className="staging-history" aria-labelledby="staging-events-title">
            <div className="staging-section-heading">
              <h2 id="staging-events-title">Son PAPER olayları</h2>
              <span>Toplam {snapshot.total_events}</span>
            </div>
            {snapshot.events.length === 0
              ? <p className="staging-empty">Henüz kaydedilmiş sanal hesap olayı yok.</p>
              : <div className="staging-records">
                {snapshot.events.map(e => <article key={e.id} className="staging-record">
                  <div className="staging-record-heading">
                    <strong>{eventTitle(e.event_type)}</strong>
                    <time dateTime={e.created_at}>{recordedAt(e.created_at)}</time>
                  </div>
                  <p>Nakit değişimi: {formatPaperUsdt(e.cash_delta_usdt)} USDT · Gerçekleşmiş PnL: {formatPaperUsdt(e.realized_pnl_delta_usdt)} USDT</p>
                  <p>Varsayımsal komisyon: {formatPaperUsdt(e.fee_usdt)} USDT</p>
                  {e.quote && <p>Kaydedilmiş referans: {e.quote.symbol} · {formatPaperUsdt(e.quote.reference_price)} USDT · {e.quote.interval}</p>}
                </article>)}
              </div>}
            {snapshot.total_events > snapshot.events.length &&
              <p className="staging-muted">Yalnızca son {snapshot.events.length} olay gösteriliyor.</p>}
          </section>}
          {page === "dashboard" && <section className="staging-status" aria-labelledby="staging-worker-title">
            <h2 id="staging-worker-title">Bot — Son bilinen durum</h2>
            <p>{snapshot.worker.requested_enabled === true
              ? "Bot ayarı açık; çalışan servis doğrulanmadı."
              : "Otomatik çalışma kapalı; çalışan servis doğrulanmadı."}</p>
            <p>Son kayıtlı sonuç: {snapshot.worker.last_status ?? "Kayıt yok"}</p>
            <p>Son deneme: {timeUTC(snapshot.worker.last_attempt_at_ms)}</p>
            <p>Bir sonraki izinli deneme: {timeUTC(snapshot.worker.next_allowed_at_ms)}</p>
            <p className="staging-muted">Bu kayıtlar canlı bot bağlantısı veya çalışan işlem motoru kanıtı değildir.</p>
          </section>}
        </>)}
      </div></div>}
      {notice && <p role="status" className="staging-notice">{notice}</p>}
      <footer>HTTPS + Secure HttpOnly cookie · 15 dakikalık oturum ·
        Yerel yönetici anahtarı kullanılmaz · Otomatik trade başlatılmaz</footer>
    </div>
  </main>;
}
