export type Kind =
  | "correct"
  | "underpaid"
  | "overpaid"
  | "denial_invalid"
  | "denial_valid"
  | "denial_other"
  | "unpriced"
  | "no_contract"
  | "skipped";

export interface Evidence {
  key: string;
  value: string;
  span_ids: string[];
  confidence: number;
  grounded: boolean;
  issues: string[];
}

export interface Step {
  rule: string;
  detail: string;
  amount: string;
  term_key: string | null;
  source: string | null;
  span_ids: string[];
}

export interface Finding {
  finding_id: string;
  line_id: string;
  claim_id: string;
  payer_claim_id: string;
  check_number: string;
  payment_date: string;
  service_date: string;
  code: string;
  modifiers: string[];
  units: string;
  billed: string;
  contract_id: string | null;
  document_id: string | null;
  kind: Kind;
  expected_allowed: string | null;
  payer_allowed: string;
  variance: string;
  recoverable: string;
  carcs: string[];
  trace: Step[];
  evidence: Evidence[];
  explanation: string;
  appeal_deadline: string | null;
  route: "auto" | "review";
  review_reasons: string[];
  status: string;
  review?: { id: string; status: string; note: string | null; decided_by: string | null } | null;
}

export interface Summary {
  lines: number;
  by_kind: Record<string, number>;
  open_reviews: number;
  recoverable: { ready: string; in_review: string; past_deadline: string; appealed: string; won: string; lost: string };
  contracts: Record<string, number>;
  today: string;
}

export interface ReviewTask {
  id: string;
  status: string;
  reasons: string[];
  finding: Finding;
}

export interface Appeal {
  id: string;
  claim_id: string;
  payer_id: string;
  finding_ids: string[];
  amount: string;
  status: string;
  letter: string;
  policy_issues: string[];
  portal_appeal_id: string | null;
  approved_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentSummary {
  id: string;
  filename: string;
  status: "active" | "needs_review" | "failed";
  contract_id: string | null;
  payer_id: string | null;
  extractor: string;
  issues: string[];
  injection_span_ids: string[];
  created_at: string;
}

export interface Span {
  span_id: string;
  page: number;
  line: number;
  text: string;
}

export interface DocumentDetail extends DocumentSummary {
  spans: Span[];
  ingested: { evidence: Evidence[]; issues: string[]; injection_span_ids: string[] };
}
