import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api";
import { Detail } from "./components/Detail";
import { Overview } from "./components/Overview";
import { Sidebar } from "./components/Sidebar";
import { Spinner } from "./components/ui";

// Poll fast while anything is processing; slowly otherwise, to pick up runs started from the CLI.
const FAST = 1000;
const SLOW = 5000;

function selectedFromUrl(): number | null {
  const id = Number(new URLSearchParams(location.search).get("invoice"));
  return Number.isInteger(id) && id > 0 ? id : null;
}

export default function App() {
  const [selected, setSelected] = useState(selectedFromUrl);

  useEffect(() => {
    const onPop = () => setSelected(selectedFromUrl());
    addEventListener("popstate", onPop);
    return () => removeEventListener("popstate", onPop);
  }, []);

  const select = (id: number | null) => {
    if (id === selected) return;
    history.pushState(null, "", id ? `?invoice=${id}` : location.pathname);
    setSelected(id);
  };

  const runs = useQuery({
    queryKey: ["invoices"],
    queryFn: api.invoices,
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "processing") ? FAST : SLOW),
  });
  const busy = runs.data?.some((r) => r.status === "processing") ?? false;
  const metrics = useQuery({ queryKey: ["metrics"], queryFn: api.metrics, refetchInterval: busy ? FAST : SLOW });
  const detail = useQuery({
    queryKey: ["invoice", selected],
    queryFn: () => api.invoice(selected!),
    enabled: selected !== null,
    retry: false,
    refetchInterval: (q) => (q.state.data?.status === "processing" ? FAST : SLOW),
  });

  return (
    <div className="mx-auto flex max-w-[1800px] flex-col md:h-dvh">
      <header className="flex items-center justify-between gap-4 border-b border-line px-5 py-4 sm:px-8">
        <button onClick={() => select(null)} className="flex items-baseline gap-4 text-left">
          <h1 className="text-lg font-semibold tracking-[0.35em] uppercase">Acme</h1>
          <span className="eyebrow max-sm:hidden">Invoice operations</span>
        </button>
        {runs.error && <p className="eyebrow text-bad-ink">Can't reach the API: {runs.error.message}</p>}
      </header>
      <main className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)] md:grid-cols-[24rem_minmax(0,1fr)]">
        <Sidebar runs={runs.data} selected={selected} onSelect={select} />
        <section className="flex min-h-0 flex-col overflow-hidden max-md:min-h-[60vh]">
          {selected === null ? (
            <div className="overflow-y-auto"><Overview metrics={metrics.data} /></div>
          ) : detail.data ? (
            <Detail key={selected} run={detail.data} onSelect={select} />
          ) : detail.error ? (
            <div className="px-8 py-10">
              <p className="eyebrow text-bad-ink">{detail.error.message}</p>
              <button onClick={() => select(null)} className="eyebrow mt-4 text-ink hover:underline">Back to overview</button>
            </div>
          ) : (
            <div className="grid flex-1 place-items-center text-muted"><Spinner className="size-5" /></div>
          )}
        </section>
      </main>
    </div>
  );
}
