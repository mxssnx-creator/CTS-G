import type { LiveStats } from "@/lib/live-stats";
import { evidencePfText, feeRows } from "@/lib/evidence-view";

export function CostEvidencePanel({ stats }: { stats: LiveStats | null }) {
  const model = stats?.costModel;
  const rows = feeRows(model);
  return <section className="rounded-xl border border-border bg-surface p-4" data-testid="cost-evidence">
    <h3 className="text-sm font-medium">Execution costs · reported model</h3>
    <p className="mt-1 text-xs text-muted">
      Paper allowance {model?.paperCostPct != null ? `${model.paperCostPct}%` : "not reported"} · round-trip slippage allowance {model?.slippageAllowancePct != null ? `${model.slippageAllowancePct}%` : "not reported"}.
      Actual fill prices already include slippage. Each leg uses its own notional and reported fee when available; LIMIT does not prove maker liquidity.
    </p>
    {model?.error ? <p role="status" className="mt-2 text-sm text-warn">{model.error}</p> : null}
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-left text-xs">
        <caption className="sr-only">Per-symbol fee rates and their provenance; rates alone are not verified fill fees</caption>
        <thead><tr>{["Symbol", "Maker / leg", "Taker / leg", "Rate source"].map(label => <th scope="col" key={label} className="p-2 font-medium">{label}</th>)}</tr></thead>
        <tbody>{rows.length ? rows.map(row => <tr key={row.symbol} className="border-t border-border">
          <th scope="row" className="p-2 font-medium">{row.symbol}</th><td className="p-2">{row.maker}</td><td className="p-2">{row.taker}</td><td className="p-2">{row.source}</td>
        </tr>) : <tr><td colSpan={4} className="p-2 text-muted">No per-symbol rate evidence for this connection. Configured estimates are not venue-verified fees.</td></tr>}</tbody>
      </table>
    </div>
  </section>;
}

export function StageEvidencePanel({ stats }: { stats: LiveStats | null }) {
  const stages = stats?.sets?.pfBase?.stages;
  const windows = stats?.sets?.pfBase?.windows;
  return <section className="rounded-xl border border-border bg-surface p-4" data-testid="stage-evidence">
    <h3 className="text-sm font-medium">Base → Main → Real · paper evidence</h3>
    <p className="mt-1 text-xs text-muted">Selected historical windows, not executed orders or a profitability forecast. A paper close can appear in several Sets. No-loss and missing samples are not finite PFs.</p>
    <p className="mt-2 text-xs text-muted">Last-N mode: {stats?.sets?.lastNMode ?? "not reported"}. {stats?.sets?.independentLastNLanes === true ? "Independent range lanes reported." : "Simultaneous independent range lanes are not reported."} Context coverage: {stats?.sets?.stageContextFullSymbols ?? "—"} full / {stats?.sets?.stageContextShortSymbols ?? "—"} short symbols.</p>
    <div className="mt-3 overflow-x-auto"><table className="w-full min-w-xl text-left text-xs">
      <caption className="sr-only">Per-stage qualifying Sets, share of all Sets, window samples and net PF</caption>
      <thead><tr>{["Stage", "Qualified Sets", "% all Sets", "Window samples", "Stage paper closes", "Net PF"].map(label => <th scope="col" className="p-2 font-medium" key={label}>{label}</th>)}</tr></thead>
      <tbody>{(["base", "main", "real", "realNeg"] as const).map(key => {
        const row = stages?.[key];
        return <tr key={key} className="border-t border-border"><th scope="row" className="p-2">{key}</th><td className="p-2">{row?.sets ?? "—"}</td><td className="p-2">{row?.pct != null ? row.pct.toFixed(2) : "—"}</td><td className="p-2">{row?.n ?? "—"}</td><td className="p-2">{key === "base" ? "— (window only)" : row?.orders ?? "—"}</td><td className="p-2">{evidencePfText(row, row?.n)}</td></tr>;
      })}</tbody>
    </table></div>
    <details className="mt-3"><summary className="cursor-pointer text-sm">Each configured Last-N window</summary>
      <div className="overflow-x-auto"><table className="mt-2 w-full min-w-xl text-left text-xs">
        <caption className="sr-only">Windows evaluated independently within each stage; not separate funded range lanes</caption>
        <thead><tr>{["Window", "N", "State", "Passing / population", "% population", "Samples", "PF"].map(label => <th scope="col" className="p-2 font-medium" key={label}>{label}</th>)}</tr></thead>
        <tbody>{Object.entries(windows ?? {}).map(([key, row]) => <tr key={key} className="border-t border-border"><th scope="row" className="p-2">{key}</th><td className="p-2">{Array.isArray(row.n) ? row.n.join(", ") : row.n ?? "—"}</td><td className="p-2">{row.on ? "on" : "off"}</td><td className="p-2">{row.sets} / {row.pop}</td><td className="p-2">{row.pct.toFixed(2)}</td><td className="p-2">{row.orders}</td><td className="p-2">{!row.on ? "Disabled" : row.role === "coordination" ? "Sign condition, not PF" : evidencePfText(row, row.orders)}</td></tr>)}</tbody>
      </table></div>
    </details>
  </section>;
}
