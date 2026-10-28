import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type DragEvent } from "react";
import { api, money, STAGE_ACTIVE, type RunStatus, type RunSummary } from "../api";
import { Spinner, StatusTag } from "./ui";

const TABS: { label: string; status: RunStatus | null }[] = [
  { label: "All", status: null },
  { label: "Review", status: "needs_review" },
  { label: "Paid", status: "paid" },
  { label: "Rejected", status: "rejected" },
];

const SUPPORTED = [".txt", ".json", ".csv", ".xml", ".pdf"];

interface Props {
  runs: RunSummary[] | undefined;
  selected: number | null;
  onSelect: (id: number) => void;
}

export function Sidebar({ runs, selected, onSelect }: Props) {
  const [tab, setTab] = useState<RunStatus | null>(null);
  const [dragging, setDragging] = useState(false);
  const queryClient = useQueryClient();

  const start = useMutation({
    mutationFn: async (sources: ({ sample: string } | { file: File })[]) => {
      const started: RunSummary[] = [];
      for (const source of sources) started.push(await api.start(source));  // in order, so duplicates are judged in order
      return started;
    },
    onSuccess: (started) => {
      for (const key of ["invoices", "metrics", "samples"]) queryClient.invalidateQueries({ queryKey: [key] });
      if (started.length) onSelect(started[0].id);
    },
  });

  const upload = (files: FileList | File[]) => {
    const accepted = [...files].filter((f) => SUPPORTED.some((s) => f.name.toLowerCase().endsWith(s)));
    if (accepted.length) start.mutate(accepted.map((file) => ({ file })));
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    upload(e.dataTransfer.files);
  };

  const visible = (runs ?? []).filter((r) => tab === null || r.status === tab);

  return (
    <aside
      className="relative flex min-h-0 flex-col border-line md:border-r max-md:max-h-[55vh] max-md:border-b"
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false); }}
      onDrop={onDrop}
    >
      <div className="space-y-4 border-b border-line p-5">
        <AddInvoice busy={start.isPending} onStart={(sources) => start.mutate(sources)} onUpload={upload} />
        {start.error && <p className="text-xs text-bad-ink">{start.error.message}</p>}
        <nav className="flex justify-between gap-2" aria-label="Filter by status">
          {TABS.map((t) => {
            const count = (runs ?? []).filter((r) => t.status === null || r.status === t.status).length;
            const active = tab === t.status;
            return (
              <button
                key={t.label}
                onClick={() => setTab(t.status)}
                aria-pressed={active}
                className={`border-b pb-1.5 text-[11px] font-medium tracking-[0.18em] whitespace-nowrap uppercase transition-colors ${active ? "border-ink text-ink" : "border-transparent text-faint hover:text-ink"}`}
              >
                {t.label} <span className="tabular-nums">{count}</span>
              </button>
            );
          })}
        </nav>
      </div>

      <ul className="min-h-0 flex-1 overflow-y-auto">
        {runs && visible.length === 0 && (
          <li className="px-5 py-10 text-center text-sm text-faint">
            {runs.length === 0 ? "No invoices yet. Add one above, or drop files here." : "Nothing here."}
          </li>
        )}
        {visible.map((run) => {
          const queued = run.stage === "queued";
          const current = run.id === selected;
          return (
            <li key={run.id}>
              <button
                onClick={() => onSelect(run.id)}
                aria-current={current}
                className={`block w-full border-b border-line px-5 py-4 text-left transition-colors hover:bg-raised ${current ? "bg-raised shadow-[inset_2px_0_0_var(--color-ink)]" : ""}`}
              >
                <div className="flex items-center justify-between gap-3">
                  <span className="truncate text-[15px] font-medium tracking-wide">{run.invoice_number ?? run.file}</span>
                  <StatusTag status={run.status} queued={queued} />
                </div>
                {(run.invoice_number || run.total_cents != null) && (
                  <div className="mt-1 flex justify-between gap-3 text-sm text-muted">
                    <span className="truncate">{run.vendor ?? run.file}</span>
                    <span className="shrink-0 tabular-nums">{money(run.total_cents, run.currency)}</span>
                  </div>
                )}
                {queued ? (
                  <p className="mt-1 text-xs text-faint">Waiting for earlier invoices</p>
                ) : run.status === "processing" ? (
                  <p className="eyebrow mt-2 text-ink">{STAGE_ACTIVE[run.stage] ?? run.stage}…</p>
                ) : (
                  run.headline && <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-faint">{run.headline}</p>
                )}
              </button>
            </li>
          );
        })}
      </ul>

      {dragging && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center border border-dashed border-ink bg-canvas/90">
          <span className="eyebrow text-ink">Drop invoices to process</span>
        </div>
      )}
    </aside>
  );
}

function AddInvoice({ busy, onStart, onUpload }: { busy: boolean; onStart: (s: { sample: string }[]) => void; onUpload: (f: FileList) => void }) {
  const [open, setOpen] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const samples = useQuery({ queryKey: ["samples"], queryFn: api.samples, enabled: open });

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !menu.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", close); };
  }, [open]);

  const run = (ids: string[]) => { onStart(ids.map((sample) => ({ sample }))); setOpen(false); };
  const folders = Object.entries(Object.groupBy(samples.data ?? [], (s) => s.folder));

  return (
    <div className="relative" ref={menu}>
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        disabled={busy}
        className="flex w-full items-center justify-center gap-3 border border-ink px-4 py-3 text-xs font-semibold tracking-[0.25em] uppercase transition-colors hover:bg-ink hover:text-canvas disabled:opacity-50"
      >
        {busy ? <Spinner /> : <span aria-hidden className="text-base leading-none font-light">+</span>} Add invoice
      </button>
      <input ref={input} type="file" multiple accept={SUPPORTED.join(",")} className="hidden" onChange={(e) => { if (e.target.files) onUpload(e.target.files); e.target.value = ""; setOpen(false); }} />
      {open && (
        <div className="absolute inset-x-0 top-full z-20 mt-2 max-h-[70vh] overflow-y-auto border border-line-strong bg-canvas shadow-2xl shadow-black">
          <button onClick={() => input.current?.click()} className="w-full px-4 py-3 text-left hover:bg-raised">
            <span className="eyebrow text-ink">Upload files</span>
            <span className="mt-1 block text-xs text-faint">PDF, TXT, JSON, CSV, XML. Or drop them on the list.</span>
          </button>
          {samples.isLoading && <p className="px-4 py-3 text-sm text-faint">Loading samples…</p>}
          {folders.map(([folder, items]) => (
            <div key={folder} className="border-t border-line py-1">
              <div className="flex items-center justify-between px-4 py-2">
                <span className="eyebrow">data/{folder}</span>
                {items!.length > 1 && (
                  <button onClick={() => run(items!.map((s) => s.id))} className="text-[11px] font-semibold tracking-[0.2em] text-ink uppercase hover:underline">
                    Run all {items!.length}
                  </button>
                )}
              </div>
              {items!.map((s) => (
                <button key={s.id} onClick={() => run([s.id])} className="flex w-full items-center justify-between px-4 py-1.5 text-left font-mono text-xs text-muted hover:bg-raised hover:text-ink">
                  {s.name}
                  {s.processed && <span className="text-faint" title="Already processed">✓</span>}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
