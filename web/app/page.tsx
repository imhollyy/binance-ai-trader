import Dashboard from "../components/Dashboard";
import StagingViewer from "../components/StagingViewer";

export default function Page() {
  // Build-time, opt-in. Default local developer UI and tokens are not shipped
  // into the explicitly staged read-only website.
  if (process.env.NEXT_PUBLIC_TP_STAGING_PREVIEW === "enabled") {
    return <StagingViewer apiOrigin={process.env.NEXT_PUBLIC_STAGING_API_ORIGIN}/>;
  }
  return <Dashboard />;
}
