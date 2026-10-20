import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
export const metadata: Metadata = {title:"TradePilot | Paper Trading",description:"Geliştirme amaçlı risk yönetimi paneli"};
export default function RootLayout({children}:{children:ReactNode}) {return <html lang="tr"><body>{children}</body></html>;}
