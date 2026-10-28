import { useEffect, useState } from "react";
import type { RunDetail } from "../api";
import { clock } from "./ui";

type NodeState = "done" | "current" | "pending" | "skipped" | "error";

const TRACK = [
  { stage: "load", label: "Load" },
  { stage: "extract", label: "Extract" },
  { stage: "check_duplicate", label: "Duplicates" },
  { stage: "validate", label: "Validate" },
  { stage: "policy_gate", label: "Policy" },
  { stage: "approval", label: "Approval" },
];

// The last segment names how the run ended.
const FINAL: Record<string, { label: string; bar: string }> = {
  paid: { label: "Paid", bar: "bg-good" },
  rejected: { label: "Rejected", bar: "bg-bad" },
  needs_review: { label: "Review", bar: "bg-warn" },
  duplicate: { label: "Duplicate", bar: "bg-neutral" },
  failed: { label: "Failed", bar: "bg-serious" },
  processing: { label: "Settle", bar: "" },
};

const BAR: Record<NodeState, string> = {
  done: "bg-ink",
  current: "animate-pulse bg-ink/60",
  pending: "bg-line-strong",
  skipped: "bg-line",
  error: "bg-serious",
};

/** Pipeline stages as a segmented bar, one segment per stage, with the run's clock. */
export function StageTrack({ run, queued }: { run: RunDetail; queued: boolean }) {
  const completed = new Set(run.events.filter((e) => e.type === "stage_completed").map((e) => e.stage));
  const failedAt = run.events.find((e) => e.type === "error")?.stage;
  const ended = run.status !== "processing";

  const nodes: { label: string; state: NodeState }[] = TRACK.map(({ stage, label }) => ({
    label,
    state: completed.has(stage)
      ? "done"
      : stage === failedAt
        ? "error"
        : !ended && !queued && run.stage === stage
          ? "current"
          : ended
            ? "skipped"
            : "pending",
  }));
  const settling = !ended && (run.stage === "payment" || run.stage === "rejection");
  const final = FINAL[run.status];
  nodes.push({ label: final.label, state: ended ? "done" : settling ? "current" : "pending" });

  return (
    <div className="flex items-start gap-6">
      <ol className="flex min-w-0 flex-1 gap-1" aria-label="Pipeline progress">
        {nodes.map((node, i) => {
          const last = i === nodes.length - 1;
          const lit = node.state === "done" || node.state === "current";
          return (
            <li key={node.label} className="min-w-0 flex-1" aria-current={node.state === "current" ? "step" : undefined}>
              <span className={`block h-0.5 ${last && ended ? final.bar : BAR[node.state]}`} />
              <span
                className={`mt-2.5 block truncate pl-[0.18em] text-center text-[10px] font-medium tracking-[0.18em] uppercase ${lit ? "text-ink" : node.state === "error" ? "text-serious-ink" : "text-faint"} ${node.state === "current" || last ? "" : "max-sm:invisible"}`}
              >
                {node.label}
              </span>
            </li>
          );
        })}
      </ol>
      <Clock run={run} queued={queued} />
    </div>
  );
}

function Clock({ run, queued }: { run: RunDetail; queued: boolean }) {
  const live = run.status === "processing" && !queued;
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(timer);
  }, [live]);
  const start = run.events.length ? Date.parse(run.events[0].ts) : Date.parse(run.started_at);
  const elapsed = queued ? 0 : live ? now - start : run.duration_ms;
  return (
    <div className="-mt-1.5 shrink-0 text-right">
      <div className={`font-mono text-sm tabular-nums ${live ? "text-ink" : "text-muted"}`}>{clock(elapsed)}</div>
      <div className="eyebrow mt-0.5 text-[10px]">{queued ? "Queued" : live ? "Running" : "Runtime"}</div>
    </div>
  );
}
