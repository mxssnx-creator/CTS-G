"""Bounded, restart-safe, idempotent pulse activity/event ledger."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, Iterable, List, Optional

from contracts import AXES, INDICATION_KINDS, STRATEGIES, stable_key

EVENT_TYPES = (
    "evaluation",
    "qualification",
    "entry_intent",
    "exchange_request",
    "exchange_response",
    "fill",
    "position_open",
    "position_snapshot",
    "protection",
    "control_request",
    "control_response",
    "cancellation",
    "close",
    "rejected",
    "error",
    "reconciliation",
    "other",
)
EVENT_TYPE_SET = set(EVENT_TYPES)


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    event_type: str
    ts: float
    connection: str
    status: str = ""
    symbol: str = ""
    side: str = ""
    set_id: str = ""
    parent_set_id: str = ""
    axis_key: str = ""
    indication_kind: str = ""
    strategy: str = ""
    order_id: str = ""
    client_id: str = ""
    code: str = ""
    qty: float = 0.0
    price: float = 0.0
    pnl: float = 0.0
    detail: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def _text(value: Any, limit: int = 220) -> str:
    return str(value or "")[:limit]


def _metadata(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, item in list(value.items())[:24]:
        try:
            json.dumps(item)
            out[str(key)[:60]] = item
        except Exception:
            out[str(key)[:60]] = str(item)[:120]
    return out


class EventLedger:
    """Keep the latest committed actions, deduped by connection + event ID."""

    def __init__(self, path: str = "", connection: str = "", max_events: int = 512) -> None:
        self.path = path
        self.connection = str(connection or "")
        self.max_events = max(32, min(4096, int(max_events or 512)))
        self.events: Deque[LedgerEvent] = deque(maxlen=self.max_events)
        self._ids: set[str] = set()
        self.duplicate_count = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as state_file:
                raw = json.load(state_file)
        except Exception:
            return
        rows = raw.get("events") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return
        for row in rows[-self.max_events :]:
            event = self._coerce(row)
            if event and event.event_id not in self._ids:
                self.events.append(event)
                self._ids.add(event.event_id)

    def _coerce(self, row: Any) -> Optional[LedgerEvent]:
        if not isinstance(row, dict):
            return None
        event_id = _text(row.get("event_id") or row.get("eventId"), 160)
        event_type = _text(row.get("event_type") or row.get("eventType"), 48)
        if not event_id or not event_type:
            return None
        if event_type not in EVENT_TYPE_SET:
            event_type = "other"
        return LedgerEvent(
            event_id=event_id,
            event_type=event_type,
            ts=_number(row.get("ts")) or time.time(),
            connection=_text(row.get("connection") or self.connection, 64),
            status=_text(row.get("status"), 32),
            symbol=_text(row.get("symbol"), 48),
            side=_text(row.get("side"), 16),
            set_id=_text(row.get("set_id") or row.get("setId"), 160),
            parent_set_id=_text(row.get("parent_set_id") or row.get("parentSetId"), 160),
            axis_key=_text(row.get("axis_key") or row.get("axisKey"), 48),
            indication_kind=_text(row.get("indication_kind") or row.get("indicationKind") or row.get("indKind"), 32),
            strategy=_text(row.get("strategy"), 48),
            order_id=_text(row.get("order_id") or row.get("orderId"), 96),
            client_id=_text(row.get("client_id") or row.get("clientId"), 120),
            code=_text(row.get("code"), 48),
            qty=_number(row.get("qty")),
            price=_number(row.get("price")),
            pnl=_number(row.get("pnl")),
            detail=_text(row.get("detail"), 240),
            metadata=_metadata(row.get("metadata")),
        )

    def _save_locked(self) -> None:
        if not self.path:
            return
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {
                "version": 1,
                "connection": self.connection,
                "updatedAt": time.time(),
                "events": [event.as_dict() for event in self.events],
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w") as state_file:
                json.dump(payload, state_file, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            # Activity accounting must never stop trading when persistence is unavailable.
            pass

    def record(
        self,
        event_type: str,
        event_id: str = "",
        *,
        status: str = "",
        ts: Optional[float] = None,
        **fields: Any,
    ) -> bool:
        """Commit one event; returns False for a duplicate callback/retry."""
        normalized_type = str(event_type or "other").strip().lower()
        if normalized_type not in EVENT_TYPE_SET:
            normalized_type = "other"
        client_id = _text(fields.get("client_id") or fields.get("clientId"), 120)
        order_id = _text(fields.get("order_id") or fields.get("orderId"), 96)
        symbol = _text(fields.get("symbol"), 48)
        side = _text(fields.get("side"), 16)
        set_id = _text(fields.get("set_id") or fields.get("setId"), 160)
        stamp = _number(ts) or time.time()
        supplied_id = _text(event_id, 160)
        key = supplied_id or stable_key(
            self.connection,
            normalized_type,
            symbol,
            side,
            set_id,
            client_id,
            order_id,
            fields.get("code"),
            round(stamp, 3),
            fields.get("detail"),
        )
        with self._lock:
            if key in self._ids:
                self.duplicate_count += 1
                return False
            event = LedgerEvent(
                event_id=key,
                event_type=normalized_type,
                ts=round(stamp, 3),
                connection=_text(fields.get("connection") or self.connection, 64),
                status=_text(status or fields.get("state"), 32),
                symbol=symbol,
                side=side,
                set_id=set_id,
                parent_set_id=_text(fields.get("parent_set_id") or fields.get("parentSetId"), 160),
                axis_key=_text(fields.get("axis_key") or fields.get("axisKey"), 48),
                indication_kind=_text(fields.get("indication_kind") or fields.get("indicationKind") or fields.get("indKind"), 32),
                strategy=_text(fields.get("strategy"), 48),
                order_id=order_id,
                client_id=client_id,
                code=_text(fields.get("code"), 48),
                qty=_number(fields.get("qty")),
                price=_number(fields.get("price")),
                pnl=_number(fields.get("pnl")),
                detail=_text(fields.get("detail"), 240),
                metadata=_metadata(fields.get("metadata")),
            )
            self.events.append(event)
            self._ids = {item.event_id for item in self.events}
            self._save_locked()
            return True

    def tail(self, n: int = 32) -> List[Dict[str, Any]]:
        with self._lock:
            return [event.as_dict() for event in list(reversed(self.events))[: max(0, int(n))]]

    def _count_by(self, attr: str) -> Dict[str, int]:
        counts = Counter()
        for event in self.events:
            value = str(getattr(event, attr, "") or "")
            if value:
                counts[value] += 1
        return dict(counts)

    def _outcome_counts(self, attr: str, known: Iterable[str]) -> Dict[str, Dict[str, int]]:
        out = {key: {"evaluated": 0, "qualified": 0, "selected": 0, "entered": 0, "exited": 0, "blocked": 0, "rejected": 0, "paused": 0, "long": 0, "short": 0} for key in known}
        for event in self.events:
            key = str(getattr(event, attr, "") or "")
            if not key:
                continue
            bucket = out.setdefault(key, {name: 0 for name in next(iter(out.values())).keys()})
            event_type = event.event_type
            status = event.status.lower()
            if event_type == "evaluation":
                bucket["evaluated"] += 1
            if event_type == "qualification" and status in ("qualified", "pass", "passed"):
                bucket["qualified"] += 1
            if event_type == "entry_intent":
                bucket["selected"] += 1
            if event_type == "position_open":
                bucket["entered"] += 1
            if event_type == "close":
                bucket["exited"] += 1
            if event_type == "rejected" or status in ("blocked", "rejected"):
                bucket["rejected"] += 1
                if status == "blocked":
                    bucket["blocked"] += 1
            if status == "paused":
                bucket["paused"] += 1
            if event.side.upper() == "LONG":
                bucket["long"] += 1
            elif event.side.upper() == "SHORT":
                bucket["short"] += 1
        return out

    def summary(self, *, internal_open: int = 0, exchange_open: int = -1, internal_closed: int = 0) -> Dict[str, Any]:
        with self._lock:
            events = list(self.events)
            by_type = {key: 0 for key in EVENT_TYPES}
            by_status: Dict[str, int] = {}
            codes: Dict[str, int] = {}
            for event in events:
                by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
                if event.status:
                    by_status[event.status] = by_status.get(event.status, 0) + 1
                if event.code:
                    codes[event.code] = codes.get(event.code, 0) + 1
            exchange_known = int(exchange_open) >= 0
            parity = "pending" if not exchange_known else ("match" if int(internal_open) == int(exchange_open) else "discrepant")
            return {
                "eventCount": len(events),
                "duplicateCount": int(self.duplicate_count),
                "byType": by_type,
                "byStatus": by_status,
                "responseCodes": codes,
                "requestCount": by_type.get("exchange_request", 0) + by_type.get("control_request", 0),
                "responseCount": by_type.get("exchange_response", 0) + by_type.get("control_response", 0),
                "fillCount": by_type.get("fill", 0),
                "openEventCount": by_type.get("position_open", 0),
                "closeEventCount": by_type.get("close", 0),
                "protectionEventCount": by_type.get("protection", 0),
                "cancellationCount": by_type.get("cancellation", 0),
                "errorCount": by_type.get("error", 0),
                "internalOpen": int(internal_open),
                "exchangeOpen": int(exchange_open),
                "internalClosed": int(internal_closed),
                "parity": parity,
                "pendingCount": sum(1 for event in events if event.status.lower() == "pending"),
                "recoveredCount": sum(1 for event in events if event.status.lower() == "recovered"),
                "discrepantCount": sum(1 for event in events if event.status.lower() in ("discrepant", "mismatch")),
                "byIndication": self._outcome_counts("indication_kind", INDICATION_KINDS),
                "byStrategy": self._outcome_counts("strategy", STRATEGIES),
                "byAxis": self._outcome_counts("axis_key", AXES),
                "tail": [event.as_dict() for event in list(reversed(events))[:32]],
                "source": "committed-event-ledger",
            }


def self_test() -> List[tuple[str, bool, str]]:
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="pulse-ledger-"), "events.json")
    ledger = EventLedger(path, "bingx-x02", max_events=32)
    first = ledger.record("entry_intent", "entry-1", status="selected", symbol="SOL-USDT", side="LONG", strategy="general")
    duplicate = ledger.record("entry_intent", "entry-1", status="selected", symbol="SOL-USDT", side="LONG", strategy="general")
    ledger.record("position_open", "open-1", status="confirmed", symbol="SOL-USDT", side="LONG", strategy="general")
    ledger.record("reconciliation", "recon-1", status="discrepant", detail="book-only")
    restored = EventLedger(path, "bingx-x02", max_events=32)
    summary = restored.summary(internal_open=1, exchange_open=2, internal_closed=0)
    return [
        ("ledger-commit", first and not duplicate, f"first={first} duplicate={duplicate}"),
        ("ledger-restart", len(restored.events) == 3, f"n={len(restored.events)}"),
        ("ledger-bounded", len(restored.tail(32)) <= 32, f"n={len(restored.tail(32))}"),
        ("ledger-parity", summary.get("parity") == "discrepant", str(summary.get("parity"))),
        ("ledger-tail", bool(summary.get("tail")) and summary["tail"][0]["event_type"] == "reconciliation", str(summary.get("tail")[:1])),
    ]


if __name__ == "__main__":
    failures = [row for row in self_test() if not row[1]]
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
    raise SystemExit(1 if failures else 0)
