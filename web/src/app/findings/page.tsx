"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { FindingTable } from "@/components/FindingTable";
import { Loading } from "@/components/ui";
import { useApi } from "@/lib/api";
import type { Finding } from "@/lib/types";

const KINDS = ["", "underpaid", "denial_invalid", "overpaid", "denial_other", "denial_valid", "correct", "unpriced",
  "no_contract", "skipped"];
const ROUTES = ["", "auto", "review"];
const STATUSES = ["", "open", "approved", "rejected", "appealed", "won", "lost", "duplicate"];
const LIMIT = 2000;

function Findings() {
  const params = useSearchParams();
  const router = useRouter();
  const query = new URLSearchParams();
  for (const key of ["kind", "route", "status"]) {
    const value = params.get(key);
    if (value) query.set(key, value);
  }
  query.set("limit", String(LIMIT));
  const { data, error, loading } = useApi<Finding[]>(`/findings?${query.toString()}`);

  function set(key: string, value: string) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    router.replace(`/findings?${next.toString()}`);
  }

  const select = (key: string, options: string[]) => (
    <label>
      {key}
      <select value={params.get(key) ?? ""} onChange={(e) => set(key, e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o}>
            {o ? o.replaceAll("_", " ") : "any"}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <>
      <h1>Findings</h1>
      <div className="filters">
        {select("kind", KINDS)}
        {select("route", ROUTES)}
        {select("status", STATUSES)}
        {loading ? <span className="muted">Loading...</span> : null}
        {data ? (
          <span className="muted">
            {data.length === LIMIT ? `first ${LIMIT}` : data.length} shown, largest recoverable first
          </span>
        ) : null}
      </div>
      <Loading error={error} loading={loading} />
      {data ? <FindingTable findings={data} /> : null}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<p className="muted">Loading...</p>}>
      <Findings />
    </Suspense>
  );
}
