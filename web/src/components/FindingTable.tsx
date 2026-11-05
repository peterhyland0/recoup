"use client";

import { FindingLink, KindBadge, RouteBadge, StatusBadge } from "@/components/ui";
import { money } from "@/lib/api";
import type { Finding } from "@/lib/types";

export function FindingTable({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) return <p className="muted">Nothing here.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Line</th>
            <th>Code</th>
            <th>Result</th>
            <th className="num">Contract</th>
            <th className="num">Payer allowed</th>
            <th className="num">Recoverable</th>
            <th>Appeal by</th>
            <th>Route</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((f) => (
            <tr key={f.finding_id}>
              <td>
                <FindingLink id={f.finding_id}>{f.line_id}</FindingLink>
              </td>
              <td className="mono">
                {f.code}
                {f.modifiers.length ? `-${f.modifiers.join("-")}` : ""}
              </td>
              <td>
                <KindBadge kind={f.kind} />
              </td>
              <td className="num">{money(f.expected_allowed)}</td>
              <td className="num">{money(f.payer_allowed)}</td>
              <td className="num strong">{Number(f.recoverable) ? money(f.recoverable) : "-"}</td>
              <td>{f.appeal_deadline ?? "-"}</td>
              <td>
                <RouteBadge route={f.route} />
              </td>
              <td>
                <StatusBadge status={f.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
