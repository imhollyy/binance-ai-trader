"use client";

import { useCallback, useEffect, useState } from "react";

type Status = {
  state: string; revision: number; worker_id: string | null;
  heartbeat_at_ms: number | null; lease_until_ms: number | null;
  verified_running: boolean; stale: boolean; start_supported: boolean;
  generation: number; manager_id: string | null; process_pid: number | null;
  process_manager_verified: boolean; observation_only: boolean;
};

export default function PaperWorkerSupervisor({connected, apiAddress, token}: {
  connected: boolean; apiAddress: string; token: string;
}) {
  const [status, setStatus] = useState<Status | null>(null);
  const [message, setMessage] = useState("Worker durumu doğrulanmadı.");
  const [busy, setBusy] = useState(false);
  const url = useCallback(() => {
    const origin = new URL(apiAddress);
    if (origin.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(origin.hostname) ||
        origin.username || origin.password) throw new Error("invalid_origin");
    return origin.origin + "/api/v1/paper/worker";
  }, [apiAddress]);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!connected) { setStatus(null); setMessage("Yerel API bağlantısı gerekli."); return; }
    try {
      const response = await fetch(url(), {headers: {"X-Admin-Token": token}, cache: "no-store", signal});
      if (!response.ok) throw new Error("unavailable");
      const value = await response.json() as Status;
      if (typeof value.verified_running !== "boolean" || typeof value.revision !== "number") {
        throw new Error("invalid_schema");
      }
      if (signal?.aborted) return;
      setStatus(value);
      setMessage(value.verified_running
        ? "Worker heartbeat ve lease geçerli; bu durum işlem açma yetkisi vermez."
        : value.stale
          ? "Son worker heartbeat süresi dolmuş; süreç çalışıyor kabul edilmiyor."
          : value.state === "STOP_REQUESTED"
            ? "Durdurma istendi; heartbeat STOPPED doğrulayana kadar kapandı sayılmaz."
            : `Worker durumu: ${value.state}.`);
    } catch {
      if (!signal?.aborted) { setStatus(null); setMessage("Worker durumu alınamadı; süreç çalışıyor gösterilmiyor."); }
    }
  }, [connected, token, url]);
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  async function stop() {
    if (!status || !status.verified_running) return;
    setBusy(true);
    try {
      const response = await fetch(url() + "/stop", {
        method: "POST",
        headers: {
          "Content-Type": "application/json", "X-Admin-Token": token,
          "Idempotency-Key": `stop_${crypto.randomUUID()}`,
        },
        cache: "no-store",
        body: JSON.stringify({expected_revision: status.revision, expected_generation: status.generation}),
      });
      if (response.status === 409) throw new Error("stale");
      if (!response.ok) throw new Error("rejected");
      const value = await response.json() as Status;
      setStatus(value);
      setMessage(value.state === "STOPPED"
        ? "Worker STOPPED olarak doğrulandı."
        : "Durdurma komutu kaydedildi; worker heartbeat kapanışı doğrulanana kadar bekleyin.");
    } catch (error) {
      setMessage(error instanceof Error && error.message === "stale"
        ? "Worker sürümü değişti; durum yenileniyor."
        : "Durdurma komutu reddedildi; worker durumu değişmedi.");
      void refresh();
    } finally { setBusy(false); }
  }

  async function start() {
    if (!status || !status.start_supported || busy) return;
    setBusy(true);
    try {
      const response = await fetch(url() + "/start", {
        method: "POST",
        headers: {
          "Content-Type": "application/json", "X-Admin-Token": token,
          "Idempotency-Key": `start_${crypto.randomUUID()}`,
        },
        cache: "no-store",
        body: JSON.stringify({expected_revision: status.revision, mode: "OBSERVATION_ONLY"}),
      });
      if (!response.ok) throw new Error("rejected");
      const value = await response.json() as Status;
      setStatus(value);
      setMessage(value.verified_running && value.observation_only
        ? "Gözlem worker'ı doğrulandı; bu durum işlem açma yetkisi vermez."
        : "Worker başlatma isteği process attestation ile doğrulanmadı.");
    } catch {
      setMessage("Gözlem worker'ı başlatılamadı; durum fail-closed kaldı.");
      void refresh();
    } finally { setBusy(false); }
  }

  useEffect(() => {
    if (!connected) return;
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [connected, refresh]);

  return <div className="worker-supervisor" aria-live="polite">
    <strong>{status?.verified_running ? "RUNNING · heartbeat doğrulandı" : status?.state ?? "UNKNOWN"}</strong>
    <p>{message}</p>
    {status?.worker_id && <small>Worker {status.worker_id} · nesil {status.generation} · rev {status.revision} · heartbeat {status.heartbeat_at_ms ? new Date(status.heartbeat_at_ms).toLocaleString("tr-TR") : "yok"}{status.observation_only ? " · yalnız gözlem" : ""}</small>}
    <div><button type="button" disabled={!status?.start_supported || busy || status?.verified_running} onClick={() => void start()} title={status?.start_supported ? "G\u00f6zlem worker'\u0131n\u0131 ba\u015flat" : "Ba\u015flatma kapal\u0131"}>{status?.start_supported ? "G\u00f6zlem worker'\u0131n\u0131 ba\u015flat" : "Ba\u015flatma kapal\u0131"}</button>
      <button type="button" disabled={!status?.verified_running || busy} onClick={() => void stop()}>
        {busy ? "Durdurma isteniyor..." : "Güvenli durdurma iste"}
      </button>
      <button type="button" disabled={!connected || busy} onClick={() => void refresh()}>Durumu yenile</button></div>
  </div>;
}
