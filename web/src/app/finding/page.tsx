"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { ContractText } from "@/components/ContractText";
import { KindBadge, Loading, RouteBadge, Section, StatusBadge } from "@/components/ui";
import { money, post, useApi } from "@/lib/api";
import type { Appeal, DocumentDetail, Finding } from "@/lib/types";

function FindingDetail({ id }: { id: string }) {
  const router = useRouter();
  const finding = useApi<Finding>(`/findings/${id}`);
  const f = finding.data;
  const document = useApi<DocumentDetail>(f?.document_id ? `/contracts/${f.document_id}` : null);
  const [focus, setFocus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  if (!f || f.finding_id !== id) return <Loading error={finding.error} loading={finding.loading} />;

  const cited = f.evidence.flatMap((e) => e.span_ids);
  const actionable = (f.kind === "underpaid" || f.kind === "denial_invalid") && ["open", "approved"].includes(f.status);
  const blocker =
    f.route === "review" && f.review?.status !== "approved"
      ? "A reviewer has to confirm this finding before it can be appealed."
      : !f.appeal_deadline
        ? "The contract states no appeal window."
        : null;


  async function draftAppeal() {
    setBusy(true);
    setProblem(null);
    try {
      const appeal = await post<Appeal>("/appeals", { finding_ids: [id] });
      router.push(`/appeal?id=${appeal.id}`);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
      setBusy(false);
    }
  }

  return (
    <>
      <p className="crumbs">
        <Link href="/findings">Findings</Link> / {f.line_id}
      </p>
      <h1>
        {f.code}
        {f.modifiers.length ? `-${f.modifiers.join("-")}` : ""} on claim {f.claim_id}
      </h1>
      <div className="badges">
        <KindBadge kind={f.kind} />
        <RouteBadge route={f.route} />
        <StatusBadge status={f.status} />
      </div>

      <div className="stats">
        <div className="stat">
          <div className="stat-label">Contract allows</div>
          <div className="stat-value">{money(f.expected_allowed)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Payer allowed</div>
          <div className="stat-value">{money(f.payer_allowed)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Recoverable</div>
          <div className="stat-value strong">{money(f.recoverable)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Appeal deadline</div>
          <div className="stat-value small-value">{f.appeal_deadline ?? "not stated"}</div>
        </div>
      </div>

      <p className="explanation">{f.explanation}</p>

      {f.review_reasons.length ? (
        <div className="callout warn">
          <strong>Why a person checks this first</strong>
          <ul>
            {f.review_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          {f.review ? (
            <p className="small">
              Review {f.review.status}
              {f.review.decided_by ? ` by ${f.review.decided_by}` : ""}
              {f.review.note ? `: ${f.review.note}` : ""}
            </p>
          ) : null}
        </div>
      ) : null}

      {actionable && blocker ? <p className="callout warn">{blocker}</p> : null}
      {actionable && !blocker ? (
        <div className="actions">
          <button onClick={draftAppeal} disabled={busy || f.finding_id !== id}>
            {busy ? "Drafting..." : "Draft appeal"}
          </button>
          <span className="muted small">
            Drafting sends nothing. The appeal window is checked when you draft and again when you approve.
          </span>
          {problem ? <div className="callout error">{problem}</div> : null}
        </div>
      ) : null}

      <div className="two-col wide-right">
        <Section title="How the contract amount is reached">
          <table>
            <thead>
              <tr>
                <th>Step</th>
                <th>Rule</th>
                <th className="num">Running amount</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {f.trace.map((step, index) => (
                <tr key={index}>
                  <td className="mono">{step.rule}</td>
                  <td>{step.detail}</td>
                  <td className="num">{money(step.amount)}</td>
                  <td>
                    {step.span_ids.map((sid) => (
                      <button key={sid} className="link" onClick={() => setFocus(sid)}>
                        {sid}
                      </button>
                    ))}
                    {step.source && step.source !== "base" ? <span className="muted"> amendment {step.source}</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Terms used</h3>
          <table>
            <thead>
              <tr>
                <th>Term</th>
                <th>Value</th>
                <th className="num">Confidence</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {f.evidence.map((e) => (
                <tr key={e.key}>
                  <td className="mono">{e.key}</td>
                  <td className="mono">{e.value}</td>
                  <td className={e.confidence < 0.85 ? "warn-text num" : "num"}>{e.confidence.toFixed(2)}</td>
                  <td className="small muted">{e.issues.join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small muted">
            Paid {f.payment_date} on {f.check_number}. Payer codes: {f.carcs.join(", ") || "none"}.
          </p>
        </Section>

        <Section
          title={`Contract ${f.contract_id ?? ""}`}
          aside={f.document_id ? <Link href={`/contract?id=${f.document_id}`}>Open contract</Link> : null}
        >
          <Loading error={document.error} loading={!!f.document_id && document.loading} />
          {document.data ? (
            <ContractText
              spans={document.data.spans}
              cited={cited}
              injected={document.data.injection_span_ids}
              focus={focus}
            />
          ) : null}
        </Section>
      </div>
    </>
  );
}

function FromQuery() {
  const id = useSearchParams().get("id");
  if (!id) return <p>No finding selected.</p>;
  return <FindingDetail key={id} id={id} />;
}

export default function Page() {
  return (
    <Suspense fallback={<p className="muted">Loading...</p>}>
      <FromQuery />
    </Suspense>
  );
}
