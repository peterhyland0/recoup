"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";
import type { Kind } from "@/lib/types";

const KIND_LABEL: Record<Kind, string> = {
  correct: "Paid correctly",
  underpaid: "Underpaid",
  overpaid: "Overpaid",
  denial_invalid: "Invalid denial",
  denial_valid: "Valid denial",
  denial_other: "Denial to check",
  unpriced: "Unpriced",
  no_contract: "No contract",
  skipped: "Not audited",
};

export function KindBadge({ kind }: { kind: Kind }) {
  return <span className={`badge kind-${kind}`}>{KIND_LABEL[kind] ?? kind}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge status-${status}`}>{status.replaceAll("_", " ")}</span>;
}

export function RouteBadge({ route }: { route: "auto" | "review" }) {
  return <span className={`badge route-${route}`}>{route === "auto" ? "auto" : "needs review"}</span>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </div>
  );
}

export function Loading({ error, loading }: { error: Error | null; loading: boolean }) {
  if (error instanceof ApiError) {
    const what = error.status === 404 ? "Not found" : error.status === 403 ? "Not allowed" : `Error ${error.status}`;
    return (
      <div className="callout error" role="alert">
        {what}: {error.message}
      </div>
    );
  }
  if (error) {
    return (
      <div className="callout error" role="alert">
        Could not reach the API ({error.message}). Start it with <code>uv run recoup serve</code>.
      </div>
    );
  }
  return loading ? <p className="muted">Loading...</p> : null;
}

export function FindingLink({ id, children }: { id: string; children: ReactNode }) {
  return <Link href={`/finding?id=${encodeURIComponent(id)}`}>{children}</Link>;
}

export function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}
