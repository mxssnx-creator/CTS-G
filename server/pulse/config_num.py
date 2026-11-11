"""Numeric config reads with one rule: a present value is used even when it is zero.

Only a missing key, an empty string or an unparsable value takes the default. The old `value or default` form turned
an explicit 0 into the default, so a deliberate zero (no lock, no minimum hold) silently changed behaviour.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence, Union


def num(ov: Mapping[str, Any], keys: Union[str, Sequence[str]], default: Any, cast: Callable[[Any], Any] = float) -> Any:
    """The first present key in `keys` read with `cast`, else `default` (cast too). Zero is a value."""
    names = (keys,) if isinstance(keys, str) else tuple(keys)
    for key in names:
        raw = ov.get(key)
        if raw is None or raw == "":
            continue
        try:
            return cast(raw)
        except (TypeError, ValueError):
            continue
    return cast(default)


if __name__ == "__main__":
    assert num({"k": 0}, "k", 45) == 0.0
    assert num({}, "k", 45) == 45.0
    assert num({"k": ""}, "k", 45) == 45.0
    assert num({"k": "x"}, "k", 45) == 45.0
    assert num({"a": 0, "b": 7}, ("a", "b"), 1) == 0.0      # first present key wins, even at zero
    assert num({"b": 7}, ("a", "b"), 1) == 7.0
    assert num({"n": 0}, "n", 5, int) == 0
    print("config_num ok")
