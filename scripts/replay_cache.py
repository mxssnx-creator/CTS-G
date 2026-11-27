"""Fingerprint and verify trusted, locally generated shadow replay caches."""
import hashlib
import gzip
import json
import os
import pickle
import tempfile
from pathlib import Path


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(data_dir, names, overlay, pulse_dir, stage_eval_path):
    files = sorted(os.path.join(pulse_dir, name) for name in os.listdir(pulse_dir) if name.endswith(".py"))
    assets = Path(pulse_dir).resolve().parents[1] / "shared" / "cts-indications"
    if not assets.is_dir():
        assets = Path(pulse_dir) / "shared" / "cts-indications"
    source_assets = {str(path.relative_to(assets)): file_sha(path) for path in sorted(assets.rglob("*"))
                     if path.is_file() and path.suffix in (".ts", ".json")}
    settings = {k: v for k, v in overlay.items() if not str(k).startswith(
        ("groupGate", "relationLanes", "laneOrder", "lastNBasis", "blockRounds", "axisDisjoint", "axisLastOutcome"))}
    def digest(values):
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    grid = settings.get("stageLastN")
    # Only the enabled mode can affect collection. Its window counts and
    # signs filter the resulting tapes after collection, in every simulation.
    shadow_settings = {**settings, "stageLastN": True} if isinstance(grid, dict) and grid.get("enabled", True) else settings
    shadow_settings = {k: v for k, v in shadow_settings.items() if k not in ("maxOpen", "maxPerGroup")}
    return {"version": 1,
            "markets": {name: file_sha(os.path.join(data_dir, name + ".json")) for name in names},
            # the profit group gate (groupGate*), the relation lanes (relationLanes) the lane order (laneOrder) the basis of the last-N windows (lastNBasis), the Block rounds (blockRounds) and the disjoint Axis rule (axisDisjoint, axisLastOutcome) act on the scoring or at entry time and change no tape:
            # arms that differ in them share one cache
            "settings": digest(settings), "shadowSettings": digest(shadow_settings),
            "engine": {os.path.basename(path): file_sha(path) for path in files},
            "sourceAssets": source_assets,
            "simulator": file_sha(stage_eval_path)}


def load(cache, expected):
    """Reject old, modified or mismatched caches; pickle must still be trusted."""
    meta_path = cache + ".meta.json"
    if not os.path.isfile(meta_path):
        raise ValueError("cache has no provenance metadata; use a new cache path")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    stored = meta.get("fingerprint", {})
    # Old manifests establish only exact-settings reuse. New manifests
    # also prove equivalence of the shadow trades across Last-N choices.
    ignored = "settings" if "shadowSettings" in stored and "shadowSettings" in expected else "shadowSettings"
    if ({k: v for k, v in stored.items() if k != ignored} != {k: v for k, v in expected.items() if k != ignored}
            or meta.get("cacheSha256") != file_sha(cache)):
        raise ValueError("cache provenance differs from the market data, settings, engine or saved bytes")
    encoding = meta.get("encoding", "pickle")
    if encoding not in ("pickle", "pickle-gzip"):
        raise ValueError("unsupported replay cache encoding")
    opener = gzip.open if encoding == "pickle-gzip" else open
    with opener(cache, "rb") as f:
        return pickle.load(f)


def save(cache, value, expected):
    """Replace each file atomically and bind metadata to the saved pickle bytes."""
    directory = os.path.dirname(os.path.abspath(cache))
    os.makedirs(directory, exist_ok=True)
    temporary = []
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
            temporary.append(f.name)
            if str(cache).endswith(".gz"):
                with gzip.GzipFile(filename="", fileobj=f, mode="wb", compresslevel=1, mtime=0) as packed:
                    pickle.dump(value, packed)
            else:
                pickle.dump(value, f)
        meta = {"fingerprint": expected, "cacheSha256": file_sha(temporary[0])}
        if str(cache).endswith(".gz"):
            meta["encoding"] = "pickle-gzip"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as f:
            temporary.append(f.name)
            json.dump(meta, f, sort_keys=True)
        os.replace(temporary[0], cache)
        os.replace(temporary[1], cache + ".meta.json")
    finally:
        for path in temporary:
            if os.path.exists(path):
                os.unlink(path)
