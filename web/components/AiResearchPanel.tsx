"use client";

import { useEffect, useState } from "react";

type Row = {
  signal_id: string; model_version: string; artifact_sha256: string;
  feature_schema_version: string; feature_sha256: string; symbol: string;
  interval: string; action: string; reason_code: string;
  observed_close_at_ms: number; received_at_ms: number; outcome_due_at_ms: number;
  probabilities: {win: string; loss: string; flat: string} | null;
  outcome: string | null; evaluated_at_ms: number | null;
  outcome_source_sha256: string | null; approval_status: string;
};
type ResponseBody = {
  available: boolean; source: string; status: string; server_time: string;
  predictions: Row[]; metrics_available: boolean; metrics_reason: string;
};

export default function AiResearchPanel({connected, apiAddress, token}: {
  connected: boolean; apiAddress: string; token: string;
}) {
  const [data, setData] = useState<ResponseBody | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!connected) { setData(null); setError("Yerel API bağlantısı gerekli."); return; }
    const controller = new AbortController();
    async function load() {
      try {
        const origin = new URL(apiAddress);
        if (origin.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(origin.hostname) ||
            origin.username || origin.password) throw new Error("origin");
        const response = await fetch(origin.origin + "/api/v1/ai/research/predictions?limit=50", {
          headers: {"X-Admin-Token": token}, cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error("unavailable");
        const body = await response.json() as ResponseBody;
        if (body.source !== "isolated_ai_research_store" || !Array.isArray(body.predictions)) {
          throw new Error("contract");
        }
        if (!controller.signal.aborted) { setData(body); setError(""); }
      } catch {
        if (!controller.signal.aborted) { setData(null); setError("AI araştırma deposu erişilemiyor; kayıtlar gösterilmedi."); }
      }
    }
    setData(null); setError("İzole AI araştırma kayıtları yükleniyor...");
    void load();
    return () => controller.abort();
  }, [connected, apiAddress, token]);

  return <div className="ai-research-panel">
    <p className="ai-research-badge">RESEARCH ONLY — NOT APPROVED</p>
    <p role="status" aria-live="polite">{error || (data?.available ? `Kaynak: ${data.source} · Sunucu zamanı: ${new Date(data.server_time).toLocaleString("tr-TR")}` : "Araştırma verisi kullanılamıyor.")}</p>
    <p>Bu tahminler işlem sinyali veya garantisi değildir. Model onay durumu değiştirilemez; olasılıklar kalibrasyon kanıtı sayılmaz.</p>
    {data?.metrics_available === false && <p>Kalibrasyon/Brier/log loss kayıtlı ölçümü mevcut değil: {data.metrics_reason}</p>}
    {!data?.predictions.length && <div className="empty"><strong>Prospektif araştırma tahmini yok</strong><span>Veri yoksa tahmin üretilmez.</span></div>}
    {Boolean(data?.predictions.length) && <div className="ai-research-list">{data?.predictions.map(row => <article key={row.signal_id}>
      <header><strong>{row.symbol} · {row.interval} · {row.action}</strong><span>{row.approval_status}</span></header>
      <p>Model {row.model_version} · özellik şeması {row.feature_schema_version} · {row.reason_code}</p>
      <p>Gözlem: {new Date(row.observed_close_at_ms).toLocaleString("tr-TR")} · Kayıt: {new Date(row.received_at_ms).toLocaleString("tr-TR")} · Sonuç zamanı: {new Date(row.outcome_due_at_ms).toLocaleString("tr-TR")}</p>
      <p>Olasılıklar: {row.probabilities ? `Alış ${row.probabilities.win} / Satış ${row.probabilities.loss} / Yatay ${row.probabilities.flat} (doğrulanmış kalibrasyon değil)` : "kaydedilmemiş"}</p>
      <p>Prospektif sonuç: {row.outcome ?? "beklemede"}{row.evaluated_at_ms ? ` · ${new Date(row.evaluated_at_ms).toLocaleString("tr-TR")}` : ""}</p>
      <small>Artifact SHA-256: {row.artifact_sha256} · Feature SHA-256: {row.feature_sha256}</small>
    </article>)}</div>}
  </div>;
}
