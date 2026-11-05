"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { ContractText } from "@/components/ContractText";
import { Loading, Section, StatusBadge } from "@/components/ui";
import { pdfUrl, useApi } from "@/lib/api";
import type { DocumentDetail } from "@/lib/types";

function ContractDetail({ id }: { id: string }) {
  const document = useApi<DocumentDetail>(`/contracts/${id}`);
  const [focus, setFocus] = useState<string | null>(null);
  const d = document.data;

  if (!d || d.id !== id) return <Loading error={document.error} loading={document.loading} />;

  const evidence = d.ingested.evidence;
  return (
    <>
      <p className="crumbs">
        <Link href="/contracts">Contracts</Link> / {d.contract_id ?? d.filename}
      </p>
      <h1>{d.contract_id ?? d.filename}</h1>
      <div className="badges">
        <StatusBadge status={d.status} />
        <span className="mono small">read by {d.extractor}</span>
        <a href={pdfUrl(d.id)} target="_blank" rel="noreferrer">
          Original PDF
        </a>
      </div>
      {d.issues.length ? (
        <div className="callout warn">
          <strong>Why a person should check this contract</strong>
          <ul>
            {d.issues.slice(0, 12).map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="two-col wide-right">
        <Section title={`Extracted terms (${evidence.length})`}>
          <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Term</th>
                <th>Value</th>
                <th className="num">Confidence</th>
                <th>Cited</th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((e) => (
                <tr key={`${e.key}-${e.span_ids.join()}`} className={e.grounded ? "" : "rejected-row"}>
                  <td className="mono">{e.key}</td>
                  <td className="mono">{e.value}</td>
                  <td className={e.confidence < 0.85 ? "num warn-text" : "num"}>{e.confidence.toFixed(2)}</td>
                  <td>
                    {e.span_ids.map((sid) => (
                      <button key={sid} className="link" onClick={() => setFocus(sid)}>
                        {sid}
                      </button>
                    ))}
                    {e.issues.length ? <div className="small warn-text">{e.issues.join("; ")}</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </Section>
        <Section title="Contract text">
          <p className="small muted">Yellow: a line some term was read from. Struck through: text addressed to an automated reader, never used.</p>
          <ContractText
            spans={d.spans}
            cited={evidence.filter((e) => e.grounded).flatMap((e) => e.span_ids)}
            injected={d.injection_span_ids}
            focus={focus}
          />
        </Section>
      </div>
    </>
  );
}

function FromQuery() {
  const id = useSearchParams().get("id");
  if (!id) return <p>No contract selected.</p>;
  return <ContractDetail key={id} id={id} />;
}

export default function Page() {
  return (
    <Suspense fallback={<p className="muted">Loading...</p>}>
      <FromQuery />
    </Suspense>
  );
}
