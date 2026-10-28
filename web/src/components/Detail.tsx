import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, money, STAGE_ACTIVE, STAGE_LABEL, STATUS_LABEL, type PipelineEvent, type RunDetail } from "../api";
import { StageTrack } from "./StageTrack";
import { Bullets, Button, clock, humanize, Row, Section, Sentences, SeverityMark, Spinner, StatusTag } from "./ui";

const TABS = ["Summary", "Document", "Agent log"] as const;
type Tab = (typeof TABS)[number];

const DECIDED_BY = { policy: "policy rules", vp_critic: "VP + critic agents", human: "a human reviewer" };
const OUTCOME = { approve: "Approved", reject: "Rejected", needs_review: "Sent to human review" };

export function Detail({ run, onSelect }: { run: RunDetail; onSelect: (id: number) => void }) {
  const [tab, setTab] = useState<Tab>("Summary");
  const processing = run.status === "processing";
  const queued = run.stage === "queued";  // the worker hasn't started it yet
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="border-b border-line px-5 pt-6 sm:px-8">
        {run.invoice_number && <p className="font-mono text-xs text-muted">{run.file}</p>}
        <div className="mt-3 flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <h2 className="text-3xl font-light tracking-wide sm:text-4xl">{run.invoice_number ?? (processing ? run.file : "No invoice number")}</h2>
            {!queued && <p className="mt-1 truncate text-muted">{run.vendor ?? (processing ? "Reading the document…" : "No vendor")}</p>}
          </div>
          <div className="text-right">
            <div className="text-3xl font-light tabular-nums sm:text-4xl">{money(run.total_cents, run.currency)}</div>
            <StatusTag status={run.status} queued={queued} className="mt-2" />
          </div>
        </div>
        <div className="mt-8 mb-10">
          <StageTrack run={run} queued={queued} />
        </div>
        <nav className="-mb-px flex gap-8" role="tablist">
          {TABS.map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`border-b pb-3 text-[11px] font-medium tracking-[0.2em] uppercase transition-colors ${tab === t ? "border-ink text-ink" : "border-transparent text-faint hover:text-ink"}`}
            >
              {t}
            </button>
          ))}
        </nav>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
        {tab === "Summary" && <Summary run={run} queued={queued} onSelect={onSelect} />}
        {tab === "Document" && <Document run={run} />}
        {tab === "Agent log" && <AgentLog events={run.events} />}
      </div>
    </div>
  );
}

function Summary({ run, queued, onSelect }: { run: RunDetail; queued: boolean; onSelect: (id: number) => void }) {
  const { state } = run;
  const decision = state.decision;
  const findings = state.validation?.findings ?? [];
  const due = run.due_on_approval_cents;

  return (
    <div className="max-w-4xl space-y-10">
      {run.status === "processing" && <Working run={run} queued={queued} />}
      {run.status === "needs_review" && <ReviewBox run={run} />}

      <dl className="border-t border-line">
        {decision && (
          <Row label="Decision">
            {OUTCOME[decision.outcome]} <span className="text-muted">by {DECIDED_BY[decision.decided_by]}</span>
            {decision.rounds > 0 && <span className="text-muted"> · {decision.rounds} critique round{decision.rounds === 1 ? "" : "s"}</span>}
          </Row>
        )}
        {(run.total_cents != null || run.status !== "processing") && <Row label="Total"><span className="tabular-nums">{money(run.total_cents, run.currency)}</span></Row>}
        {due != null && due !== run.total_cents && (
          <Row label="Due on approval">
            <span className="tabular-nums">{money(due, run.currency)}</span>{" "}
            <span className="text-muted">({due === 0 ? "already paid in full" : `${money((run.total_cents ?? 0) - due, run.currency)} already paid`})</span>
          </Row>
        )}
        {run.due_date && <Row label="Due date"><span className="tabular-nums">{run.due_date}</span></Row>}
        {state.payment && (
          <Row label="Payment">
            <span className="tabular-nums">{money(state.payment.amount_cents, state.payment.currency)}</span>
            {state.payment.status !== "paid" && <span className="text-muted"> ({humanize(state.payment.status).toLowerCase()})</span>}
            {state.payment.previously_paid_cents > 0 && state.payment.status === "paid" && (
              <span className="text-muted"> (approved {money(state.payment.approved_total_cents, state.payment.currency)} minus {money(state.payment.previously_paid_cents, state.payment.currency)} already paid)</span>
            )}
          </Row>
        )}
        {run.status === "duplicate" && state.duplicate?.earlier_run_id && (
          <Row label="Duplicate of">
            <button className="underline decoration-line-strong underline-offset-4 hover:decoration-ink" onClick={() => onSelect(state.duplicate!.earlier_run_id!)}>
              {state.duplicate.earlier_file ?? `Run ${state.duplicate.earlier_run_id}`}
            </button>{" "}
            {state.duplicate.earlier_status && <span className="text-muted">({STATUS_LABEL[state.duplicate.earlier_status].toLowerCase()})</span>}
          </Row>
        )}
        {decision && <Row label="Rationale"><Sentences text={decision.rationale} /></Row>}
        {decision?.review && (
          <Row label="Reviewer">
            {decision.review.reviewer}: {decision.review.outcome}
            {decision.review.comment && <span className="text-muted"> — {decision.review.comment}</span>}
          </Row>
        )}
        {decision && decision.critic_issues.length > 0 && (
          <Row label="Critic issues"><Bullets items={decision.critic_issues} /></Row>
        )}
        {state.error && <Row label="Error"><span className="text-serious-ink">{state.error}</span></Row>}
      </dl>

      {findings.length > 0 && (
        <Section title={`Issues · ${findings.length}`}>
          <ul className="border-t border-line">
            {findings.map((f, i) => (
              <li key={i} className="grid grid-cols-[3.5rem_11rem_minmax(0,1fr)] gap-4 border-b border-line py-3 text-sm max-sm:grid-cols-[3.5rem_minmax(0,1fr)]">
                <SeverityMark severity={f.severity} />
                <span className="text-muted max-sm:hidden">{humanize(f.code)}</span>
                <span>{f.message}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function Working({ run, queued }: { run: RunDetail; queued: boolean }) {
  const last = [...run.events].reverse().find((e) => e.type === "stage_completed");
  if (queued) {
    return (
      <div className="border border-line p-5">
        <p className="eyebrow">Queued</p>
        <p className="mt-1.5 text-sm text-muted">Invoices run one at a time in the order they were added, so duplicates are judged in order. This one starts when the earlier ones finish.</p>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-4 border border-line-strong p-5">
      <Spinner className="mt-1 size-3.5" />
      <div>
        <p className="eyebrow text-ink">{STAGE_ACTIVE[run.stage] ?? run.stage}…</p>
        {last && <p className="mt-1.5 text-sm text-muted">{STAGE_LABEL[last.stage]}: {last.message}</p>}
      </div>
    </div>
  );
}

function ReviewBox({ run }: { run: RunDetail }) {
  const [comment, setComment] = useState("");
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const review = useMutation({
    mutationFn: (decision: "approve" | "reject") => api.review(run.id, decision, comment.trim()),
    onSuccess: (detail) => {
      queryClient.setQueryData(["invoice", run.id], detail);
      for (const key of ["invoices", "metrics"]) queryClient.invalidateQueries({ queryKey: [key] });
    },
  });
  const total = run.total_cents ?? 0;
  const due = run.due_on_approval_cents ?? total;
  const question =
    due === total
      ? `${money(due, run.currency)} will be paid to ${run.vendor}.`
      : due === 0
        ? `Nothing more is due on ${money(total, run.currency)}; it is already paid in full.`
        : `${money(due, run.currency)} will be paid to ${run.vendor} (${money(total, run.currency)} minus ${money(total - due, run.currency)} already paid).`;

  return (
    <section className="border border-warn/60 p-5 sm:p-6" aria-label="Review">
      <p className="eyebrow flex items-center gap-2 text-warn-ink">
        <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-warn" /> Awaiting your decision
      </p>
      <ul className="mt-4 space-y-1.5 text-[15px]">
        {run.state.decision?.reasons.map((r) => (
          <li key={r} className="grid grid-cols-[1rem_1fr]">
            <span aria-hidden className="text-warn-ink">—</span>
            {r}
          </li>
        ))}
      </ul>
      <label className="mt-6 block">
        <span className="eyebrow">Comment (optional)</span>
        <textarea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          rows={1}
          placeholder="e.g. Rush pricing agreed with purchasing"
          className="mt-2 block w-full resize-none border-b border-line-strong bg-transparent py-1.5 text-[15px] outline-none field-sizing-content placeholder:text-faint focus:border-ink"
        />
      </label>
      <div className="mt-6">
        {confirming ? (
          <div>
            <p className="text-[15px]"><span className="eyebrow mr-3 text-ink">Confirm</span>{question}</p>
            <div className="mt-4 flex flex-wrap gap-3">
            <Button onClick={() => review.mutate("approve")} disabled={review.isPending}>
              {review.isPending ? "Paying…" : due > 0 ? `Confirm · Pay ${money(due, run.currency)}` : "Confirm"}
            </Button>
            <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap gap-3">
            <Button onClick={() => setConfirming(true)}>Approve{due > 0 ? ` · Pay ${money(due, run.currency)}` : ""}</Button>
            <Button variant="danger" onClick={() => review.mutate("reject")} disabled={review.isPending}>Reject</Button>
          </div>
        )}
      </div>
      {review.error && <p className="mt-3 text-sm text-bad-ink">{review.error.message}</p>}
    </section>
  );
}

function Document({ run }: { run: RunDetail }) {
  const { state } = run;
  const invoice = state.invoice;
  const lines = state.validation?.lines ?? [];
  const amounts = state.validation?.computed.line_amounts_cents ?? [];
  const amount = (value: number | null) => (value == null ? null : money(Math.round(value * 100), run.currency));
  const field = (label: string, value: string | null | undefined, normalized?: string | null) => (
    <Row label={label}>
      {value ?? <span className="text-faint">Not on the document</span>}
      {normalized && normalized !== value && <Normalized value={normalized} />}
    </Row>
  );

  return (
    <div className="grid gap-10 xl:grid-cols-2">
      <Section title="Source document" aside={<span className="font-mono text-xs text-faint">{run.file}</span>}>
        <pre className="max-h-[70vh] overflow-auto border border-line bg-sunken p-5 font-mono text-xs leading-relaxed whitespace-pre-wrap text-muted">
          {state.source_text ?? "Not loaded yet."}
        </pre>
      </Section>
      <div className="space-y-10">
        {!invoice ? (
          <p className="text-sm text-faint">Nothing extracted yet.</p>
        ) : (
          <>
            <Section title="Extracted by the intake agent">
              <dl className="border-t border-line">
                {field("Invoice number", invoice.invoice_number, state.identity?.number)}
                {field("Vendor", invoice.vendor)}
                {field("Invoice date", invoice.invoice_date)}
                {field("Due date", invoice.due_date)}
                {field("Currency", invoice.currency, invoice.currency ? null : "USD (default)")}
                {field("Subtotal", amount(invoice.stated_subtotal))}
                {invoice.stated_tax_amount != null && field("Tax", amount(invoice.stated_tax_amount))}
                {invoice.stated_shipping != null && field("Shipping", amount(invoice.stated_shipping))}
                {field("Total", amount(invoice.stated_total))}
                {invoice.payment_terms && field("Terms", invoice.payment_terms)}
                {invoice.revision && field("Revision", invoice.revision)}
              </dl>
            </Section>
            <Section title="Line items · matched to inventory">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-y border-line text-left">
                    <tr>
                      <th className="eyebrow py-2.5 pr-3 font-medium">Item</th>
                      <th className="eyebrow px-3 py-2.5 text-right font-medium">Qty</th>
                      <th className="eyebrow px-3 py-2.5 text-right font-medium">Unit</th>
                      <th className="eyebrow py-2.5 pl-3 text-right font-medium">Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invoice.line_items.map((item, i) => {
                      const match = lines[i];
                      return (
                        <tr key={i} className="border-b border-line">
                          <td className="py-3 pr-3">
                            {item.description}
                            {match && (match.sku ? <Normalized value={match.sku} same={match.sku === item.description} /> : <span className="ml-2 text-[10px] font-semibold tracking-[0.2em] text-bad-ink uppercase">Not in inventory</span>)}
                          </td>
                          <td className="px-3 py-3 text-right tabular-nums">{item.quantity ?? "—"}</td>
                          <td className="px-3 py-3 text-right tabular-nums">{amount(item.unit_price) ?? "—"}</td>
                          <td className="py-3 pl-3 text-right tabular-nums">{money(amounts[i], run.currency)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {state.validation?.agent_summary && <p className="text-sm leading-relaxed text-muted">Validation agent: {state.validation.agent_summary}</p>}
              {state.validation?.overrides.map((o) => <p key={o} className="text-sm text-warn-ink">{o}</p>)}
            </Section>
            {invoice.risk_signals.length > 0 && (
              <Section title="Risk signals">
                <ul className="space-y-4 text-sm">
                  {invoice.risk_signals.map((r, i) => (
                    <li key={i}>
                      {r.description}
                      <blockquote className="mt-1.5 border-l border-warn pl-3 font-mono text-xs text-muted">{r.quote}</blockquote>
                    </li>
                  ))}
                </ul>
              </Section>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Normalized({ value, same = false }: { value: string; same?: boolean }) {
  if (same) return <span className="ml-2 text-[10px] font-semibold tracking-[0.2em] text-good-ink uppercase">In inventory</span>;
  return (
    <span className="ml-2 font-mono text-xs text-warn-ink" title="Normalized by code before matching">
      → {value}
    </span>
  );
}

// Lifecycle noise and lines that repeat the stage summary; "Show every event" reveals them.
const QUIET = new Set(["stage_started", "transition", "agent_output", "finding", "payment", "rejection", "duplicate"]);
const isQuiet = (e: PipelineEvent) => QUIET.has(e.type) || (e.type === "stage_completed" && e.stage === "policy_gate");

function AgentLog({ events }: { events: PipelineEvent[] }) {
  const [all, setAll] = useState(false);
  const start = events.length ? Date.parse(events[0].ts) : 0;
  const shown = all ? events : events.filter((e) => !isQuiet(e));

  return (
    <Section
      title="Telemetry · what each agent did"
      aside={
        <label className="eyebrow flex cursor-pointer items-center gap-2">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} className="accent-white" /> All events · {events.length}
        </label>
      }
    >
      <ol className="border-t border-line">
        {shown.map((e) => (
          <li key={e.id} className="grid grid-cols-[5.5rem_1fr] gap-x-4 border-b border-line py-3 text-sm sm:grid-cols-[5.5rem_7.5rem_1fr]">
            <span className="flex h-5 items-center gap-2.5 self-start font-mono text-xs text-faint">
              <span aria-hidden className={`size-1.5 shrink-0 rounded-full ${dot(e)}`} />
              {clock(Date.parse(e.ts) - start)}
            </span>
            <span className="eyebrow pt-0.5 text-[10px] max-sm:hidden">{STAGE_LABEL[e.stage] ?? e.stage}</span>
            <div className="min-w-0 max-sm:col-start-2">
              <EventText event={e} />
            </div>
          </li>
        ))}
      </ol>
    </Section>
  );
}

function EventText({ event: e }: { event: PipelineEvent }) {
  switch (e.type) {
    case "critique": {
      const issues = (e.payload.issues as string[] | undefined) ?? [];
      return (
        <div>
          <p>
            <span className={`mr-2 text-[10px] font-semibold tracking-[0.2em] uppercase ${e.payload.agrees ? "text-good-ink" : "text-warn-ink"}`}>
              Critic {e.payload.agrees ? "agrees" : "objects"}
            </span>
            {e.message}
          </p>
          {issues.length > 0 && <ul className="mt-1 space-y-0.5 text-muted">{issues.map((i) => <li key={i}>— {i}</li>)}</ul>}
        </div>
      );
    }
    case "decision":
      return <p><span className="mr-2 text-[10px] font-semibold tracking-[0.2em] uppercase">VP</span>{e.message}</p>;
    case "tool_call":
      return <p className="font-mono text-xs text-faint">tool {e.message}</p>;
    default:
      return <p className={e.type === "error" ? "text-serious-ink" : e.type === "stage_started" || e.type === "transition" ? "text-faint" : "text-muted"}>{e.message}</p>;
  }
}

function dot(e: PipelineEvent): string {
  if (e.type === "error") return "bg-serious";
  if (e.type === "critique") return e.payload.agrees ? "bg-good" : "bg-warn";
  if (["decision", "payment", "rejection", "review_resolved"].includes(e.type)) return "bg-ink";
  return "bg-line-strong";
}
