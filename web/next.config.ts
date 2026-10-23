import type { NextConfig } from "next";

const stagingBuild = process.env.NEXT_PUBLIC_TP_STAGING_PREVIEW === "enabled";
const config: NextConfig = {
  reactStrictMode: true,
  // The staging site must not ship the local admin dashboard (including its
  // X-Admin-Token controls). This build-time alias removes it from the graph.
  turbopack: {
    resolveAlias: {
      "../components/Dashboard": stagingBuild
        ? "./components/StagingDashboardUnavailable.tsx"
        : "./components/Dashboard.tsx",
    },
  },
};
export default config;
