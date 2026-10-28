// Types mirror api/app.py and invoice_agent/models.py.

export type RunStatus = "processing" | "paid" | "rejected" | "needs_review" | "duplicate" | "failed";
export type Severity = "error" | "warning";

export interface Sample {
  id: string;
  name: string;
  folder: string;
  processed: boolean;
}

export interface RunSummary {
  id: number;
  file: string;
  invoice_number: string | null;
  vendor: string | null;
  total_cents: number | null;
  currency: string;
  status: RunStatus;
  stage: string;
  headline: string;
  started_at: string;
  duration_ms: number;
}

export interface LineItem {
  description: string;
  quantity: number | null;
  unit_price: number | null;
  stated_amount: number | null;
  note: string | null;
}

export interface Invoice {
  invoice_number: string | null;
  vendor: string | null;
  invoice_date: string | null;
  due_date: string | null;
  currency: string | null;
  line_items: LineItem[];
  stated_subtotal: number | null;
  stated_tax_rate: number | null;
  stated_tax_amount: number | null;
  stated_shipping: number | null;
  stated_total: number | null;
  payment_terms: string | null;
  revision: string | null;
  risk_signals: { description: string; quote: string }[];
}

export interface Finding {
  code: string;
  severity: Severity;
  message: string;
  line: number | null;
}

export interface Decision {
  outcome: "approve" | "reject" | "needs_review";
  decided_by: "policy" | "vp_critic" | "human";
  policy_mode: string;
  rationale: string;
  reasons: string[];
  rounds: number;
  critic_issues: string[];
  review: { outcome: string; comment: string; reviewer: string; reviewed_at: string } | null;
}

export interface Payment {
  status: "paid" | "already_paid" | "nothing_due";
  amount_cents: number;
  currency: string;
  approved_total_cents: number;
  previously_paid_cents: number;
  idempotency_key: string;
}

export interface RunState {
  source_text: string | null;
  invoice: Invoice | null;
  identity: { number: string; key: string } | null;
  duplicate: { kind: string; earlier_run_id: number | null; earlier_status: RunStatus | null; earlier_file: string | null; message: string } | null;
  validation: {
    lines: { line: number; description: string; sku: string | null }[];
    findings: Finding[];
    computed: { line_amounts_cents: (number | null)[]; subtotal_cents: number | null; tax_cents: number | null };
    agent_summary: string;
    overrides: string[];
  } | null;
  policy: { mode: string; reasons: string[] } | null;
  decision: Decision | null;
  payment: Payment | null;
  usage: { requests: number; input_tokens: number; output_tokens: number; tool_calls: number };
  error: string | null;
}

export interface PipelineEvent {
  id: number;
  ts: string;
  stage: string;
  type: string;
  message: string;
  payload: Record<string, unknown>;
}

export interface RunDetail extends RunSummary {
  state: RunState;
  due_date: string | null;
  due_on_approval_cents: number | null;
  events: PipelineEvent[];
}

export interface Metrics {
  by_status: Partial<Record<RunStatus, number>>;
  findings_by_code: Record<string, number>;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail ?? `${response.status} ${response.statusText}`);
  }
  return response.json();
}

export const api = {
  samples: () => request<Sample[]>("/api/samples"),
  invoices: () => request<RunSummary[]>("/api/invoices"),
  invoice: (id: number) => request<RunDetail>(`/api/invoices/${id}`),
  metrics: () => request<Metrics>("/api/metrics"),
  start: (source: { sample: string } | { file: File }) => {
    const form = new FormData();
    if ("sample" in source) form.append("sample", source.sample);
    else form.append("file", source.file);
    return request<RunSummary>("/api/runs", { method: "POST", body: form });
  },
  review: (id: number, decision: "approve" | "reject", comment: string) =>
    request<RunDetail>(`/api/invoices/${id}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, comment }),
    }),
};

export function money(cents: number | null | undefined, currency = "USD"): string {
  if (cents == null) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(cents / 100);
}

export const STATUS_LABEL: Record<RunStatus, string> = {
  processing: "Processing",
  paid: "Paid",
  rejected: "Rejected",
  needs_review: "Needs review",
  duplicate: "Duplicate",
  failed: "Failed",
};

export const STAGE_ACTIVE: Record<string, string> = {
  load: "Loading",
  extract: "Extracting",
  check_duplicate: "Checking duplicates",
  validate: "Validating",
  policy_gate: "Checking policy",
  approval: "VP + critic reviewing",
  payment: "Paying",
  rejection: "Recording rejection",
};

export const STAGE_LABEL: Record<string, string> = {
  load: "Load",
  extract: "Extract",
  check_duplicate: "Duplicate check",
  validate: "Validate",
  policy_gate: "Policy gate",
  approval: "Approval",
  payment: "Payment",
  rejection: "Rejection",
  needs_review: "Review",
  paid: "Paid",
  rejected: "Rejected",
  duplicate: "Duplicate",
  failed: "Failed",
};
