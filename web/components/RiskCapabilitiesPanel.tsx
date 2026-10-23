"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type Profile = {
  available: boolean;
  selectable: boolean;
  reason: string;
  active_profile_version: number | null;
  active_risk_level: number | null;
  runtime_revision: number | null;
  bot_state: string;
  schema_version: string | null;
  limits: Record<string, string | number> | null;
};

const emptyForm = {
  risk_level: "",
  max_positions: "",
  risk_per_trade_pct: "",
  max_open_capital: "",
  spot_budget: "",
  daily_loss_pct: "",
  max_drawdown_pct: "",
  max_consecutive_losses: "",
};

export default function RiskCapabilitiesPanel({connected, apiAddress, token}: {
  connected: boolean; apiAddress: string; token: string;
}) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [form, setForm] = useState(emptyForm);
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState("Risk profili doğrulanamadı.");
  const [busy, setBusy] = useState(false);

  const endpoint = useCallback(() => {
    const origin = new URL(apiAddress);
    if (origin.protocol !== "http:" ||
        !["localhost", "127.0.0.1"].includes(origin.hostname) ||
        origin.username || origin.password) throw new Error("invalid_origin");
    return origin.origin + "/api/v1/risk/profiles";
  }, [apiAddress]);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!connected) {
      setProfile(null);
      setMessage("Yerel API bağlantısı yok; RiskGuard profili doğrulanmadı.");
      return;
    }
    try {
      const response = await fetch(endpoint(), {
        headers: {"X-Admin-Token": token}, cache: "no-store", signal,
      });
      if (!response.ok) throw new Error("unavailable");
      const value = await response.json() as Profile;
      if (typeof value.available !== "boolean" || typeof value.selectable !== "boolean" ||
          !["RUNNING", "PAUSED", "STOPPED", "EMERGENCY_FLATTEN"].includes(value.bot_state)) {
        throw new Error("invalid_contract");
      }
      if (signal?.aborted) return;
      setProfile(value);
      setMessage(value.available
        ? value.active_profile_version === null
          ? "İzole RiskGuard deposu hazır. Limitleri siz girip açık onay vermeden profil kurulmaz."
          : `Etkin profil: Seviye ${value.active_risk_level ?? "?"}, sürüm ${value.active_profile_version}, bot ${value.bot_state}.`
        : "İzole RiskGuard profili kullanılamıyor; seçim ve kayıt kapalı.");
    } catch {
      if (!signal?.aborted) {
        setProfile(null);
        setMessage("RiskGuard API doğrulanamadı; profil değişikliği kapalı.");
      }
    }
  }, [connected, endpoint, token]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!profile?.available || !profile.selectable || !confirmed) return;
    setBusy(true);
    setMessage("Profil onayı kaydediliyor...");
    try {
      const numeric = Object.fromEntries(Object.entries(form).map(([key, value]) => [
        key, key === "risk_level" || key === "max_positions" || key === "max_consecutive_losses"
          ? Number.parseInt(value, 10) : value,
      ]));
      const response = await fetch(endpoint(), {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-Token": token,
          "X-Risk-Profile-Approval": "I_CONFIRM_RISK_LIMITS",
        },
        cache: "no-store",
        body: JSON.stringify({
          ...numeric,
          expected_revision: profile.runtime_revision ?? 0,
          user_hard_limits: {},
          user_confirmed: true,
        }),
      });
      if (response.status === 409) throw new Error("revision");
      if (!response.ok) throw new Error("rejected");
      setConfirmed(false);
      setForm(emptyForm);
      await load();
      setMessage("Kullanıcı onaylı profil sürümlendi. Bu işlem botu başlatmaz ve emir yetkisi vermez.");
    } catch (error) {
      setMessage(error instanceof Error && error.message === "revision"
        ? "Profil sürümü değişti; güncel durumu yenileyip tekrar deneyin."
        : "Profil kaydedilmedi; limitleri ve RiskGuard bağlantısını doğrulayın.");
    } finally {
      setBusy(false);
    }
  }

  const enabled = Boolean(profile?.available && profile.selectable && confirmed && !busy);
  return <div className="risk-profile-panel">
    <p role="status" aria-live="polite">{message}</p>
    {profile?.limits && <dl className="risk-profile-limits">
      {Object.entries(profile.limits).filter(([key]) => key !== "user_hard_limits").map(([key, value]) =>
        <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{String(value)}</dd></div>)}</dl>}
    {profile?.available && <form onSubmit={submit}>
      <p>Yeni profil için bütün sayısal limitleri kullanıcı belirlemelidir. Seviye etiketi limit üretmez.</p>
      <label>Risk seviyesi<select required value={form.risk_level}
        onChange={event => setForm({...form, risk_level: event.target.value})} disabled={!profile.selectable}>
        <option value="">Seviye seç</option>
        <option value="1">1 · Çok düşük</option><option value="2">2 · Düşük</option>
        <option value="3">3 · Dengeli</option><option value="4">4 · Yüksek</option>
        <option value="5">5 · Agresif</option></select></label>
      <div className="risk-profile-fields">
        {([
          ["max_positions", "Pozisyon üst sınırı", "adet", "1"],
          ["risk_per_trade_pct", "İşlem başına risk", "%", "0.01"],
          ["max_open_capital", "Toplam açık sermaye", "USDT", "0.01"],
          ["spot_budget", "Spot bütçe", "USDT", "0.01"],
          ["daily_loss_pct", "Günlük zarar sınırı", "%", "0.01"],
          ["max_drawdown_pct", "Toplam drawdown sınırı", "%", "0.01"],
          ["max_consecutive_losses", "Ardışık kayıp sınırı", "adet", "1"],
        ] as const).map(([key, label, unit, step]) => <label key={key}>{label}
          <span className="risk-input"><input required type="number" min={step} step={step}
            value={form[key]} disabled={!profile.selectable}
            onChange={event => setForm({...form, [key]: event.target.value})}/><small>{unit}</small></span>
        </label>)}
      </div>
      <label className="risk-confirm"><input type="checkbox" checked={confirmed}
        disabled={!profile.selectable} onChange={event => setConfirmed(event.target.checked)}/>
        Girilen limitleri anladım ve bu RiskGuard profilini kurmayı onaylıyorum.</label>
      <button type="submit" disabled={!enabled}>Onaylı profili kaydet</button>
      <small>Kaydetmek botu başlatmaz. RiskGuard her yeni risk talebini ayrıca değerlendirir.</small>
    </form>}
  </div>;
}
