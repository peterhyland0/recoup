"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { FindingLink, Loading, StatusBadge } from "@/components/ui";
import { money, post, useApi } from "@/lib/api";
import type { Appeal } from "@/lib/types";

interface Me {
  user: string;
  can_decide: boolean;
}

function AppealDetail({ id }: { id: string }) {
  const appeal = useApi<Appeal>(`/appeals/${id}`);
  const me = useApi<Me>("/me");
  const [busy, setBusy] = useState(false);
  const [checked, setChecked] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const a = appeal.data;

  if (!a || a.id !== id) return <Loading error={appeal.error} loading={appeal.loading} />;

  async function act(path: string, body: unknown = {}) {
    setBusy(true);
    setProblem(null);
    try {
      await post(`/appeals/${id}/${path}`, body);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      appeal.reload();
      setBusy(false);
    }
  }

  const canDecide = me.data?.can_decide ?? false;
  const stuck = a.status === "submit_failed" || a.status === "submitting" || a.status === "approved";

  return (
    <>
      <p className="crumbs">
        <Link href="/appeals">Appeals</Link> / {a.claim_id}
      </p>
      <h1>Appeal for claim {a.claim_id}</h1>
      <div className="badges">
        <StatusBadge status={a.status} />
        <span className="strong">{money(a.amount)}</span>
        {a.portal_appeal_id ? <span className="mono">payer ref {a.portal_appeal_id}</span> : null}
        {a.approved_by ? <span className="small muted">decided by {a.approved_by}</span> : null}
      </div>

      {a.policy_issues.length ? (
        <div className={`callout ${a.status === "blocked" ? "error" : "warn"}`}>
          <strong>{a.status === "blocked" ? "Blocked: this appeal cannot be sent" : "Notes"}</strong>
          <ul>
            {a.policy_issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <h2>Letter</h2>
      <pre className="letter">{a.letter || "(no letter drafted)"}</pre>
      <p className="small muted">
        Findings:{" "}
        {a.finding_ids.map((fid) => (
          <span key={fid}>
            <FindingLink id={fid}>{fid.slice(0, 8)}</FindingLink>{" "}
          </span>
        ))}
      </p>

      {a.status === "awaiting_approval" ? (
        <div className="callout approve">
          {canDecide ? (
            <>
              <p>
                Approving sends this letter to the payer portal and records <strong>{me.data?.user}</strong> as the
                approver. The appeal window is checked again at that moment.
              </p>
              <label className="confirm">
                <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} /> I checked
                each amount against the quoted contract lines.
              </label>
              <div className="actions">
                <button onClick={() => act("decision", { approve: true })} disabled={busy || !checked}>
                  {busy ? "Sending..." : "Approve and send"}
                </button>
                <button className="secondary" onClick={() => act("decision", { approve: false })} disabled={busy}>
                  Reject the letter
                </button>
              </div>
            </>
          ) : (
            <p>This key belongs to a service, not a person, so it can draft appeals but not decide them.</p>
          )}
        </div>
      ) : null}

      {stuck && canDecide ? (
        <div className="callout error">
          <p>
            The payer portal has not confirmed this appeal. Retrying is safe: the portal recognises the appeal id, so a
            filing that did land is not made twice.
          </p>
          <div className="actions">
            <button onClick={() => act("retry")} disabled={busy}>
              Retry submission
            </button>
            {a.status === "submit_failed" ? (
              <button className="secondary" onClick={() => act("withdraw")} disabled={busy}>
                Withdraw
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
      {a.status === "blocked" && canDecide ? (
        <div className="actions">
          <button className="secondary" onClick={() => act("withdraw")} disabled={busy}>
            Withdraw and free the findings
          </button>
        </div>
      ) : null}
      {problem ? (
        <div className="callout error" role="alert">
          {problem}
        </div>
      ) : null}
    </>
  );
}

function FromQuery() {
  const id = useSearchParams().get("id");
  if (!id) return <p>No appeal selected.</p>;
  // Keyed on the id: a different appeal is a fresh component, never the last one's state.
  return <AppealDetail key={id} id={id} />;
}

export default function Page() {
  return (
    <Suspense fallback={<p className="muted">Loading...</p>}>
      <FromQuery />
    </Suspense>
  );
}
