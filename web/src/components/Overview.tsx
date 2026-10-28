import { ResponsiveContainer, Sankey, Tooltip } from "recharts";
import { STATUS_LABEL, type FlowPath, type Metrics, type RunFindings, type Severity } from "../api";
import { humanize, Section, StatusTag } from "./ui";

// rank orders nodes top to bottom inside a column, so paths never cross. Routing steps stay neutral;
// the five outcomes wear their reserved status colors.
const STEP = "var(--color-ink)";
const NODES: Record<string, { label: string; color: string; rank: number; outcome?: true }> = {
  received: { label: "Received", color: STEP, rank: 0 },
  agents: { label: "VP + critic agents", color: STEP, rank: 1 },
  human: { label: "Human review", color: STEP, rank: 1.5 },  // below the agents' own decisions, above the rules
  rules_review: { label: "Rules: needs a human", color: STEP, rank: 2 },
  rules_reject: { label: "Rules: reject", color: STEP, rank: 3 },
  seen_before: { label: "Seen before", color: STEP, rank: 4 },
  paid: { label: STATUS_LABEL.paid, color: "var(--color-good)", rank: 10, outcome: true },
  needs_review: { label: STATUS_LABEL.needs_review, color: "var(--color-warn)", rank: 11, outcome: true },
  rejected: { label: STATUS_LABEL.rejected, color: "var(--color-bad)", rank: 12, outcome: true },
  duplicate: { label: STATUS_LABEL.duplicate, color: "var(--color-neutral)", rank: 13, outcome: true },
  failed: { label: STATUS_LABEL.failed, color: "var(--color-serious)", rank: 14, outcome: true },
};
const COLUMNS = 4;  // received → route → human review → outcome
const NODE_WIDTH = 3;

// An id this page doesn't know (a newer server) still draws, as a neutral step.
const known = (id: string) => NODES[id] ?? { label: humanize(id), color: STEP, rank: 5 };

// A path that skips a column crosses it on an unlabeled pass-through node, "~prev>next#column", so
// every link joins neighboring columns and Recharts never draws one through another column's node.
const PASS = /^~(.+)>(.+)#\d$/;
const ends = (id: string) => {
  const match = PASS.exec(id);
  return match ? { prev: match[1], next: match[2] } : null;
};
const node = (id: string) => {
  const pass = ends(id);
  if (!pass) return known(id);
  const prev = known(pass.prev);
  const next = known(pass.next);
  return { ...next, rank: pass.prev === "received" ? next.rank : prev.rank, pass: true };
};
// How a link end reads in a tooltip: a pass-through stands for the real step on its far side.
const endLabel = (id: string, side: "source" | "target") => {
  const pass = ends(id);
  return known(pass ? pass[side === "source" ? "prev" : "next"] : id).label;
};

const SEVERITY_DOT: Record<Severity, string> = { error: "bg-bad", warning: "bg-warn" };

interface Props {
  metrics: Metrics | undefined;
  runCount: number;
  onSelect: (id: number) => void;
}

export function Overview({ metrics, runCount, onSelect }: Props) {
  if (!metrics) return null;
  if (runCount === 0) {
    return (
      <div className="grid min-h-[60vh] place-items-center px-6 py-16 text-center">
        <div className="max-w-md">
          <p className="eyebrow">Standing by</p>
          <h2 className="mt-4 text-4xl font-light tracking-wide">No invoices yet</h2>
          <p className="mt-4 leading-relaxed text-muted">Add a file or run the samples to begin.</p>
        </div>
      </div>
    );
  }
  if (!metrics.runs.length) {
    return <p className="px-5 py-10 text-sm text-faint sm:px-8">The first invoice is still being processed.</p>;
  }
  return (
    <div className="max-w-6xl space-y-16 px-5 py-8 sm:px-8">
      <Section title="How each invoice was decided" aside={<span className="eyebrow">{metrics.runs.length} finished</span>}>
        <Flow paths={metrics.flow} />
      </Section>
      <Section
        title="What the checks found"
        aside={
          <span className="flex gap-5">
            {(["error", "warning"] as const).map((s) => (
              <span key={s} className="eyebrow flex items-center gap-2 whitespace-nowrap">
                <span aria-hidden className={`size-1.5 rounded-full ${SEVERITY_DOT[s]}`} /> {s === "error" ? "Error" : "Warning"}
              </span>
            ))}
          </span>
        }
      >
        <FindingGrid runs={metrics.runs} onSelect={onSelect} />
      </Section>
    </div>
  );
}

function sankeyData(paths: FlowPath[]) {
  const links = new Map<string, number>();
  for (const { steps, value } of paths) {
    const route = steps.slice(0, -1);
    const end = steps[steps.length - 1];
    const prev = route[route.length - 1];
    while (route.length < COLUMNS - 1) route.push(`~${prev}>${end}#${route.length}`);
    const padded = [...route, end];
    for (let i = 1; i < padded.length; i++) {
      const key = `${padded[i - 1]}\n${padded[i]}`;
      links.set(key, (links.get(key) ?? 0) + value);
    }
  }
  const pairs = [...links].map(([key, value]) => ({ ends: key.split("\n"), value }));
  const ids = [...new Set(pairs.flatMap((p) => p.ends))].sort((a, b) => node(a).rank - node(b).rank);
  return {
    nodes: ids.map((name) => ({ name })),
    links: pairs.map(({ ends: [source, target], value }) => ({ source: ids.indexOf(source), target: ids.indexOf(target), value })),
  };
}

interface FlowNode {
  name: string;
  value: number;
}

/** Where finished invoices went: who decided (rules, agents, a human) and how each one ended. */
function Flow({ paths }: { paths: FlowPath[] }) {
  const total = paths.reduce((n, p) => n + p.value, 0);
  const height = Math.min(380, 150 + total * 12);  // a handful of invoices shouldn't render as slabs
  const summary = paths.map((p) => `${p.steps.map((s) => known(s).label).join(" → ")}: ${p.value}`).join("; ");

  return (
    <div className="overflow-x-auto border-t border-line pt-4">
      <div className="min-w-[680px]" style={{ height }} role="img" aria-label={summary}>
        <ResponsiveContainer>
          <Sankey
            data={sankeyData(paths)}
            sort={false}
            nodeWidth={NODE_WIDTH}
            nodePadding={34}
            margin={{ top: 28, right: 130, bottom: 4, left: 0 }}
            node={FlowNodeMark}
            link={FlowLinkPath}
          >
            <Tooltip
              content={({ active, payload }) => (active && payload?.[0] ? <FlowTip name={String(payload[0].name)} value={Number(payload[0].value)} /> : null)}
            />
          </Sankey>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// Steps are labeled above the bar so a long name never runs into the next column; outcomes sit on the right edge.
function FlowNodeMark({ x, y, width, height, payload }: { x: number; y: number; width: number; height: number; payload: FlowNode }) {
  const n = node(payload.name);
  if ("pass" in n) return null;  // the link out of it starts early and covers the gap
  const outcome = "outcome" in n;
  return (
    <g>
      <rect x={x} y={y} width={width} height={Math.max(height, 2)} fill={n.color} />
      <text x={outcome ? x + width + 12 : x} y={outcome ? y + height / 2 : y - 10} dominantBaseline={outcome ? "middle" : "auto"}>
        <tspan className="fill-muted" fontSize={10} fontWeight={500} letterSpacing="0.18em">{n.label.toUpperCase()}</tspan>
        <tspan dx={8} fontSize={13} className="fill-ink tabular-nums">{payload.value}</tspan>
      </text>
    </g>
  );
}


interface LinkShape {
  sourceX: number;
  targetX: number;
  sourceY: number;
  targetY: number;
  sourceControlX: number;
  targetControlX: number;
  linkWidth: number;
  payload: { source: FlowNode; target: FlowNode };
}

function FlowLinkPath({ sourceX, targetX, sourceY, targetY, sourceControlX, targetControlX, linkWidth, payload }: LinkShape) {
  const target = node(payload.target.name);
  const startX = "pass" in node(payload.source.name) ? sourceX - NODE_WIDTH : sourceX;
  return (
    <path
      d={`M${startX},${sourceY} L${sourceX},${sourceY} C${sourceControlX},${sourceY} ${targetControlX},${targetY} ${targetX},${targetY}`}
      fill="none"
      stroke={target.color}
      strokeWidth={Math.max(linkWidth, 1)}
      // Flows into an outcome carry its color; flows between routing steps stay faint.
      className={`transition-[stroke-opacity] ${"outcome" in target ? "[stroke-opacity:0.3] hover:[stroke-opacity:0.5]" : "[stroke-opacity:0.1] hover:[stroke-opacity:0.25]"}`}
    />
  );
}

/** Recharts names a node by its id and a link "source - target". */
function FlowTip({ name, value }: { name: string; value: number }) {
  const [source, target] = name.split(" - ");
  const what = target ? `${endLabel(source, "source")} → ${endLabel(target, "target")}` : node(source).label;
  return (
    <div className="border border-line-strong bg-canvas px-3 py-2 text-xs">
      <span className="text-muted">{what}</span> · {value} invoice{value === 1 ? "" : "s"}
    </div>
  );
}

/** One row per finished invoice, one column per kind of finding: compare it against the expected outcome at a glance. */
function FindingGrid({ runs, onSelect }: { runs: RunFindings[]; onSelect: (id: number) => void }) {
  const all = runs.flatMap((r) => r.findings);
  const count = (code: string) => runs.filter((r) => r.findings.some((f) => f.code === code)).length;
  const codes = [...new Set(all.map((f) => f.code))].sort((a, b) => {
    const blocks = (code: string) => all.some((f) => f.code === code && f.severity === "error");
    return Number(blocks(b)) - Number(blocks(a)) || count(b) - count(a) || a.localeCompare(b);
  });

  return (
    <div className="overflow-x-auto border-t border-line">
      <table className="min-w-max border-collapse text-sm">
        <thead>
          <tr>
            <th className="eyebrow pt-6 pr-12 pb-3 text-left align-bottom font-medium">Invoice</th>
            <th className="eyebrow pt-6 pr-10 pb-3 text-left align-bottom font-medium">Outcome</th>
            {codes.map((code) => (
              <th key={code} className="w-10 pt-6 pb-3 align-bottom font-medium" title={`${count(code)} of ${runs.length} invoices`}>
                <span className="eyebrow inline-block rotate-180 text-[10px] whitespace-nowrap [writing-mode:vertical-rl]">{humanize(code)}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const stopped = run.status === "duplicate" || run.status === "failed";
            return (
              <tr key={run.id} onClick={() => onSelect(run.id)} className="group cursor-pointer border-t border-line transition-colors hover:bg-raised">
                <td className="py-2.5 pr-12">
                  <button onClick={() => onSelect(run.id)} className={`font-mono text-xs group-hover:text-ink ${stopped ? "text-faint" : "text-muted"}`}>
                    {run.file}
                  </button>
                </td>
                <td className="py-2.5 pr-10"><StatusTag status={run.status} className={stopped ? "opacity-60" : ""} /></td>
                {codes.map((code) => {
                  const hits = run.findings.filter((f) => f.code === code);
                  if (!hits.length) {
                    return <td key={code} className="text-center"><span aria-hidden className="inline-block size-0.5 rounded-full bg-line-strong align-middle" /></td>;
                  }
                  const severity: Severity = hits.some((f) => f.severity === "error") ? "error" : "warning";
                  return (
                    <td key={code} className="text-center" title={`${hits.length} × ${humanize(code)} (${severity})`}>
                      <span className="relative inline-block align-middle">
                        <span className={`block size-2 rounded-full ${SEVERITY_DOT[severity]}`} />
                        {hits.length > 1 && <span className="absolute -top-2 left-3 text-[10px] text-muted tabular-nums">{hits.length}</span>}
                      </span>
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
