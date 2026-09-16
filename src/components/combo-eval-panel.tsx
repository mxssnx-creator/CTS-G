export type ComboStat = {
  pf?: number;
  n?: number;
  wr?: number;
  evalN?: number;
  validated?: boolean;
  maxDdS?: number;
  avgDdS?: number;
};

export type ComboEvalBlob = {
  pfStats?: Record<string, ComboStat>;
  withWithout?: Record<string, { with?: ComboStat; without?: ComboStat }>;
  comboMatrix?: Array<{ indication: string; strategy: string; n?: number; pf?: number; wr?: number; evalN?: number; validated?: boolean }>;
  successfulConfigs?: Array<{
    indication?: string;
    config?: string;
    strategy?: string;
    setId?: string;
    pf?: number;
    n?: number;
    wr?: number;
    validated?: boolean;
    slRatio?: number;
    step?: number;
    trailKey?: string;
  }>;
  combo?: { engine?: string; journal?: string; cells?: number; successfulCount?: number; validatedCount?: number };
};

const PF_KEYS = ["overall", "normal", "trailing", "axis", "block", "dca"] as const;
const STRATEGIES = ["normal", "trailing", "axis", "block", "dca"] as const;

function pfTone(stat?: ComboStat) {
  return stat?.validated ? "text-primary" : "text-muted";
}

export function ComboEvalPanel({
  job,
  compact = false,
}: {
  job?: ComboEvalBlob | null;
  compact?: boolean;
}) {
  if (!job) return null;
  const pfStats = job.pfStats || {};
  const withWithout = job.withWithout || {};
  const matrix = job.comboMatrix || [];
  const successful = job.successfulConfigs || [];
  const hasPf = Object.keys(pfStats).length > 0;
  const hasWw = Object.keys(withWithout).length > 0;
  const hasMatrix = matrix.length > 0;
  const hasSuccessful = successful.length > 0;
  if (!hasPf && !hasWw && !hasMatrix && !hasSuccessful) return null;
  const indications = Array.from(new Set(matrix.map((c) => c.indication)));
  const maxRows = compact ? 12 : 40;

  return (
    <div className="space-y-3" data-testid="combo-eval-panel">
      {hasPf ? (
        <section data-testid="combo-pf-families">
          <h3 className="mb-2 text-sm font-medium">PF families · overall / normal / trailing / axis / block / DCA</h3>
          <p className="mb-2 text-xs text-muted">Each family is scored from its own fills. PFs are never averaged across sets.</p>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-6">
            {PF_KEYS.map((key) => {
              const v = pfStats[key];
              return (
                <article key={key} className="rounded-lg border border-border bg-bg2 px-3 py-2">
                  <p className="font-mono text-[10px] uppercase tracking-wide text-muted">{key}</p>
                  <p className={`mt-0.5 font-mono text-lg tabular-nums ${pfTone(v)}`}>{(v?.pf ?? 0).toFixed(3)}</p>
                  <p className="font-mono text-[10px] text-muted">{v?.n ?? 0} fills · WR {(v?.wr ?? 0).toFixed(1)}%</p>
                  <p className="font-mono text-[10px] text-muted">DDT {Math.round(Number(v?.maxDdS || 0))}s</p>
                </article>
              );
            })}
          </div>
        </section>
      ) : null}

      {hasWw ? (
        <section className="grid gap-3 lg:grid-cols-2" data-testid="combo-with-without">
          {(["block", "dca"] as const).map((key) => {
            const pair = withWithout[key];
            return (
              <section key={key} className="rounded-lg border border-border bg-bg2 p-3">
                <h3 className="mb-2 text-sm font-medium">With / without {key === "dca" ? "DCA" : "Block"}</h3>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="font-mono text-[10px] uppercase tracking-wide text-muted">
                      <th className="py-1 text-left">Book</th>
                      <th className="text-left">PF</th>
                      <th className="text-left">Fills</th>
                      <th className="text-left">WR</th>
                      <th className="text-left">DDT</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(["with", "without"] as const).map((side) => {
                      const v = pair?.[side];
                      return (
                        <tr key={side} className="border-t border-border/60">
                          <td className="py-1.5 capitalize">{side}</td>
                          <td className={`font-mono ${pfTone(v)}`}>{(v?.pf ?? 0).toFixed(3)}</td>
                          <td className="font-mono">{v?.n ?? 0}</td>
                          <td className="font-mono">{(v?.wr ?? 0).toFixed(1)}%</td>
                          <td className="font-mono">{Math.round(Number(v?.maxDdS || 0))}s</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </section>
            );
          })}
        </section>
      ) : null}

      {hasMatrix ? (
        <section className="overflow-auto rounded-lg border border-border bg-bg2 p-3" data-testid="combo-matrix">
          <h3 className="mb-2 text-sm font-medium">Indication × strategy · independent last-N PF</h3>
          <p className="mb-2 text-xs text-muted">Every cell is its own book. Empty cells stay at PF 1.00 with n=0 rather than inheriting another lane.</p>
          <table className="w-max min-w-full text-center font-mono text-[11px]">
            <thead>
              <tr>
                <th className="px-1 py-1 text-left text-muted">Indication</th>
                {STRATEGIES.map((s) => (
                  <th key={s} className="px-2 py-1 text-muted">{s}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {indications.map((indication) => (
                <tr key={indication}>
                  <th className="px-1 py-1 text-left font-normal text-muted">{indication}</th>
                  {STRATEGIES.map((strategy) => {
                    const cell = matrix.find((c) => c.indication === indication && c.strategy === strategy);
                    const n = cell?.n ?? 0;
                    return (
                      <td key={strategy} className={cell?.validated ? "text-primary" : "text-muted"} title={`${indication} × ${strategy} n=${n} ddt=${Math.round(Number(cell?.maxDdS || 0))}s`}>
                        {n ? `${Number(cell?.pf || 0).toFixed(2)}/${Math.round(Number(cell?.maxDdS || 0))}s` : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {job.combo?.engine ? (
            <p className="mt-2 font-mono text-[10px] text-muted">
              {job.combo.engine} · {job.combo.cells ?? 0} combos · {job.combo.successfulCount ?? successful.length} successful
            </p>
          ) : null}
        </section>
      ) : null}

      {hasSuccessful ? (
        <section className="rounded-lg border border-border bg-bg2 p-3" data-testid="successful-configs">
          <h3 className="mb-2 text-sm font-medium">Successful configs · used for subsequent calcs</h3>
          <p className="mb-2 text-xs text-muted">Only combinations that cleared the historic PF floor. Each row is one indication + type + config + strategy set.</p>
          <table className="w-full text-sm">
            <thead>
              <tr className="font-mono text-[10px] uppercase tracking-wide text-muted">
                {["Indication", "Strategy", "Config", "Set", "PF", "N", "WR"].map((h) => (
                  <th key={h} className="py-1 text-left">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {successful.slice(0, maxRows).map((row, i) => (
                <tr key={row.setId || i} className="border-t border-border/60">
                  <td className="py-1.5">{row.indication || "—"}</td>
                  <td>{row.strategy || "—"}</td>
                  <td className="font-mono text-xs">{row.config || "—"}</td>
                  <td className="font-mono text-xs">{row.setId || "—"}</td>
                  <td className="font-mono text-primary">{(row.pf ?? 0).toFixed(3)}</td>
                  <td className="font-mono">{row.n ?? 0}</td>
                  <td className="font-mono">{(row.wr ?? 0).toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  );
}
