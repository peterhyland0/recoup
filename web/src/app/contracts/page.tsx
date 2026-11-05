"use client";

import Link from "next/link";

import { Loading, StatusBadge } from "@/components/ui";
import { useApi } from "@/lib/api";
import type { DocumentSummary } from "@/lib/types";

export default function Contracts() {
  const documents = useApi<DocumentSummary[]>("/contracts");
  return (
    <>
      <h1>Contracts</h1>
      <p className="muted">
        A contract is <em>active</em> when every value was found in its own words with no doubts. Anything else waits
        for a person: findings priced from it are never sent automatically.
      </p>
      <Loading error={documents.error} loading={documents.loading} />
      {documents.data ? (
        <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Contract</th>
              <th>Status</th>
              <th>Read by</th>
              <th>First issue</th>
            </tr>
          </thead>
          <tbody>
            {documents.data.map((d) => (
              <tr key={d.id}>
                <td>
                  <Link href={`/contract?id=${d.id}`}>{d.contract_id ?? d.filename}</Link>
                </td>
                <td>
                  <StatusBadge status={d.status} />
                  {d.injection_span_ids.length ? <span className="badge status-failed">injected text</span> : null}
                </td>
                <td className="mono small">{d.extractor}</td>
                <td className="small">{d.issues[0] ?? <span className="muted">none</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
    </>
  );
}
