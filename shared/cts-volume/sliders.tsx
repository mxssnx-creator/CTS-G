"use client";
import React from "react";
import {
  DEFAULT_RELATIONAL_VOLUME,
  relationalVolumeSettings,
  type VolumeBucket,
  type RelationalVolumeRatios,
} from "./ratios.ts";
function distinctVolumeKeys(keys: readonly string[], values: Record<string, number>) {
  const labels = new Map<string, string>();
  for (const key of [...keys, ...Object.keys(values)]) {
    const id = key.toLowerCase();
    if (!labels.has(id)) labels.set(id, key);
  }
  return [...labels.entries()];
}
export function VolumeRatioSliders({
  value,
  onChange,
  buckets,
  overall = false,
}: {
  value: unknown;
  onChange: (value: RelationalVolumeRatios) => void;
  buckets: Partial<Record<VolumeBucket, readonly string[]>>;
  overall?: boolean;
}) {
  const s = relationalVolumeSettings(value);
  const slider = (
    label: string,
    value: number,
    set: (value: number) => void,
    min = 0.1,
    max = 5,
  ) => (
    <label
      key={label}
      style={{
        display: "grid",
        gap: 6,
        fontSize: 12,
        padding: 8,
        border: "1px solid color-mix(in srgb,currentColor 16%,transparent)",
        borderRadius: 8,
      }}
    >
      <span style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <span>{label}</span>
        <output>{value.toFixed(1)}×</output>
      </span>
      <input
        type="range"
        aria-label={label + " volume ratio"}
        min={min}
        max={max}
        step={0.1}
        value={value}
        onChange={(e) => set(Number(e.target.value))}
        style={{ width: "100%", accentColor: "var(--color-primary,#38bdf8)" }}
      />
    </label>
  );
  return (
    <div data-testid="relational-volume-ratios" style={{ display: "grid", gap: 12 }}>
      {overall && (
        <div style={{ display: "grid", gap: 10 }}>
          <label style={{ fontSize: 12 }}>
            <input
              type="checkbox"
              checked={s.enabled}
              onChange={(e) => onChange({ ...s, enabled: e.target.checked })}
            />{" "}
            Relational volume weights enabled
          </label>
          {slider("Overall", s.overall, (overall) => onChange({ ...s, overall }))}
          {slider(
            "Combined weight ceiling",
            s.maxMultiplier,
            (maxMultiplier) => onChange({ ...s, maxMultiplier }),
            1,
            25,
          )}
          <button
            type="button"
            onClick={() => onChange(structuredClone(DEFAULT_RELATIONAL_VOLUME))}
          >
            Equalize all weights · 1.0×
          </button>
        </div>
      )}
      {Object.entries(buckets).map(([name, keys]) => {
        const bucket = name as VolumeBucket;
        return (
          <fieldset key={bucket} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
            <legend style={{ fontSize: 12, marginBottom: 8 }}>
              {
                {
                  indicationTypes: "Indication types",
                  indications: "Individual indications",
                  strategies: "Strategy types",
                  ranges: "Coordinations / Ranges",
                  symbols: "Symbols",
                }[bucket]
              }
            </legend>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,170px),1fr))",
                gap: 8,
              }}
            >
              {distinctVolumeKeys(keys ?? [], s[bucket]).map(([id, key]) =>
                slider(
                  {
                    indicationTypes: "Indication type",
                    indications: "Indication",
                    strategies: "Strategy",
                    ranges: "Range",
                    symbols: "Symbol",
                  }[bucket] +
                    " · " +
                    key,
                  s[bucket][id] ?? 1,
                  (value) => onChange({ ...s, [bucket]: { ...s[bucket], [id]: value } }),
                ),
              )}
            </div>
          </fieldset>
        );
      })}
      <p style={{ fontSize: 11, opacity: 0.75, margin: 0 }}>
        1.0× keeps the existing base size. Matching weights multiply once before venue rounding and
        existing size limits. Existing fills keep their size; additional legs inherit parent
        source/range sizing. Qualification uses unit returns after costs.
      </p>
    </div>
  );
}
