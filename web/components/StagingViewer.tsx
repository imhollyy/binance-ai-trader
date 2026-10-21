"use client";

import { useState } from "react";
import { Activity, LockKeyhole, LogOut, RefreshCw, ShieldCheck } from "lucide-react";

type Summary = {
  mode: "paper"; source: "persisted_paper_spot";
  initialized: boolean;
  account: null | {
    initial_capital_usdt: string; cash_usdt: string;
    realized_pnl_usdt: string; paid_fees_usdt: string;
  };
  total_positions: number; open_positions: number; total_events: number;
  worker: {
    requested_enabled: boolean | null;
    process_verified_running: false;
    last_status: string | null;
    last_attempt_at_ms: number | null;
    next_allowed_at_ms: number | null;
  };
};
type LoggedIn = { role: string; csrf: string };
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
      (view.initialized && !view.account)) {
    throw new Error("Güvenilir PAPER özeti alınamadı.");
  }
  return view;
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
        method:"POST",credentials:"include",
        headers:{"X-CSRF-Token":session.csrf},
      });
      if (!response.ok) {
        setNotice("Oturum kapatma doğrulanamadı. Sayfayı kapat ve yöneticine haber ver.");
        return;
      }
      setSession(null);
      setSnapshot(null);
      setNotice("Oturum güvenli biçimde kapatıldı.");
    } catch {
      setNotice("Oturum kapatma isteği iletilemedi.");
    } finally { setBusy(false); }
  }

  return <main className="staging-shell">
    <div className="staging-panel">
      <header className="staging-identity"><span><Activity size={22}/></span>
        <div><h1>TradePilot</h1><p>Güvenli PAPER test önizlemesi</p></div>
      </header>
      <div className="staging-warning"><ShieldCheck size={20}/>
        Bu yalnız sanal hesap görüntüleme ekranıdır. Gerçek Binance emirleri, API anahtarı,
        otomatik çalışan bot doğrulaması ve canlı portföy değeri içermez.</div>
      {!base ? <div role="alert" className="staging-error">
        Önizleme API adresi güvenli HTTPS olarak yapılandırılmamış. Giriş kapalı.
      </div> : !session ? <form onSubmit={event=>void signIn(event)} className="staging-form">
        <h2>Güvenli oturum aç</h2>
        <label>Kullanıcı rolü
          <select value={role} disabled={busy} onChange={e=>setRole(e.target.value as "viewer"|"operator")}>
            <option value="viewer">Görüntüleyici</option>
            <option value="operator">Operatör (görüntüleme)</option>
          </select>
        </label>
        <label>Parola
          <input type="password" autoComplete="current-password" value={password}
            onChange={e=>setPassword(e.target.value)} disabled={busy} minLength={1} maxLength={256} required/>
        </label>
        <button type="submit" disabled={busy}><LockKeyhole size={16}/>
          {busy?"Kontrol ediliyor...":"Giriş yap"}</button>
      </form> : <div className="staging-details">
        <div className="staging-actions">
          <strong>Oturum açık · {session.role === "viewer"?"Görüntüleyici":"Operatör"}</strong>
          <button onClick={()=>void refresh()} disabled={busy}><RefreshCw size={16}/> Yenile</button>
          <button onClick={()=>void signOut()} disabled={busy}><LogOut size={16}/> Çıkış</button>
        </div>
        {!snapshot ? <p>Kayıtlı PAPER özeti henüz yüklenmedi.</p> : <>
          {snapshot.initialized && snapshot.account ? <div className="staging-metrics">
            <article><span>Başlangıç (sanal)</span><strong>{snapshot.account.initial_capital_usdt} USDT</strong></article>
            <article><span>Kaydedilmiş nakit (sanal)</span><strong>{snapshot.account.cash_usdt} USDT</strong></article>
            <article><span>Gerçekleşmiş PnL (sanal)</span><strong>{snapshot.account.realized_pnl_usdt} USDT</strong></article>
            <article><span>Varsayımsal komisyon</span><strong>{snapshot.account.paid_fees_usdt} USDT</strong></article>
          </div> : <p>Henüz PAPER hesap verisi bulunmuyor; tutar uydurulmaz.</p>}
          <div className="staging-status">
            <strong>Worker — Son bilinen durum</strong>
            <p>Çalışan servis doğrulanmadı. Sonuç: {snapshot.worker.last_status ?? "Kayıt yok"}</p>
            <p>Son deneme: {timeUTC(snapshot.worker.last_attempt_at_ms)}</p>
            <p>Bir sonraki izinli deneme: {timeUTC(snapshot.worker.next_allowed_at_ms)}</p>
            <p>Açık sanal pozisyon: {snapshot.open_positions} · Toplam kayıt: {snapshot.total_events}</p>
          </div>
        </>}
      </div>}
      {notice && <p role="status" className="staging-notice">{notice}</p>}
      <footer>HTTPS + Secure HttpOnly cookie · 15 dakikalık oturum ·
        Yerel yönetici anahtarı kullanılmaz · Otomatik trade başlatılmaz</footer>
    </div>
  </main>;
}
