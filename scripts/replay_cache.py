"""Fingerprint and verify trusted, locally generated shadow replay caches."""
import hashlib
import json
import os
import pickle
import tempfile


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(data_dir, names, overlay, pulse_dir, stage_eval_path):
    files = sorted(os.path.join(pulse_dir, name) for name in os.listdir(pulse_dir) if name.endswith(".py"))
    return {"version": 1,
            "markets": {name: file_sha(os.path.join(data_dir, name + ".json")) for name in names},
            "settings": hashlib.sha256(json.dumps(overlay, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "engine": {os.path.basename(path): file_sha(path) for path in files},
            "simulator": file_sha(stage_eval_path)}


def load(cache, expected):
    """Reject old, modified or mismatched caches; pickle must still be trusted."""
    meta_path = cache + ".meta.json"
    if not os.path.isfile(meta_path):
        raise ValueError("cache has no provenance metadata; use a new cache path")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if meta.get("fingerprint") != expected or meta.get("cacheSha256") != file_sha(cache):
        raise ValueError("cache provenance differs from the market data, settings, engine or saved bytes")
    with open(cache, "rb") as f:
        return pickle.load(f)


def save(cache, value, expected):
    """Replace each file atomically and bind metadata to the saved pickle bytes."""
    directory = os.path.dirname(os.path.abspath(cache))
    os.makedirs(directory, exist_ok=True)
    temporary = []
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
            temporary.append(f.name)
            pickle.dump(value, f)
        meta = {"fingerprint": expected, "cacheSha256": file_sha(temporary[0])}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as f:
            temporary.append(f.name)
            json.dump(meta, f, sort_keys=True)
        os.replace(temporary[0], cache)
        os.replace(temporary[1], cache + ".meta.json")
    finally:
        for path in temporary:
            if os.path.exists(path):
                os.unlink(path)
