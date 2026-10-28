import { Bar, BarChart, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { STATUS_LABEL, type Metrics, type RunStatus } from "../api";
import { humanize, Section } from "./ui";

// Status marks use the reserved status colors; the axis label names each bar, so color is never alone.
const STATUS_FILL: Record<RunStatus, string> = {
  paid: "var(--color-good)",
  rejected: "var(--color-bad)",
  needs_review: "var(--color-warn)",
  duplicate: "var(--color-neutral)",
  failed: "var(--color-serious)",
  processing: "var(--color-ink)",
};
const ORDER: RunStatus[] = ["paid", "rejected", "needs_review", "duplicate", "failed", "processing"];

interface Datum {
  label: string;
  value: number;
  fill: string;
}

export function Overview({ metrics }: { metrics: Metrics | undefined }) {
  if (!metrics) return null;
  const outcomes: Datum[] = ORDER.filter((s) => metrics.by_status[s]).map((s) => ({ label: STATUS_LABEL[s], value: metrics.by_status[s]!, fill: STATUS_FILL[s] }));
  const findings: Datum[] = Object.entries(metrics.findings_by_code)
    .sort(([, a], [, b]) => b - a)
    .map(([code, value]) => ({ label: humanize(code), value, fill: "var(--color-ink)" }));

  if (!outcomes.length) {
    return (
      <div className="grid min-h-[60vh] place-items-center px-6 py-16 text-center">
        <div className="max-w-md">
          <p className="eyebrow">Standing by</p>
          <h2 className="mt-4 text-4xl font-light tracking-wide">No invoices yet</h2>
          <p className="mt-4 leading-relaxed text-muted">
            Add a file or run the samples. Each invoice goes through extraction, inventory checks, the policy gate, a VP and
            critic review, and payment.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="px-5 py-8 sm:px-8">
      <div className="grid gap-12 xl:grid-cols-2">
        <Section title="Outcomes">
          <HorizontalBars data={outcomes} unit="invoice" />
        </Section>
        <Section title="What the checks found">
          {findings.length ? <HorizontalBars data={findings} unit="invoice" /> : <p className="text-sm text-faint">No findings yet.</p>}
        </Section>
      </div>
    </div>
  );
}

function HorizontalBars({ data, unit }: { data: Datum[]; unit: string }) {
  return (
    <div className="border-t border-line pt-4" style={{ height: data.length * 34 + 24 }} role="img" aria-label={data.map((d) => `${d.label}: ${d.value}`).join(", ")}>
      <ResponsiveContainer>
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 40, bottom: 0, left: 0 }} barCategoryGap={10}>
          <XAxis type="number" hide allowDecimals={false} />
          <YAxis
            type="category"
            dataKey="label"
            width={170}
            tickLine={false}
            axisLine={false}
            tick={{ fill: "var(--color-muted)", fontSize: 11, letterSpacing: "0.15em", fontWeight: 500 }}
            tickFormatter={(v: string) => v.toUpperCase()}
          />
          <Tooltip
            cursor={{ fill: "var(--color-raised)" }}
            content={({ active, payload }) =>
              active && payload?.length ? (
                <div className="border border-line-strong bg-canvas px-3 py-2 text-xs">
                  <span className="text-muted">{payload[0].payload.label}</span> · {payload[0].payload.value} {unit}
                  {payload[0].payload.value === 1 ? "" : "s"}
                </div>
              ) : null
            }
          />
          <Bar dataKey="value" maxBarSize={10} radius={[0, 2, 2, 0]} isAnimationActive={false}>
            {data.map((d) => <Cell key={d.label} fill={d.fill} />)}
            <LabelList dataKey="value" position="right" fill="var(--color-ink)" fontSize={13} className="tabular-nums" />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
