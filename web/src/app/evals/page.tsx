"use client";

import { Loading } from "@/components/ui";
import { useApi } from "@/lib/api";

type Metrics = Record<string, number | Record<string, Record<string, number>>>;

const GROUPS: { title: string; note: string; metrics: string[] }[] = [
  {
    title: "Safety (zero tolerance in CI)",
    note: "Any miss here fails the pull request, including when a new baseline is being recorded.",
    metrics: ["auto_precision", "auto_on_suspect", "injection_unsafe", "false_claim_dollars", "duplicate_findings", "missing_findings"],
  },
  {
    title: "Extraction",
    note: "Contract terms read from the PDFs, compared with the generator's ground truth.",
    metrics: ["term_precision", "term_recall", "value_recall", "contract_exact", "citation_precision", "injection_recall"],
  },
  {
    title: "Audit",
    note: "Every remittance line classified and priced, compared with its label.",
    metrics: ["line_accuracy", "detect_precision", "detect_recall", "dollar_recall", "review_rate"],
  },
];

export default function Evals() {
  const report = useApi<Record<string, Metrics>>("/evals/latest");
  const datasets = report.data ? Object.keys(report.data) : [];
  return (
    <>
      <h1>Evals</h1>
      <p className="muted">
        The same suite gates every pull request. The holdout set uses contract wording the extractor was never tuned
        on: its low scores are the honest generalisation gap, and every holdout contract fails closed rather than
        guessing.
      </p>
      <Loading error={report.error} loading={report.loading} />
      {GROUPS.map((group) =>
        report.data ? (
          <section className="section" key={group.title}>
            <h2>{group.title}</h2>
            <p className="small muted">{group.note}</p>
            <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Metric</th>
                  {datasets.map((d) => (
                    <th className="num" key={d}>
                      {d}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {group.metrics.map((metric) => (
                  <tr key={metric}>
                    <td className="mono">{metric}</td>
                    {datasets.map((d) => (
                      <td className="num" key={d}>
                        {String(report.data?.[d]?.[metric] ?? "-")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </section>
        ) : null,
      )}
    </>
  );
}
