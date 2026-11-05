"use client";

import { useEffect, useRef } from "react";

import type { Span } from "@/lib/types";

/**
 * The contract as numbered lines. Cited lines are highlighted, injected lines are struck
 * through, and the first highlighted line scrolls into view, so a reviewer checks the
 * clause in place instead of trusting a summary of it.
 */
export function ContractText({
  spans,
  cited,
  injected = [],
  focus,
}: {
  spans: Span[];
  cited: string[];
  injected?: string[];
  focus?: string | null;
}) {
  const citedSet = new Set(cited);
  const injectedSet = new Set(injected);
  const target = focus ?? cited[0] ?? null;
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!target || !box.current) return;
    const line = box.current.querySelector<HTMLElement>(`[data-span="${target}"]`);
    // offsetTop is measured from the scroll box itself (.contract is position: relative).
    if (line) box.current.scrollTop = Math.max(0, line.offsetTop - 60);
  }, [target]);

  return (
    <div className="contract" ref={box}>
      {spans.map((span, index) => {
        const newPage = index === 0 || spans[index - 1].page !== span.page;
        const header = newPage ? <div className="page-break">Page {span.page}</div> : null;
        const classes = [
          "line",
          citedSet.has(span.span_id) ? "cited" : "",
          injectedSet.has(span.span_id) ? "injected" : "",
          span.span_id === target ? "focus" : "",
        ].join(" ");
        return (
          <div key={span.span_id}>
            {header}
            <div className={classes} data-span={span.span_id} title={injectedSet.has(span.span_id) ? "Flagged: addressed to an automated reader. Never used as a source." : undefined}>
              <span className="line-no">{span.line}</span>
              <span className="line-text">{span.text}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
