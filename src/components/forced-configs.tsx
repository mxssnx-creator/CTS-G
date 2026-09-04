import { useEffect, useState } from "react";
import { fetchHistCalc, startHistCalc, type ForcedConfigSummary, type HistCalcJob } from "@/lib/hist-calc";

export function ForcedConfigsPanel({ live }: { live?: ForcedConfigSummary }) {
  const [job, setJob] = useState<HistCalcJob | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const next = await fetchHistCalc();
      if (stopped) return;
      setJob(next);
      const running = ["queued", "fetch", "replay", "score"].includes(next.phase);
      timer = setTimeout(poll, running ? 1500 : 15000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, []);
  const start = async () => {
    setPending(true);
    setError("");
    try {
      const next = await startHistCalc({ forcedOnly: true, hours: 24, allSymbols: false });
      setJob(next);
      if (next.ok === false || next.phase === "error") setError(next.error || next.detail);
    } catch {
      setError("Calculation could not be started. No execution settings changed.");
    } finally { setPending(false); }
  };
  const data = job?.forcedConfigs || live;
  const rows = data?.rows ?? [];
  const liveById = new Map((live?.rows ?? []).map(row => [row.id, row]));
  const busy = pending || ["queued", "fetch", "replay", "score"].includes(job?.phase ?? "");
  return (
    <section className="min-w-0 space-y-4 rounded-xl border border-border bg-bg2 p-4 sm:p-5" data-testid="forced-configs">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-medium">Forced best configs · XRP / BCH / SOL</h2>
          <p className="mt-1 text-xs text-muted">TP 0.40–0.80% · SL 0.10–0.50% · step 0.05% · up to 5 per symbol / indication</p>
        </div>
        <button type="button" disabled={busy} onClick={() => void start()} className="min-h-11 rounded-lg border border-border px-4 text-sm disabled:opacity-50">
          {busy ? "Calculating…" : "Run 24h baseline test"}
        </button>
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-2 font-mono text-xs">
        <span>{data?.completed ?? 0} / {data?.requested ?? 3888} combinations</span>
        <span>{data?.coveragePct ?? 0}% coverage</span>
        <span>{rows.length} selected</span>
        <span>Net classic PF &gt; 1.02</span>
        <span>{live?.trialMode ? "VST trial lane on" : "Live lane not enabled"}</span>
      </div>
      <p className="text-xs leading-relaxed text-muted">
        Baseline only: no Block, DCA, trailing or early-exit strategy. Fees are deducted. Each candidate must pass training,
        the later 30% holdout, recent windows and drawdown limits. Historical throughput is not live throughput.
        VST requires its own confirmed roundtrips; positive observations do not automatically enable mainnet.
      </p>
      {busy ? <p role="status" className="text-sm text-muted">{job?.detail || "Starting…"}</p> : null}
      {error || job?.error ? <p role="alert" className="text-sm text-danger">{error || job?.error}</p> : null}
      {!rows.length ? <p className="rounded-lg border border-border p-4 text-sm text-muted">No qualifying baseline configs yet. Unproven or failing combinations remain inactive.</p> : (
        <div className="max-h-[70vh] overflow-auto rounded-lg border border-border" tabIndex={0} role="region" aria-label="Forced config results">
          <table className="w-full whitespace-nowrap text-left text-xs">
            <caption className="sr-only">Every selected configuration, one row each. PF after configured replay costs.</caption>
            <thead className="sticky top-0 bg-bg2 text-muted"><tr>
              {["Symbol / type", "Side", "TP / SL %", "SL:TP", "Closes train / test", "Net PF / holdout", "CTS ratio", "Trades/h hist", "DD R", "VST closes / PF", "Status"].map(label => <th key={label} className="px-3 py-3 font-medium">{label}</th>)}
            </tr></thead>
            <tbody>{rows.map(row => {
              const observed = liveById.get(row.id);
              return <tr key={row.id} className="border-t border-border align-top" data-config-id={row.id}>
                <td className="px-3 py-3"><div>{row.symbol} · {row.indication} #{row.rank}</div><div className="mt-1 font-mono text-[10px] text-muted">{row.settingsKey} · {row.source}</div></td>
                <td className="px-3 py-3">{row.direction}</td>
                <td className="px-3 py-3 font-mono">{row.tpPct.toFixed(2)} / {row.slPct.toFixed(2)}</td>
                <td className="px-3 py-3 font-mono">{row.slRatio.toFixed(3)}</td>
                <td className="px-3 py-3">{row.trainN} / {row.holdoutN}</td>
                <td className="px-3 py-3 font-mono text-primary">{row.pf.toFixed(3)} / {row.holdoutPf.toFixed(3)}</td>
                <td className="px-3 py-3 font-mono">{row.costRatio.toFixed(3)}</td>
                <td className="px-3 py-3 font-mono">{row.tradesPerHour.toFixed(2)}</td>
                <td className="px-3 py-3 font-mono">{row.maxDrawdownR.toFixed(2)}</td>
                <td className="px-3 py-3 font-mono">{observed?.liveN ?? 0} / {observed?.liveN ? observed.livePf?.toFixed(3) : "—"}</td>
                <td className="px-3 py-3 text-muted">{observed?.liveStatus || "unvalidated"}<div>{observed?.measuredCosts ? "measured costs" : "no complete measured-cost proof"}</div></td>
              </tr>;
            })}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}
