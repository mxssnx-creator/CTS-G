#!/usr/bin/env python3
"""Add descriptive hourly evidence to an immutable six-hour account report.

Only existing raw and profile-derived caches are read. Numerical work is
bounded to one symbol; this command never replays prices, places orders or
changes an account or its recorded admission decisions. Each gate remains
symbol x own configuration x direction. Combined PF is a description of
weighted completed observations, never qualification for another scope.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
from pathlib import Path
import pickle
import sys
import textwrap
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_six_hour_audit as audit

STAGES = ("preBase", "Base", "Main", "Real", "admitted")
OPTIONAL_FIELDS = ("preparationHourly", "preparationBySymbol", "byIndicationHourly",
                   "byIndicationHourlyBySymbol", "byStrategyHourly", "byStrategyHourlyBySymbol",
                   "supplementalStageSnapshots", "htf", "augmentation")
SCOPE_NOTE = ("Hypothetical completed entry-cohort observations with configured Set alias weights. "
              "PF is descriptive; qualification uses only each symbol/configuration/direction's "
              "strictly earlier own closes. No hypothetical wallet or account cash is assigned.")


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def calculation_model_signature():
    """Checkpoint math identity, independent of output paths/lifetime waits."""
    functions = (mark_own_stages, own_dd_flags, own_ddt_prefix, own_snapshots,
                 family, hourly_categories, summarize_symbol, combine_stats,
                 combine_endpoints, merge_hourly, audit.build_causal_candidates,
                 closed_utc_hour_evidence, combined_htf_evidence,
                 audit.base_qualified_parent_uids, audit.derived_replay,
                 audit.canonical_stats, audit.stage_report, audit.account_model.stage_flags)
    book_methods = (audit.se.SetBook.load, audit.se.SetBook.eval_need,
                    audit.se.SetBook._stage_window_ns, audit.se.SetBook.ddt_window)
    return digest_json(dict(functions=[audit.physics_ast(fn) for fn in functions],
        bookMethods=[ast.dump(ast.parse(textwrap.dedent(inspect.getsource(fn))), include_attributes=False) for fn in book_methods],
        stages=STAGES, kinds=audit.INDICATION_KINDS,
        positionCost=sha_file(audit.ROOT / "server/pulse/position_cost.py")))


def anonymous_memory_gib():
    path = Path("/sys/fs/cgroup/memory.stat")
    if not path.exists():
        return None
    values = dict(line.split() for line in path.read_text().splitlines())
    return int(values["anon"]) / 1024 ** 3


def wait_for_headroom(max_anon_gib, profile, completed, requested, progress_path=None):
    """Release the prior symbol before permitting another full-tape builder."""
    audit.release_working_memory()
    waiting = False
    while max_anon_gib and (used := anonymous_memory_gib()) is not None and used > max_anon_gib:
        if not waiting:
            state = dict(phase="augmentation-waiting-for-memory", profile=profile,
                completedSymbols=completed, requestedSymbols=requested, anonymousMemoryGiB=round(used, 3),
                maxAnonymousMemoryBeforeBuilderGiB=max_anon_gib)
            print(json.dumps(state), flush=True)
            if progress_path:
                audit.atomic_json(progress_path, state)
        waiting = True
        time.sleep(5)
        audit.release_working_memory()
    if waiting:
        print(json.dumps(dict(phase="augmentation-memory-ready", profile=profile,
                              completedSymbols=completed)), flush=True)


def checkpoint_key(symbol, profile, source, overlay, model_sha, raw_sha, derived_sha):
    return digest_json(dict(symbol=symbol, profile=profile, source=source, overlay=overlay,
                            calculationModelSHA=model_sha, rawCacheSHA256=raw_sha, derivedCacheSHA256=derived_sha))


def closed_utc_hour_evidence(rows):
    """Count exact complete UTC 1h buckets, excluding either partial edge."""
    buckets = {}
    stamps = []
    for row in rows:
        stamp = int(row[0])
        stamps.append(stamp)
        buckets.setdefault(stamp // 3600000, []).append(stamp)
    complete = []
    for bucket, values in buckets.items():
        expected = list(range(bucket * 3600000, (bucket + 1) * 3600000, 60000))
        if sorted(values) == expected:
            complete.append(bucket)
    return dict(usableClosedHourlyBars=len(complete),
        dataSpanHours=(max(stamps) + 60000 - min(stamps)) / 3600000 if stamps else None,
        firstCompleteHourUtc=audit.utc(min(complete) * 3600) if complete else None,
        lastCompleteHourEndUtc=audit.utc((max(complete) + 1) * 3600) if complete else None)


def combined_htf_evidence(report, summaries):
    supplied = (report.get("coverage") or {}).get("htf") or {}
    values = [dict(symbol=summary["symbol"], **summary["htfSource"]) for summary in summaries if summary.get("htfSource") is not None]
    available = min(row["usableClosedHourlyBars"] for row in values) if len(values) == len(summaries) else None
    spans = {row["dataSpanHours"] for row in values}
    return dict(supplied, availableHourlyBars=available, usableClosedHourlyBars=available,
        dataSpanHours=next(iter(spans)) if len(spans) == 1 else None,
        sourceReportedDurationBasedAvailableHourlyBars=supplied.get("availableHourlyBars"),
        bySymbol=values, bucketCountMethod="Exact UTC 60-minute timestamp buckets; leading/trailing incomplete hours excluded.",
        evidenceNote="Hourly history is independent of 1m PF and admission. A 48-hour source span supplies 47 complete UTC hourly buckets when its edges are minute 08.")


def derived_signature(result, overlay, profile, data_dir):
    """Match the harness's cached derivation key without executing replay."""
    pieces = (profile + repr(result["signature"]) + json.dumps(overlay, sort_keys=True)
        + sha_file(Path(data_dir) / "funding" / (result["symbol"] + ".json"))
        + sha_file(audit.ROOT / "server/pulse/axis_grid.py")
        + sha_file(audit.ROOT / "server/pulse/indication_adjustment_grid.py")
        + audit.physics_ast(audit.derived_replay)
        + audit.physics_ast(audit.base_qualified_parent_uids))
    return hashlib.sha256(pieces.encode()).hexdigest()


def stage_masks(rows):
    return {"preBase": np.ones(len(rows["uid"]), dtype=bool), "Base": rows["base_ok"],
            "Main": rows["main_ok"], "Real": rows["real_ok"], "admitted": rows["admitted"]}


def mark_own_stages(rows, evidence, book):
    """Use precisely the harness's own strict-past windows, including DDT."""
    groups = rows["uid"].astype(np.int64) * 2 + (rows["side"] > 0)
    previous, first = evidence.lookup(groups, rows["entry"])
    flags = audit.account_model.stage_flags(evidence, previous, first, book.eval_need(),
                                           book._stage_window_ns(), book.stage_min_pf)
    flags["base_ok"] &= flags["r30"] > 1.02 + 1e-9
    flags["main_ok"] &= flags["base_ok"]
    flags["real_ok"] &= flags["main_ok"]
    rows.update(flags)
    rows["countBeforeEntry"] = np.where(previous >= 0, previous - first + 1, 0)
    dd_ok, _ = own_dd_flags(groups, previous, first, flags["base_ok"], evidence, book)
    rows["dd_ok"] = dd_ok
    rows["admitted"] = flags["real_ok"] & dd_ok
    return rows


def own_dd_flags(groups, previous, first, eligible, evidence, book, cache=None):
    cache = {} if cache is None else cache
    dd_ok = np.ones(len(previous), dtype=bool)
    maximum = float(book.max_dd_s)
    for index in np.flatnonzero(eligible):
        end, beginning = int(previous[index]), int(first[index])
        key = (int(groups[index]), end)
        if key not in cache:
            lo = max(beginning, end - int(book.ddt_window()) + 1)
            cache[key] = audit.account_model.ddt_max_s_fast(
                evidence.exit[lo:end + 1].astype(float) * audit.BAR,
                evidence.tiebreak[lo:end + 1], evidence.moves[lo:end + 1],
                audit.cost_as_frac(book.cost_pct))
        dd_ok[index] = cache[key] <= maximum + 1e-9
    return dd_ok, cache


def own_ddt_prefix(evidence, cost_pct):
    """Maximum observed elapsed drawdown per own group after each close.

    Episodes start at the first cost-net close below that own tape's prior
    peak and finish on recovery, or at its last completed close. No duration
    crosses a scope boundary. This is a full-tape diagnostic; configured
    admission DDT uses its own configured last-N window separately.
    """
    output = np.zeros(len(evidence.exit), dtype=np.int32)
    starts = evidence.first
    ends = np.append(starts[1:], len(evidence.exit))
    cost = audit.cost_as_frac(cost_pct)
    for lo, hi in zip(starts, ends):
        cumulative = peak = 0.0
        since = None
        longest = 0
        for index in range(int(lo), int(hi)):
            stamp = int(evidence.exit[index]) * audit.BAR
            cumulative += float(evidence.moves[index]) - cost
            if cumulative >= peak - 1e-12:
                if since is not None:
                    longest = max(longest, stamp - since)
                    since = None
                peak = max(peak, cumulative)
            elif since is None:
                since = stamp
            if since is not None:
                longest = max(longest, stamp - since)
            output[index] = longest
    return output


def own_snapshots(result, catalog, evidence, book, coverage):
    weights = np.asarray([row["mult"] for row in catalog], dtype=np.int64)
    groups = evidence.ug
    multiplicity = weights[groups // 2]
    prefix_ddt = own_ddt_prefix(evidence, book.cost_pct)
    requested = sum(row["requestedConfigs"] for row in result["coverage"]) * 2
    requested += int(coverage.get("requestedDescendantDirectionConfigs", 0))
    start = len(result["bars"]) - 360
    rows, dd_cache = [], {}
    windows = book._stage_window_ns()
    for bar in range(start - 360, start + 361, 60):
        previous, first = evidence.lookup(groups, np.full(len(groups), bar, dtype=np.int32))
        flags = audit.account_model.stage_flags(evidence, previous, first, book.eval_need(), windows, book.stage_min_pf)
        flags["base_ok"] &= flags["r30"] > 1.02 + 1e-9
        flags["main_ok"] &= flags["base_ok"]
        flags["real_ok"] &= flags["main_ok"]
        dd_ok, dd_cache = own_dd_flags(groups, previous, first, flags["base_ok"], evidence, book, dd_cache)
        flags["admitted"] = flags["real_ok"] & dd_ok
        observed = previous >= 0
        counts = np.where(observed, previous - first + 1, 0)
        pf_windows = {}
        for stage, window in zip(("Base", "Main", "Real"), windows):
            # Insufficient windows remain visible as descriptive evidence;
            # their false gate never becomes a positive admission.
            take = np.minimum(counts, window)
            hi = np.where(observed, previous + 1, 0)
            lo = np.where(observed, previous + 1 - take, 0)
            value = 1 + .1 * (evidence.cs[hi] - evidence.cs[lo]) / np.maximum(take, 1)
            available = observed & np.isfinite(value) & (take > 0)
            n = int(multiplicity[available].sum())
            pf_windows[stage] = dict(windowN=int(window), requiredSamples=int(book.eval_need() if stage == "Base" else window),
                sampledDirectionConfigs=n, pfMin=round(float(value[available].min()), 6) if n else None,
                pfMean=round(float(np.dot(value[available], multiplicity[available]) / n), 6) if n else None,
                pfMax=round(float(value[available].max()), 6) if n else None)
        qualification = (("Base", "base_ok"), ("Main", "main_ok"), ("Real", "real_ok"), ("admitted", "admitted"))
        rows.append(dict(t=audit.utc(result["startS"] + bar * audit.BAR),
            phase="preparation" if bar <= start else "execution",
            maxOwnObservedDdtS=int(prefix_ddt[previous[observed]].max(initial=0)),
            ddBlockedDirectionConfigs=int(multiplicity[flags["real_ok"] & ~dd_ok].sum()),
            qualifiedDirectionConfigs={stage: int(multiplicity[flags[key]].sum()) for stage, key in qualification},
            qualifiedSets={stage: int(weights[np.unique(groups[flags[key]] // 2)].sum()) for stage, key in qualification},
            requestedDirectionConfigs=requested, observedDirectionConfigs=int(multiplicity[observed].sum()),
            insufficientBaseSamples=int(multiplicity[observed & ~flags["ok30"]].sum()),
            unobservedDirectionConfigs=requested - int(multiplicity[observed].sum()), ownWindowPf=pf_windows))
    return rows


def family(meta):
    kind = meta["kind"]
    if kind in ("axis", "block"):
        return kind + ":" + str(meta.get("axisKey", "unknown"))
    if kind == "dca":
        return "dca:" + str(meta.get("relativeCount", "unknown"))
    return "trailing" if kind == "trail" else "normal"


def hourly_categories(catalog, rows, cost_pct, start, start_s, categories, category_of, field):
    """Summarize each category, retaining empty kinds and exact alias weights."""
    weights = np.asarray([row["mult"] for row in catalog], dtype=np.int64)[rows["uid"]]
    category_index = {name: i for i, name in enumerate(categories)}
    category = np.asarray([category_index.get(category_of(row), -1) for row in catalog], dtype=np.int16)[rows["uid"]]
    masks = stage_masks(rows)
    width = len(categories)
    slots = width * 6
    exit_hour = (rows["exit"] - start) // 60
    entry_hour = (rows["entry"] - start) // 60
    closed = (exit_hour >= 0) & (exit_hour < 6) & (category >= 0)
    entered = (entry_hour >= 0) & (entry_hour < 6) & (category >= 0)
    valid = np.isfinite(rows["evidenceRaw"]) & np.isfinite(weights) & (weights > 0)
    net = rows["evidenceRaw"] - audit.cost_as_frac(cost_pct)
    stages = {}
    # Five grouped reductions replace 17 x six x five full-tape scans.
    # Own stage decisions are supplied unchanged; these are only statistics.
    for stage, mask in masks.items():
        selected = closed & mask & valid
        key = exit_hour[selected] * width + category[selected]
        n = np.bincount(key, weights=weights[selected], minlength=slots)
        summed = np.bincount(key, weights=net[selected] * weights[selected], minlength=slots)
        wins = np.bincount(key, weights=weights[selected] * (net[selected] > 0), minlength=slots)
        losses = np.bincount(key, weights=weights[selected] * (net[selected] < 0), minlength=slots)
        invalid_rows = closed & mask & ~valid
        invalid = np.bincount(exit_hour[invalid_rows] * width + category[invalid_rows], minlength=slots)
        entry_rows = entered & mask
        entries = np.bincount(entry_hour[entry_rows] * width + category[entry_rows], weights=weights[entry_rows], minlength=slots)
        # Count each numerical prototype once per hour/category, even when
        # LONG and SHORT close in that same hour. Aliases count only in n.
        distinct = np.zeros(slots, dtype=np.int64)
        if len(key):
            packed = np.unique(key.astype(np.int64) * len(catalog) + rows["uid"][selected])
            distinct = np.bincount(packed // len(catalog), minlength=slots)
        values = []
        for slot in range(slots):
            count = int(n[slot])
            mean = summed[slot] / count if count else None
            value = dict(n=count, pf=round(audit.net_return_pf(mean, cost_pct), 6) if count else None,
                netAvgPct=round(mean * 100, 8) if count else None, wins=int(wins[slot]), losses=int(losses[slot]),
                entryPositions=int(entries[slot]), distinctExitBehaviours=int(distinct[slot]))
            if count:
                value["netSumPctUnits"] = round(float(summed[slot] * 100), 6)
            if invalid[slot]:
                value["invalidObservations"] = int(invalid[slot])
            values.append(value)
        stages[stage] = values
    output = []
    for hour in range(6):
        lo, hi = start + hour * 60, start + (hour + 1) * 60
        values = {name: dict(stages={stage: stages[stage][hour * width + i] for stage in STAGES})
                  for i, name in enumerate(categories)}
        output.append(dict(hour=hour + 1, startUtc=audit.utc(start_s + lo * audit.BAR),
                           endUtc=audit.utc(start_s + hi * audit.BAR), **{field: values}))
    return output


def summarize_symbol(result, derived, book, derived_coverage, *, require_zero_parent_proof=False):
    merged = audit.merge_replays(result, derived)
    completed = merged["core"]["exit"] >= 0
    if not np.isfinite(merged["core"]["raw"][completed]).all():
        raise ValueError("Completed own replay contains an unavailable return; source audit required")
    catalog, tapes, candidates, evidence = audit.build_causal_candidates([merged], book)
    zero_parent_proof = None
    if require_zero_parent_proof:
        if derived is not None or derived_coverage.get("ownBaseEligibleParents") != 0:
            raise ValueError("Only a cached empty descendant result can use a zero-parent proof")
        end_bar = len(result["bars"]) - 1
        # This is the same all-closed-endpoint prefix test used to select
        # owners in derived_replay, not a terminal snapshot qualification.
        owners = audit.base_qualified_parent_uids(evidence, book, end_bar)
        if len(owners):
            raise ValueError("Current own evidence has an eligible descendant parent; stale cache refused")
        from axis_grid import axis_ranges
        requested = sum(row["requestedConfigs"] for row in result["coverage"]) * (
            2 * len(axis_ranges(book)) + 2 * (2 * len(book.block_counts) + book.dca_max_steps))
        if derived_coverage.get("requestedDescendantDirectionConfigs") != requested:
            raise ValueError("Stale empty descendant cache has different requested scope geometry")
        zero_parent_proof = dict(qualifiedParents=0, scannedOwnClosedEndpoints=int(len(evidence.exit)),
            eligibleClosedEndpoints=int((evidence.exit < end_bar).sum()), finalEligibleExitBar=end_bar - 1,
            test="Every own closed endpoint before final bar, with canonical own Base last-N and strict PF floor",
            originalDerivedCoverage=dict(derived_coverage))
    start = len(merged["bars"]) - 360
    prep_mask = (tapes["entry"] >= start - 360) & (tapes["entry"] < start)
    preparation = mark_own_stages({key: value[prep_mask] for key, value in tapes.items()}, evidence, book)
    prep_hours = audit.stage_report(catalog, preparation, book.cost_pct, start - 360, merged["startS"])
    snapshots = own_snapshots(merged, catalog, evidence, book, derived_coverage)
    for i, row in enumerate(prep_hours):
        row["endUtc"] = audit.utc(merged["startS"] + (start - 360 + (i + 1) * 60) * audit.BAR)
        row["endpoint"] = {key: value for key, value in snapshots[i + 1].items() if key != "ownWindowPf"}
    kinds = hourly_categories(catalog, candidates, book.cost_pct, start, merged["startS"], audit.INDICATION_KINDS,
        lambda row: row["lane"].split("|")[0] if "|" in row["lane"] else "", "types")
    families = sorted({family(row) for row in catalog})
    strategies = hourly_categories(catalog, candidates, book.cost_pct, start, merged["startS"], families, family, "strategies")
    return dict(symbol=result["symbol"], preparationHourly=prep_hours, byIndicationHourly=kinds,
                byStrategyHourly=strategies, snapshots=snapshots, zeroParentProof=zero_parent_proof)


def combine_stats(values, cost_pct):
    n = sum(value["n"] for value in values)
    summed = sum(value.get("netSumPctUnits", 0) for value in values)
    result = dict(n=n, pf=round(audit.net_return_pf(summed / n / 100, cost_pct), 6) if n else None,
                  netAvgPct=summed / n if n else None, netSumPctUnits=summed,
                  wins=sum(value["wins"] for value in values), losses=sum(value["losses"] for value in values))
    for field in ("entryPositions", "distinctExitBehaviours"):
        result[field] = sum(value.get(field, 0) for value in values)
    invalid = sum(value.get("invalidObservations", 0) for value in values)
    if invalid:
        result["invalidObservations"] = invalid
    return result


def combine_endpoints(values):
    result = dict(t=values[0]["t"], maxOwnObservedDdtS=max(row["maxOwnObservedDdtS"] for row in values))
    for field in ("ddBlockedDirectionConfigs", "requestedDirectionConfigs", "observedDirectionConfigs",
                  "insufficientBaseSamples", "unobservedDirectionConfigs"):
        result[field] = sum(row[field] for row in values)
    for field in ("qualifiedDirectionConfigs", "qualifiedSets"):
        result[field] = {stage: sum(row[field][stage] for row in values) for stage in STAGES if stage != "preBase"}
    return result


def merge_hourly(summaries, source_field, cost_pct, category_field=None):
    output = []
    for hour in range(6):
        rows = [summary[source_field][hour] for summary in summaries]
        result = {key: rows[0][key] for key in ("hour", "startUtc", "endUtc")}
        if category_field:
            names = sorted({name for row in rows for name in row[category_field]})
            result[category_field] = {name: dict(stages={stage: combine_stats(
                [row[category_field][name]["stages"][stage] for row in rows if name in row[category_field]], cost_pct)
                for stage in STAGES}) for name in names}
        else:
            result["stages"] = {stage: combine_stats([row["stages"][stage] for row in rows], cost_pct) for stage in STAGES}
            result["endpoint"] = combine_endpoints([row["endpoint"] for row in rows])
        output.append(result)
    return output


def verify_original_execution(report, strategy_hourly):
    """Reject a supplemental gate model that differs from recorded cohorts."""
    original = report.get("stageHourly") or []
    if len(original) != 6 or any("stages" not in row for row in original):
        return None
    for expected, actual in zip(original, strategy_hourly):
        if expected["startUtc"] != actual["startUtc"]:
            raise ValueError("Supplemental execution hour differs from original report")
        for stage in STAGES:
            rolled = combine_stats([family["stages"][stage] for family in actual["strategies"].values()], report["costPct"])
            wanted = expected["stages"][stage]
            for field in ("n", "wins", "losses", "entryPositions", "distinctExitBehaviours"):
                if wanted[field] != rolled[field]:
                    raise ValueError(f"Supplemental execution cohort differs: hour {expected['hour']} {stage} {field}")
            if wanted["pf"] is None or rolled["pf"] is None:
                if wanted["pf"] != rolled["pf"]:
                    raise ValueError("Supplemental execution PF availability differs")
            elif abs(wanted["pf"] - rolled["pf"]) > 2e-6:
                raise ValueError(f"Supplemental execution PF differs: hour {expected['hour']} {stage}")
    return True


def supplement_report(report, summaries, provenance, model_sha):
    """Return a new report and verify every preexisting field is unchanged."""
    if not summaries:
        raise ValueError("No independently scoped source summaries")
    original = {key: value for key, value in report.items() if key not in OPTIONAL_FIELDS}
    invariant = digest_json(original)
    cost = report["costPct"]
    extra = dict(preparationHourly=merge_hourly(summaries, "preparationHourly", cost),
        preparationBySymbol=[dict(symbol=s["symbol"], hourly=s["preparationHourly"]) for s in summaries],
        byIndicationHourly=merge_hourly(summaries, "byIndicationHourly", cost, "types"),
        byIndicationHourlyBySymbol=[dict(symbol=s["symbol"], hourly=s["byIndicationHourly"]) for s in summaries],
        byStrategyHourly=merge_hourly(summaries, "byStrategyHourly", cost, "strategies"),
        byStrategyHourlyBySymbol=[dict(symbol=s["symbol"], hourly=s["byStrategyHourly"]) for s in summaries],
        supplementalStageSnapshots=[dict(symbol=s["symbol"], snapshots=s["snapshots"]) for s in summaries],
        htf=combined_htf_evidence(report, summaries),
        augmentation=dict(modelSHA=model_sha, generatedAt=audit.utc(time.time()), profile=report.get("profile", "baseline"),
            scopeNote=SCOPE_NOTE, ddtScope="Maximum elapsed seconds within one own full closed tape; admission DDT separately uses its configured own last-N window.",
            sourceCacheSignatures=provenance, preservedOriginalFieldsSHA256=invariant,
            preservedAccountSHA256=digest_json(report["account"]), preservedSourceManifestSHA256=digest_json(report["sourceManifest"]),
            calculationSourceAccountSHA256=digest_json(report["account"]),
            calculationSourceScoringSignature=report.get("scoringSignature"),
            calculationSourceAdmissionCheckpointSha256=report.get("admissionCheckpointSha256"),
            calculationSourceCostPct=cost, calculationSourceEngine=report.get("engine"),
            sourceSymbols=len(summaries), preparationHours=6, executionHours=6, indicationKinds=len(audit.INDICATION_KINDS)))
    augmented = dict(report, **extra)
    augmented["augmentation"]["matchesOriginalHypotheticalExecution"] = verify_original_execution(report, extra["byStrategyHourly"])
    if digest_json({key: augmented[key] for key in original}) != invariant:
        raise AssertionError("An original report field changed during augmentation")
    return augmented


def calculation_supplement(augmented):
    return {name: augmented[name] for name in OPTIONAL_FIELDS}


def merge_calculation_supplement(report, supplement):
    """Reuse descriptive evidence on a later account without recomputation."""
    provenance = supplement["augmentation"]
    if digest_json(report["sourceManifest"]) != provenance["preservedSourceManifestSHA256"]:
        raise ValueError("Calculation supplement source provenance differs")
    if report.get("profile", "baseline") != provenance["profile"] or report["costPct"] != provenance["calculationSourceCostPct"]:
        raise ValueError("Calculation supplement profile or cost differs")
    if report.get("engine") != provenance["calculationSourceEngine"] or report.get("scoringSignature") != provenance["calculationSourceScoringSignature"]:
        raise ValueError("Calculation supplement own window or scoring signature differs")
    if report.get("admissionCheckpointSha256") != provenance.get("calculationSourceAdmissionCheckpointSha256"):
        raise ValueError("Calculation supplement admission checkpoint differs")
    original = {key: value for key, value in report.items() if key not in OPTIONAL_FIELDS}
    fields = {key: supplement[key] for key in OPTIONAL_FIELDS}
    fields["augmentation"] = dict(provenance, preservedAccountSHA256=digest_json(report["account"]),
        preservedOriginalFieldsSHA256=digest_json(original), reusedCalculationSupplement=True,
        mergedAt=audit.utc(time.time()))
    merged = dict(report, **fields)
    if digest_json({key: merged[key] for key in original}) != digest_json(original):
        raise AssertionError("Original fields changed while merging calculation supplement")
    return merged


def write_outputs(augmented, output_path, html_path=None, supplement_path=None):
    output_path = Path(output_path)
    audit.atomic_json(output_path, augmented)
    if supplement_path:
        audit.atomic_json(supplement_path, calculation_supplement(augmented))
    from render_six_hour_audit import render_report
    render_report(augmented, Path(html_path) if html_path else output_path.with_suffix(".html"))


def augment_report(report_path, cache_dir, data_dir, *, output_path=None, html_path=None, progress_path=None,
                   supplement_path=None, checkpoint_dir=None, max_anon_before_builder_gib=3.5):
    report_path, cache_dir, data_dir = Path(report_path), Path(cache_dir), Path(data_dir)
    report = json.loads(report_path.read_text())
    profile = report.get("profile", "baseline")
    manifest = report["sourceManifest"]
    overlay_path = data_dir / "overlay.json"
    if sha_file(overlay_path) != manifest["overlaySha256"]:
        raise ValueError("Frozen overlay differs from report source provenance")
    overlay = audit.profile_overlay(json.loads(overlay_path.read_text()), profile)
    book = audit.se.SetBook()
    book.load(overlay)
    if book.cost_pct != report["costPct"]:
        raise ValueError("Configured cost differs from original report")
    geometry = tuple(report["engine"][key] for key in ("baseWindow", "mainWindow", "realWindow"))
    if geometry != tuple(book._stage_window_ns()) or report["engine"]["requiredSamples"] != book.eval_need():
        raise ValueError("Profile window geometry differs from original report")
    if report["engine"].get("minPf") is not None and report["engine"]["minPf"] != book.min_pf:
        raise ValueError("Profile PF floor differs from original report")
    sources = {source["symbol"]: source for source in manifest["symbols"]}
    output_path = Path(output_path) if output_path else report_path.with_name("six-hour-augmented.json")
    checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else output_path.parent / "augmentation-checkpoints"
    model_sha = calculation_model_signature()
    summaries, provenance = [], []
    for symbol in report["symbols"]:
        raw_path = cache_dir / (symbol + ".pkl")
        derived_path = cache_dir / (symbol + "." + profile + ".derived.pkl")
        source = sources[symbol]
        if sha_file(data_dir / (symbol + ".json")) != source["sha256"]:
            raise ValueError("Frozen candles differ from report: " + symbol)
        source_rows = json.loads((data_dir / (symbol + ".json")).read_text())["rows"]
        htf_source = closed_utc_hour_evidence(source_rows)
        del source_rows
        funding_path = data_dir / "funding" / (symbol + ".json")
        if sha_file(funding_path) != source["fundingSha256"]:
            raise ValueError("Frozen funding differs from report: " + symbol)
        if not derived_path.exists():
            raise ValueError("Existing profile-derived cache required; no replay is allowed: " + symbol)
        raw_sha, derived_sha = sha_file(raw_path), sha_file(derived_path)
        key = checkpoint_key(symbol, profile, source, overlay, model_sha, raw_sha, derived_sha)
        checkpoint_path = checkpoint_dir / (symbol + "." + profile + ".json")
        saved = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else None
        if saved and saved.get("calculationKey") == key:
            summary, proof = saved["summary"], saved["provenance"]
            if summary["symbol"] != symbol or tuple(proof["rawSignature"][1:]) != (source["sha256"], manifest["overlaySha256"]):
                raise ValueError("Supplemental checkpoint own input scope differs: " + symbol)
            summaries.append(summary)
            provenance.append(proof)
            print(json.dumps(dict(phase="augment", profile=profile, symbol=symbol, cached=True,
                completedSymbols=len(summaries), requestedSymbols=len(report["symbols"]))), flush=True)
            continue
        # No previous full-tape object survives this boundary. Lightweight
        # verified checkpoints can be read even while other scorers are busy.
        wait_for_headroom(max_anon_before_builder_gib, profile, len(summaries), len(report["symbols"]), progress_path)
        raw = pickle.loads(raw_path.read_bytes())
        if raw["symbol"] != symbol or tuple(raw["signature"][1:]) != (source["sha256"], manifest["overlaySha256"]):
            raise ValueError("Raw cache input provenance differs: " + symbol)
        raw["funding"] = json.loads(funding_path.read_text())
        derived = pickle.loads(derived_path.read_bytes())
        expected_derived_signature = derived_signature(raw, overlay, profile, data_dir)
        derived_key_matches = derived["signature"] == expected_derived_signature
        if not derived_key_matches and (derived["result"] is not None or derived["coverage"].get("ownBaseEligibleParents") != 0):
            raise ValueError("Nonempty profile-derived cache signature differs: " + symbol)
        if not derived["coverage"].get("complete"):
            raise ValueError("Profile-derived coverage is incomplete: " + symbol)
        expected_start = manifest["historyStart"] / 1000
        if raw["startS"] != expected_start or len(raw["bars"]) != (manifest["end"] - manifest["historyStart"]) // 60000:
            raise ValueError("Raw replay time geometry differs: " + symbol)
        summary = summarize_symbol(raw, derived["result"], book, derived["coverage"],
                                   require_zero_parent_proof=not derived_key_matches)
        summary["htfSource"] = htf_source
        proof = dict(symbol=symbol, rawSignature=list(raw["signature"]), rawCacheSHA256=raw_sha,
            derivedSignature=derived["signature"], derivedCacheSHA256=derived_sha,
            currentDerivedSignature=expected_derived_signature, derivedModelKeyMatched=derived_key_matches,
            emptyDerivedReuseProof=summary["zeroParentProof"],
            fundingSHA256=source["fundingSha256"], derivedCoverage=derived["coverage"])
        # Publish only compact completed statistics. A cancellation after this
        # atomic write loses no completed symbol and cannot alter admission.
        audit.atomic_json(checkpoint_path, dict(calculationKey=key, summary=summary, provenance=proof))
        summaries.append(summary)
        provenance.append(proof)
        print(json.dumps(dict(phase="augment", profile=profile, symbol=symbol, completedSymbols=len(summaries),
                             requestedSymbols=len(report["symbols"]))), flush=True)
        if progress_path:
            audit.atomic_json(progress_path, dict(phase="augment", profile=profile, completedSymbols=len(summaries),
                                                  requestedSymbols=len(report["symbols"])))
        del raw, derived, summary
        audit.release_working_memory()
    if calculation_model_signature() != model_sha:
        raise ValueError("Supplemental math source changed while processing; verified checkpoints are preserved")
    augmented = supplement_report(report, summaries, provenance, model_sha)
    supplement_path = Path(supplement_path) if supplement_path else output_path.with_name("six-hour-calculation-supplement.json")
    write_outputs(augmented, output_path, html_path, supplement_path)
    if progress_path:
        audit.atomic_json(progress_path, dict(phase="complete", profile=profile, completedSymbols=len(summaries),
                                              preservedAccountSHA256=augmented["augmentation"]["preservedAccountSHA256"]))
    return augmented


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--html", default="")
    parser.add_argument("--progress", default="")
    parser.add_argument("--supplement", default="", help="Write reusable supplemental fields separately")
    parser.add_argument("--merge-supplement", default="", help="Reuse matching computed fields with a later account report")
    parser.add_argument("--checkpoint-dir", default="", help="Compact per-symbol output checkpoints, outside input caches")
    parser.add_argument("--max-anon-before-builder-gib", type=float, default=3.5,
                        help="Wait at each symbol boundary if shared anonymous memory exceeds this limit")
    args = parser.parse_args()
    output = args.output or str(Path(args.report).with_name("six-hour-augmented.json"))
    if args.merge_supplement:
        result = merge_calculation_supplement(json.loads(Path(args.report).read_text()),
                                             json.loads(Path(args.merge_supplement).read_text()))
        write_outputs(result, output, args.html or None, args.supplement or None)
    else:
        if not args.cache_dir or not args.data_dir:
            parser.error("--cache-dir and --data-dir are required when calculating a new supplement")
        result = augment_report(args.report, args.cache_dir, args.data_dir,
            output_path=output, html_path=args.html or None, progress_path=args.progress or None,
            supplement_path=args.supplement or None, checkpoint_dir=args.checkpoint_dir or None,
            max_anon_before_builder_gib=args.max_anon_before_builder_gib)
    print(json.dumps(dict(report=output, profile=result.get("profile"),
                          symbols=result["augmentation"]["sourceSymbols"],
                          accountPreservedSHA256=result["augmentation"]["preservedAccountSHA256"])))


if __name__ == "__main__":
    main()
