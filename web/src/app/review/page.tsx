"use client";

import { useState } from "react";

import { FindingLink, KindBadge, Loading } from "@/components/ui";
import { money, post, useApi } from "@/lib/api";
import type { ReviewTask } from "@/lib/types";

export default function ReviewQueue() {
  const tasks = useApi<ReviewTask[]>("/review-tasks");
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Set<string>>(new Set());

  async function decide(task: ReviewTask, approve: boolean) {
    if (busy.has(task.id)) return;
    setBusy(new Set(busy).add(task.id));
    setProblems({ ...problems, [task.id]: "" });
    try {
      await post(`/review-tasks/${task.id}/decision`, { approve, note: notes[task.id] ?? "" });
      tasks.reload();
    } catch (error) {
      setProblems((p) => ({ ...p, [task.id]: error instanceof Error ? error.message : String(error) }));
    } finally {
      setBusy((b) => {
        const next = new Set(b);
        next.delete(task.id);
        return next;
      });
    }
  }

  return (
    <>
      <h1>Review queue</h1>
      <p className="muted">
        Each item is a finding the system would not send on its own. Approving it says the finding is right and may be
        appealed; it does not send anything.
      </p>
      <Loading error={tasks.error} loading={tasks.loading} />
      {tasks.data && tasks.data.length === 0 ? <p>Nothing waiting.</p> : null}
      <div className="cards">
        {tasks.data?.map((task) => (
          <div className="card" key={task.id}>
            <div className="card-head">
              <FindingLink id={task.finding.finding_id}>{task.finding.line_id}</FindingLink>
              <KindBadge kind={task.finding.kind} />
              <span className="strong">{Number(task.finding.recoverable) ? money(task.finding.recoverable) : ""}</span>
            </div>
            <p className="small">{task.finding.explanation}</p>
            <ul className="small reasons">
              {task.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
            <div className="actions">
              <input
                aria-label={`Note for ${task.finding.line_id}`}
                placeholder="Note (what you checked)"
                value={notes[task.id] ?? ""}
                onChange={(e) => setNotes({ ...notes, [task.id]: e.target.value })}
              />
              <button onClick={() => decide(task, true)} disabled={busy.has(task.id)}>
                Finding is right
              </button>
              <button className="secondary" onClick={() => decide(task, false)} disabled={busy.has(task.id)}>
                Reject
              </button>
            </div>
            {problems[task.id] ? (
              <div className="callout error" role="alert">
                {problems[task.id]}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </>
  );
}
