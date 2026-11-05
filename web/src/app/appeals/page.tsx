"use client";

import Link from "next/link";

import { Loading, StatusBadge } from "@/components/ui";
import { money, useApi } from "@/lib/api";
import type { Appeal } from "@/lib/types";

export default function Appeals() {
  const appeals = useApi<Appeal[]>("/appeals");
  return (
    <>
      <h1>Appeals</h1>
      <p className="muted">
        Draft an appeal from a finding. It waits here until a person approves it; only then does it go to the payer.
      </p>
      <Loading error={appeals.error} loading={appeals.loading} />
      {appeals.data && appeals.data.length === 0 ? <p>No appeals yet.</p> : null}
      {appeals.data && appeals.data.length > 0 ? (
        <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Claim</th>
              <th className="num">Amount</th>
              <th>Status</th>
              <th>Payer reference</th>
              <th>Approved by</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {appeals.data.map((a) => (
              <tr key={a.id}>
                <td>
                  <Link href={`/appeal?id=${a.id}`}>{a.claim_id}</Link>
                </td>
                <td className="num">{money(a.amount)}</td>
                <td>
                  <StatusBadge status={a.status} />
                </td>
                <td className="mono">{a.portal_appeal_id ?? "-"}</td>
                <td>{a.approved_by ?? "-"}</td>
                <td className="small muted">{a.updated_at.slice(0, 16).replace("T", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
    </>
  );
}
