import type { ReactNode } from "react";
import { STATUS_LABEL, type RunStatus, type Severity } from "../api";

// Every status pairs a colored dot with its label, so color is never the only signal.
export const STATUS_TONE: Record<RunStatus, { dot: string; text: string }> = {
  paid: { dot: "bg-good", text: "text-good-ink" },
  rejected: { dot: "bg-bad", text: "text-bad-ink" },
  needs_review: { dot: "bg-warn", text: "text-warn-ink" },
  duplicate: { dot: "bg-neutral", text: "text-muted" },
  failed: { dot: "bg-serious", text: "text-serious-ink" },
  processing: { dot: "bg-ink animate-pulse", text: "text-ink" },
};

export function StatusTag({ status, queued = false, className = "" }: { status: RunStatus; queued?: boolean; className?: string }) {
  const tone = queued ? { dot: "bg-line-strong", text: "text-faint" } : STATUS_TONE[status];
  return (
    <span className={`inline-flex shrink-0 items-center gap-2 text-[11px] font-medium tracking-[0.2em] uppercase ${tone.text} ${className}`}>
      <span aria-hidden className={`size-1.5 rounded-full ${tone.dot}`} />
      {queued ? "Queued" : STATUS_LABEL[status]}
    </span>
  );
}

export function Spinner({ className = "size-3" }: { className?: string }) {
  return <span aria-hidden className={`inline-block animate-spin rounded-full border border-current border-r-transparent ${className}`} />;
}

export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-4 border-b border-line py-3 sm:grid-cols-[11rem_minmax(0,1fr)]">
      <dt className="eyebrow pt-0.5">{label}</dt>
      <dd className="min-w-0 text-[15px]">{children}</dd>
    </div>
  );
}

export function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="eyebrow">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function SeverityMark({ severity }: { severity: Severity }) {
  return severity === "error" ? (
    <span className="text-[11px] font-semibold tracking-[0.2em] text-bad-ink" title="Error: blocks payment">ERROR</span>
  ) : (
    <span className="text-[11px] font-semibold tracking-[0.2em] text-warn-ink" title="Warning: the approver must address it">WARN</span>
  );
}

export function Button({ variant = "primary", className = "", ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" }) {
  const look = {
    primary: "border-ink bg-ink text-canvas hover:bg-transparent hover:text-ink",
    ghost: "border-line-strong text-ink hover:border-ink",
    danger: "border-bad/60 text-bad-ink hover:border-bad-ink",
  }[variant];
  return (
    <button
      {...props}
      className={`border px-5 py-2.5 text-xs font-semibold tracking-[0.2em] uppercase transition-colors disabled:pointer-events-none disabled:opacity-40 ${look} ${className}`}
    />
  );
}

/** One sentence per line, like the CLI card; "$7,185.00" and "Ltd." never split. */
export function Sentences({ text }: { text: string }) {
  const sentences = text.trim().split(/(?<=[.!?])(?<!(?:Ltd|Inc|Co|Corp)\.)\s+(?=[A-Z0-9$])/).filter(Boolean);
  return sentences.length < 2 ? <p>{text}</p> : <Bullets items={sentences} />;
}

export function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="space-y-1.5">
      {items.map((s, i) => (
        <li key={i} className="grid grid-cols-[1rem_1fr]">
          <span aria-hidden className="text-faint">—</span>
          {s}
        </li>
      ))}
    </ul>
  );
}

const SPECIAL: Record<string, string> = { non_usd_currency: "Non-USD currency" };

export function humanize(code: string): string {
  if (SPECIAL[code]) return SPECIAL[code];
  const text = code.replaceAll("_", " ");
  return text[0].toUpperCase() + text.slice(1);
}

/** Elapsed time as mm:ss.s, e.g. 01:23.4 */
export function clock(ms: number): string {
  const total = Math.max(ms, 0) / 1000;
  const minutes = Math.floor(total / 60);
  return `${String(minutes).padStart(2, "0")}:${(total % 60).toFixed(1).padStart(4, "0")}`;
}
