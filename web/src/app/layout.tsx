import type { Metadata } from "next";
import Link from "next/link";

import { KeyGate } from "@/components/KeyGate";
import { Nav } from "@/components/Nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "Recoup",
  description: "Payer underpayment audits with every number traced to a contract clause.",
};


export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <Link href="/" className="brand">
            Recoup
          </Link>
          <Nav />
          <span className="synthetic">Synthetic data only</span>
        </header>
        <main className="page">
          <KeyGate>{children}</KeyGate>
        </main>
      </body>
    </html>
  );
}
