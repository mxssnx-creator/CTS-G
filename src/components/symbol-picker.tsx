import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  DEFAULT_SYMBOL_COUNT,
  MAX_SYMBOLS,
  PULSE_SYMBOLS,
  SYMBOL_SORTS,
  capSymbols,
  coerceSymbolSort,
  rankSymbolRows,
  type SymbolSortId,
} from "@/lib/config-model";

export type UniverseRow = {
  symbol: string;
  last: number;
  quoteVolume: number;
  changePct: number;
  vol1h?: number;
  vol24h?: number;
  maxLeverage?: number;
  high?: number;
  low?: number;
};

export function SymbolPicker({
  selected,
  onChange,
  sort = "vol1h",
  onSortChange,
  dynamic = true,
  onDynamicChange,
  onCap,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
  sort?: string;
  onSortChange?: (next: SymbolSortId) => void;
  dynamic?: boolean;
  onDynamicChange?: (next: boolean) => void;
  onCap?: (n: number) => void;
}) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<UniverseRow[]>([]);
  const [updated, setUpdated] = useState<number | null>(null);
  const sortId = coerceSymbolSort(sort);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const r = await fetch("/universe.json", { cache: "no-store" });
        if (!r.ok) return;
        const j = await r.json();
        if (!alive) return;
        const list = (j.rows ?? j.symbols ?? []) as UniverseRow[];
        if (Array.isArray(list) && list.length) {
          setRows(list);
          setUpdated(j.updated ?? Date.now() / 1000);
        }
      } catch {
        /* keep last */
      }
    };
    pull();
    const id = setInterval(pull, 8000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const source = useMemo(
    () =>
      rows.length
        ? rows
        : PULSE_SYMBOLS.map((symbol) => ({
            symbol,
            last: 0,
            quoteVolume: 0,
            changePct: 0,
            vol1h: 0,
            vol24h: 0,
            maxLeverage: 0,
          })),
    [rows],
  );

  const ranked = useMemo(() => rankSymbolRows(source, sortId), [source, sortId]);

  const filtered = useMemo(() => {
    const s = q.trim().toUpperCase();
    if (!s) return ranked;
    return ranked.filter((r) => r.symbol.toUpperCase().includes(s));
  }, [q, ranked]);

  const unlimited = !MAX_SYMBOLS || MAX_SYMBOLS <= 0;
  const allOn = selected.includes("*") || selected.includes("ALL");
  const sel = new Set(selected);
  const atMax = !unlimited && !allOn && selected.length >= MAX_SYMBOLS;
  const sortMeta = SYMBOL_SORTS.find((s) => s.id === sortId);

  const toggle = (symbol: string) => {
    if (allOn) {
      onChange([symbol]);
      return;
    }
    if (sel.has(symbol)) onChange(selected.filter((x) => x !== symbol));
    else if (!atMax) onChange(capSymbols([...selected, symbol]));
  };

  const applyTop = (n: number) => {
    const take = ranked
      .filter((r) => !r.symbol.startsWith("NCCO") && !r.symbol.startsWith("NCS"))
      .slice(0, n)
      .map((r) => r.symbol);
    onChange(capSymbols(take));
    onCap?.(n);
  };

  const top = ranked[0];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-mono text-sm tabular-nums">
          <span className="text-fg">{allOn ? "all" : selected.length}</span>
          <span className="text-muted"> / {unlimited ? "unlimited" : MAX_SYMBOLS}</span>
          <span className="ml-2 text-muted">default {DEFAULT_SYMBOL_COUNT}</span>
        </p>
        <div className="flex flex-wrap gap-2">
          <Mini onClick={() => applyTop(DEFAULT_SYMBOL_COUNT)}>Default 12</Mini>
          <Mini onClick={() => applyTop(12)}>Top 12</Mini>
          <Mini onClick={() => applyTop(24)}>Top 24</Mini>
          <Mini onClick={() => applyTop(50)}>Top 50</Mini>
          <Mini onClick={() => onChange(["*"])}>All unlimited</Mini>
          <Mini onClick={() => onChange([])}>Clear</Mini>
        </div>
      </div>

      <div>
        <p className="mb-2 font-mono text-xs uppercase tracking-wide text-muted">Order by</p>
        <div className="flex flex-wrap gap-2">
          {SYMBOL_SORTS.map((s) => {
            const on = s.id === sortId;
            return (
              <button
                key={s.id}
                type="button"
                data-testid={`symbol-sort-${s.id}`}
                title={s.hint}
                onClick={() => onSortChange?.(s.id)}
                className={`min-h-11 rounded-lg border px-3 text-xs ${
                  on ? "border-primary bg-primary-dim/40 text-fg" : "border-border text-muted"
                }`}
              >
                {s.label}
              </button>
            );
          })}
        </div>
        <p className="mt-2 text-xs text-muted">
          Always ranked by exchange max leverage, then {sortMeta?.label ?? "Volatility 1H"}.{" "}
          {sortId === "vol1h" ? "Default is the most volatile last 1 hour among the highest-leverage contracts." : sortMeta?.hint}
        </p>
      </div>

      {onDynamicChange ? (
        <button
          type="button"
          data-testid="symbols-dynamic"
          onClick={() => onDynamicChange(!dynamic)}
          className="flex min-h-11 w-full items-center justify-between rounded-lg border border-border bg-bg2 px-3 text-left text-sm"
        >
          <span>
            Dynamic book
            <span className="ml-2 font-mono text-xs text-muted">
              {dynamic ? "live rank keeps the scan on current top names" : "frozen list"}
            </span>
          </span>
          <span className={`font-mono text-xs ${dynamic ? "text-primary" : "text-muted"}`}>{dynamic ? "ON" : "OFF"}</span>
        </button>
      ) : null}

      {selected.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {selected.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => toggle(s)}
              className="rounded-full border border-primary bg-primary-dim/30 px-2.5 py-1 font-mono text-xs text-fg"
            >
              {s.replace("-USDT", "")}
            </button>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted">
          {allOn
            ? "Every listed USDT-M swap is processed, ordered by max leverage then the criterion above."
            : `Select any set of USDT-M swaps. Cap ${unlimited ? "unlimited" : MAX_SYMBOLS}. Empty reverts to default 12 on save unless All is on.`}
        </p>
      )}

      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search universe"
        className="min-h-11 w-full rounded-lg border border-border bg-bg2 px-3 font-mono text-sm text-fg outline-none"
      />

      <div className="max-h-80 overflow-auto rounded-xl border border-border">
        <table className="w-full text-left text-sm">
          <thead className="sticky top-0 bg-surface font-mono text-xs text-muted">
            <tr>
              <th className="px-3 py-2 font-medium">Symbol</th>
              <th className="px-3 py-2 font-medium">Last</th>
              <th className="px-3 py-2 font-medium">1H vol</th>
              <th className="px-3 py-2 font-medium">24h %</th>
              <th className="px-3 py-2 font-medium">Max lev</th>
              <th className="px-3 py-2 font-medium">Quote vol</th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice(0, 200).map((r) => {
              const on = sel.has(r.symbol);
              return (
                <tr
                  key={r.symbol}
                  onClick={() => toggle(r.symbol)}
                  className={`cursor-pointer border-t border-border ${on ? "bg-primary-dim/25" : ""}`}
                >
                  <td className="px-3 py-2 font-medium">{r.symbol.replace("-USDT", "")}</td>
                  <td className="px-3 py-2 font-mono text-xs tabular-nums">{fmtPx(r.last)}</td>
                  <td className="px-3 py-2 font-mono text-xs tabular-nums text-primary">{fmtPct(r.vol1h)}</td>
                  <td
                    className={`px-3 py-2 font-mono text-xs tabular-nums ${
                      r.changePct >= 0 ? "text-primary" : "text-danger"
                    }`}
                  >
                    {r.changePct >= 0 ? "+" : ""}
                    {r.changePct.toFixed(2)}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs tabular-nums">{r.maxLeverage ? `${r.maxLeverage}x` : "—"}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted">{fmtVol(r.quoteVolume)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="font-mono text-xs text-faint" data-testid="symbol-rank-meta">
        {ranked.length} listed · lev then {sortMeta?.label ?? "1H vol"}
        {top
          ? ` · lead ${top.symbol.replace("-USDT", "")} ${top.maxLeverage ? `${top.maxLeverage}x` : ""} ${fmtPct(top.vol1h)}`
          : ""}
        {updated ? " · live book" : " · fallback list"}
      </p>
    </div>
  );
}

function Mini({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="min-h-11 rounded-lg border border-border px-3 text-xs text-muted"
    >
      {children}
    </button>
  );
}

function fmtPx(n: number) {
  if (!n) return "—";
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

function fmtVol(n: number) {
  if (!n) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return n.toFixed(0);
}

function fmtPct(n?: number) {
  if (!n) return "—";
  return `${n.toFixed(2)}%`;
}
