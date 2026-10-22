"use client";

import { useEffect, useState } from "react";

type Capabilities = {
  source: "application_api";
  available: boolean;
  selectable: boolean;
  active_profile_version: number | null;
  active_risk_level: number | null;
  allowed_levels: number[];
  reason: string;
};

export default function RiskCapabilitiesPanel({connected, apiAddress, token}: {
  connected: boolean; apiAddress: string; token: string;
}) {
  const [status, setStatus] = useState("Bağlantı yok; risk profili doğrulanamadı.");
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    async function load() {
      try {
        const origin = new URL(apiAddress);
        if (origin.protocol !== "http:" ||
            !["localhost", "127.0.0.1"].includes(origin.hostname) ||
            origin.username || origin.password) throw new Error("invalid_origin");
        const response = await fetch(origin.origin + "/api/v1/risk/profiles/capabilities", {
          headers: {"X-Admin-Token": token}, cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error("unavailable");
        const data: Capabilities = await response.json();
        if (data.source !== "application_api" ||
            typeof data.available !== "boolean" ||
            typeof data.selectable !== "boolean" ||
            !Array.isArray(data.allowed_levels)) throw new Error("invalid_contract");
        if (!controller.signal.aborted) {
          setStatus(
            !data.available || !data.selectable
              ? "Bağımsız risk profili bu uygulamada henüz etkin değil; seçim kapalı."
              : "Risk profili API tarafından bildirildi ancak güvenli yazma sözleşmesi henüz doğrulanmadı; seçim kapalı."
          );
        }
      } catch {
        if (!controller.signal.aborted) setStatus("Risk profili API doğrulanamadı; seçim güvenlik nedeniyle kapalı.");
      }
    }
    setStatus("Risk profili API kontrol ediliyor...");
    void load();
    return () => controller.abort();
  }, [connected, apiAddress, token]);
  return <p role="status" aria-live="polite">{status}</p>;
}
