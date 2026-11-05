"use client";

import Link from "next/link";

import { FindingTable } from "@/components/FindingTable";
import { KindBadge, Loading, Section, Stat } from "@/components/ui";
import { money, useApi } from "@/lib/api";
import type { Finding, Kind, Summary } from "@/lib/types";

export default function Overview() {
  const summary = useApi<Summary>("/summary");
  const top = useApi<Finding[]>("/findings?kind=underpaid&status=open&limit=8");
  const s = summary.data;

  return (
    <>
      <h1>Overview</h1>
      <Loading error={summary.error} loading={summary.loading} />
      {s ? (
        <>
          <div className="stats">
            <Stat label="Ready to appeal" value={money(s.recoverable.ready)} hint="verified, inside the window" />
            <Stat
              label="Waiting on a reviewer"
              value={money(s.recoverable.in_review)}
              hint={`${s.open_reviews} findings in the review queue`}
            />
            <Stat label="In appeal" value={money(s.recoverable.appealed)} />
            <Stat label="Recovered" value={money(s.recoverable.won)} />
            <Stat
              label="Past the appeal deadline"
              value={money(s.recoverable.past_deadline)}
              hint={`windows closed before ${s.today}`}
            />
          </div>

          <div className="two-col">
            <Section title="Every remittance line, classified">
              <table>
                <tbody>
                  {Object.entries(s.by_kind)
                    .sort((a, b) => b[1] - a[1])
                    .map(([kind, count]) => (
                      <tr key={kind}>
                        <td>
                          <Link href={`/findings?kind=${kind}`}>
                            <KindBadge kind={kind as Kind} />
                          </Link>
                        </td>
                        <td className="num">{count}</td>
                        <td className="num muted">{((count / s.lines) * 100).toFixed(1)}%</td>
                      </tr>
                    ))}
                </tbody>
              </table>
              <p className="muted small">{s.lines} lines audited.</p>
            </Section>
            <Section title="Contracts">
              <table>
                <tbody>
                  {Object.entries(s.contracts).map(([status, count]) => (
                    <tr key={status}>
                      <td>{status.replaceAll("_", " ")}</td>
                      <td className="num">{count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted small">
                Findings from a contract that needs review always wait for a person.{" "}
                <Link href="/contracts">See why each one was flagged</Link>.
              </p>
            </Section>
          </div>
        </>
      ) : null}

      <Section title="Largest open underpayments" aside={<Link href="/findings?kind=underpaid">All</Link>}>
        <Loading error={top.error} loading={top.loading} />
        {top.data ? <FindingTable findings={top.data} /> : null}
      </Section>
    </>
  );
}
