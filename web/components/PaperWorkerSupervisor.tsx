"use client";

import { useCallback, useEffect, useState } from "react";

type Status = {
  state: string; revision: number; worker_id: string | null;
  heartbeat_at_ms: number | null; lease_until_ms: number | null;
  verified_running: boolean; stale: boolean; start_supported: boolean;
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
        body: JSON.stringify({expected_revision: status.revision}),
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

  return <div className="worker-supervisor" aria-live="polite">
    <strong>{status?.verified_running ? "RUNNING · heartbeat doğrulandı" : status?.state ?? "UNKNOWN"}</strong>
    <p>{message}</p>
    {status?.worker_id && <small>Worker {status.worker_id} · rev {status.revision} · heartbeat {status.heartbeat_at_ms ? new Date(status.heartbeat_at_ms).toLocaleString("tr-TR") : "yok"}</small>}
    <div><button type="button" disabled title="Otomatik başlatma bu sürümde kapalı">Başlatma kapalı</button>
      <button type="button" disabled={!status?.verified_running || busy} onClick={() => void stop()}>
        {busy ? "Durdurma isteniyor..." : "Güvenli durdurma iste"}
      </button>
      <button type="button" disabled={!connected || busy} onClick={() => void refresh()}>Durumu yenile</button></div>
  </div>;
}
