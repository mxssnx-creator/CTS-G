"""Independent DCA cost bases inside one physical parent position.

The venue may maintain one averaged position. This ledger retains the base
entry and every confirmed child Set lot, and attributes confirmed partial
closes proportionally across their remaining quantities. It never creates
orders or adds unconfirmed requested quantities to accounting.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from contracts import normalize_direction, stable_key
from position_cost import (POSITION_COST_PCT_DEFAULT, normalize_position_cost_pct,
                           row_notional, row_pnl_pct, row_position_cost_pct)
from storage_paths import atomic_write

EPS = 1e-10
RECEIPT_LIMIT = 4096


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise ValueError(f"Invalid {name}")
    return number


def _manual_cost(meta: dict) -> bool:
    source = str(meta.get("cost_source") or meta.get("costSource") or "").lower()
    return any(token in source for token in ("manual", "fallback", "live-average", "estimate"))


class DcaLotLedger:
    """Connection-scoped, restart-safe confirmed-fill accounting.

    ``path=None`` is intentionally in-memory for deterministic simulations.
    A live caller supplies its durable connection-specific state path.
    """

    def __init__(self, connection: str, path: str | None = None):
        self.connection = str(connection)
        if not self.connection:
            raise ValueError("DCA ledger requires connection identity")
        self.path = str(path) if path is not None else None
        self.lock = threading.RLock()
        self.lots: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.entries: dict[str, bool] = {}
        self.closes: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.retired: dict[str, bool] = {}
        self.epochs: dict[str, int] = {}
        if self.path and Path(self.path).exists():
            state = json.loads(Path(self.path).read_text())
            if state.get("version") != 1 or state.get("connection") != self.connection:
                raise ValueError("DCA ledger version/connection mismatch")
            self._restore(state)

    def _state(self) -> dict:
        return deepcopy(dict(version=1, connection=self.connection, lots=self.lots,
                             orders=self.orders, entries=self.entries, closes=self.closes,
                             pending=self.pending, retired=self.retired, epochs=self.epochs))

    def _restore(self, state: dict) -> None:
        for name in ("lots", "orders", "entries", "closes", "pending", "retired", "epochs"):
            setattr(self, name, deepcopy(state.get(name) or {}))

    def _commit(self, previous: dict) -> None:
        # Atomic-write failure must leave a retryable original inventory.
        try:
            if self.path:
                atomic_write(self.path, self._state())
        except Exception:
            self._restore(previous)
            raise

    def _trim(self) -> None:
        for mapping in (self.entries, self.closes, self.retired):
            while len(mapping) > RECEIPT_LIMIT:
                mapping.pop(next(iter(mapping)))

    def has_parent(self, parent_key: str) -> bool:
        with self.lock:
            return (any(lot["parent_key"] == str(parent_key) and lot["remaining_qty"] > EPS for lot in self.lots.values())
                    or any(receipt["parent_key"] == str(parent_key) for receipt in self.pending.values()))

    def pending_closes(self, parent_key: str | None = None) -> list[dict]:
        """Unacknowledged close rows for idempotent journal recovery."""
        with self.lock:
            return [deepcopy(receipt) for receipt in self.pending.values()
                    if parent_key is None or receipt["parent_key"] == str(parent_key)]

    def ack_close(self, close_fill_id: str, parent_key: str | None = None) -> bool:
        """Acknowledge only after every row is durably in the trade journal.

        The unambiguous ``outbox_id`` may be used directly. Raw exchange fill
        ids also work when unique, or when qualified by ``parent_key``.
        """
        with self.lock:
            keys = ([close_fill_id] if close_fill_id in self.pending else
                    [key for key, receipt in self.pending.items()
                     if receipt["close_fill_id"] == str(close_fill_id)
                     and (parent_key is None or receipt["parent_key"] == str(parent_key))])
            if len(keys) > 1:
                raise ValueError("Ambiguous DCA close acknowledgement requires parent_key")
            if not keys:
                return False
            previous = self._state()
            self.pending.pop(keys[0])
            self._commit(previous)
            return True

    def parent_qty(self, parent_key: str) -> float:
        with self.lock:
            return sum(lot["remaining_qty"] for lot in self.lots.values() if lot["parent_key"] == str(parent_key))

    def tracked_parent_state(self, parent_key: str) -> dict:
        """Inventory truth even after a full close was published/acknowledged."""
        parent_key = str(parent_key)
        with self.lock:
            remaining = sum(lot["remaining_qty"] for lot in self.lots.values()
                            if lot["parent_key"] == parent_key)
            latest = next((receipt for receipt in reversed(list(self.closes.values()))
                           if receipt["parent_key"] == parent_key), None)
            tracked = (parent_key in self.retired or latest is not None
                       or any(lot["parent_key"] == parent_key for lot in self.lots.values())
                       or any(receipt["parent_key"] == parent_key for receipt in self.pending.values()))
            return dict(tracked=tracked, parent_key=parent_key, remainingQty=remaining,
                        retired=parent_key in self.retired,
                        latestClose=deepcopy(latest), closeIntent=deepcopy((latest or {}).get("metadata") or {}))

    def snapshot(self, parent_key: str | None = None) -> dict:
        with self.lock:
            lots = [deepcopy(lot) for lot in self.lots.values()
                    if parent_key is None or lot["parent_key"] == str(parent_key)]
            return dict(connection=self.connection, lots=lots,
                        qty=sum(lot["remaining_qty"] for lot in lots), persistent=bool(self.path))

    def entry_lots(self, client_id: str) -> list[dict]:
        """Only matching active original-entry lots, without copying the book."""
        client_id = str(client_id)
        with self.lock:
            return [deepcopy(lot) for lot in self.lots.values()
                    if lot["remaining_qty"] > EPS
                    and lot.get("entry_client_id", lot["client_id"]) == client_id]

    def refine_order(self, parent_key: str, cumulative_qty: float, price: float, fee: float, *,
                     order_id: str = "", client_id: str = "", metadata: dict | None = None) -> dict:
        """Apply a measured cumulative price/fee refinement before any close.

        Runtime delta registrations also retain their cumulative quote basis,
        so a later venue fee need not create another entry fill. Corrections
        after a partial close require a separate cash reconciliation and are
        deliberately rejected here rather than rewriting published history.
        """
        parent_key = str(parent_key)
        if not parent_key or not (order_id or client_id):
            raise ValueError("Order refinement requires parent and order identity")
        qty = _number(cumulative_qty, "cumulative quantity", positive=True)
        price, fee = _number(price, "price", positive=True), _number(fee, "fee")
        meta = dict(metadata or {})
        with self.lock:
            matching = [lot for lot in self.lots.values() if lot["parent_key"] == parent_key
                        and (not order_id or lot["order_id"] == str(order_id))
                        and (not client_id or lot.get("entry_client_id", lot["client_id"]) == str(client_id))]
            if len(matching) != 1:
                raise ValueError("Order refinement requires one unambiguous own lot")
            lot = matching[0]
            if lot["closed_qty"] > EPS or abs(lot["opened_qty"]-qty) > max(EPS, qty*1e-8):
                raise ValueError("Closed or unmatched order refinement requires cash reconciliation")
            delta_fee = fee-lot["entry_fee"]
            if delta_fee < -EPS:
                raise ValueError("Cumulative fee regressed")
            delta_notional = qty*price-lot["notional"]
            known = not _manual_cost(meta)
            if (abs(delta_fee) <= EPS and abs(delta_notional) <= EPS
                    and known == lot["entry_fee_known"] and all(lot["metadata"].get(key) == value for key,value in meta.items())):
                return dict(duplicate=True, qty=0., feeDelta=0., notionalDelta=0., lot=deepcopy(lot))
            previous = self._state()
            lot.update(entry=price, notional=qty*price, entry_fee=fee,
                       remaining_entry_fee=fee, entry_fee_known=known)
            lot["metadata"].update(meta)
            for order in self.orders.values():
                if order["lot_id"] == lot["lot_id"]:
                    order.update(qty=qty, fee=fee, notional=qty*price)
            self._commit(previous)
            return dict(duplicate=False, qty=0., feeDelta=delta_fee,
                        notionalDelta=delta_notional, lot=deepcopy(lot))

    def register_fill(self, parent_key: str, parent_set_id: str, child_set_id: str, symbol: str, side: str,
                      qty: float, price: float, fee: float = 0, fill_id: str = "", *, order_id: str = "",
                      client_id: str = "", strategy: str = "dca", t: float | None = None,
                      cumulative: bool = False, metadata: dict | None = None,
                      reactivate: bool = False, prior_roundtrip_result: dict | None = None) -> dict:
        """Record a unique confirmed delta, or cumulative order qty/avg/fee.

        For cumulative updates the incremental cost basis is the change in
        cumulative quote notional, rather than the newest average price.
        A delta requires a stable unique fill id; requested qty is excluded.
        """
        parent_key, child_set_id = str(parent_key), str(child_set_id)
        direction = normalize_direction(side)
        if not parent_key or not child_set_id or not direction:
            raise ValueError("DCA fill requires parent, child Set and direction")
        qty, price, fee = _number(qty, "quantity"), _number(price, "price", positive=True), _number(fee, "fee")
        stamp = _number(time.time() if t is None else t, "timestamp")
        meta = dict(metadata or {})
        prior = dict(prior_roundtrip_result or {})
        if prior:
            if (cumulative or reactivate or str(child_set_id) != str(parent_set_id)
                    or not prior.get("exchange_confirmed") or not prior.get("partial")
                    or str(prior.get("client_id") or "") != str(client_id)
                    or str(prior.get("set_id") or "") != child_set_id
                    or str(prior.get("symbol") or "") != str(symbol)
                    or normalize_direction(prior.get("side")) != direction):
                raise ValueError("Prior partial close must belong to the confirmed baseline position")
            prior_qty = _number(prior.get("qty", 0), "prior closed quantity", positive=True)
            prior_notional = _number(row_notional(prior), "prior closed notional", positive=True)
            prior_entry_fee = _number(prior.get("entry_fee", 0), "prior entry fee")
            prior_cost = prior_notional * row_position_cost_pct(prior) / 100.0
            prior_gross = prior_notional * row_pnl_pct(prior)
            if not math.isfinite(float(prior.get("pnl", math.nan))):
                raise ValueError("Prior partial close has invalid net PnL")
            prior.update(notional=prior_notional, grossPnl=prior_gross, cost_usdt=prior_cost,
                         estimated_cost_usdt=prior.get("estimated_cost_usdt", prior_cost if _manual_cost(prior) else 0.0))
        if not cumulative and not fill_id:
            raise ValueError("Confirmed delta requires unique fill_id")
        event_key = stable_key(self.connection, parent_key, fill_id) if fill_id else ""
        with self.lock:
            retired = parent_key in self.retired
            epoch = int(self.epochs.get(parent_key) or 0) + int(retired and reactivate)
            lot_id = stable_key("dca-lot", self.connection, parent_key, child_set_id,
                                order_id or client_id or fill_id, epoch)
            if (retired and not reactivate) or (not cumulative and event_key in self.entries):
                return dict(duplicate=True, qty=0.0, lot_id=lot_id)
            if retired and cumulative:
                raise ValueError("Reactivation requires a proven unique confirmed delta")
            order_key = stable_key(self.connection, parent_key, order_id or client_id or fill_id, epoch)
            old = self.orders.get(order_key, {}) if cumulative else {}
            if old and (old["lot_id"] != lot_id or old["symbol"] != symbol or old["side"] != direction):
                raise ValueError("DCA cumulative order identity changed")
            old_qty = float(old.get("qty") or 0)
            if cumulative and qty < old_qty - EPS:
                return dict(duplicate=True, stale=True, qty=0.0, lot_id=lot_id)
            delta = qty - old_qty if cumulative else qty
            delta_fee = fee - float(old.get("fee") or 0) if cumulative else fee
            delta_notional = qty * price - float(old.get("notional") or 0) if cumulative else delta * price
            if delta_fee < -EPS:
                raise ValueError("Cumulative fee regressed")
            if delta <= EPS and abs(delta_fee) <= EPS and abs(delta_notional) <= EPS:
                return dict(duplicate=True, qty=0.0, lot_id=lot_id)
            if retired and (delta <= EPS or delta_notional <= 0):
                raise ValueError("Reactivation requires positive confirmed fill quantity")
            previous = self._state()
            if retired:
                self.retired.pop(parent_key, None)
                self.epochs[parent_key] = epoch
            lot = self.lots.get(lot_id)
            if prior and (lot is not None or any(row["parent_key"] == parent_key for row in self.lots.values())):
                raise ValueError("Prior partial accumulator is valid only on initial baseline seed")
            if delta <= EPS:
                if not lot or lot["closed_qty"] > EPS:
                    raise ValueError("Late fee-only correction requires exchange cash reconciliation")
                remaining_notional = lot["entry"] * lot["remaining_qty"] + delta_notional
                if remaining_notional <= 0:
                    raise ValueError("Cumulative price correction has invalid notional")
                lot["entry"] = remaining_notional / lot["remaining_qty"]
                lot["notional"] += delta_notional
                lot["entry_fee"] += delta_fee
                lot["remaining_entry_fee"] += delta_fee
                lot["entry_fee_known"] = not _manual_cost(meta)
                lot["metadata"].update(meta)
            else:
                if delta_notional <= 0:
                    raise ValueError("Cumulative fill has invalid incremental notional")
                if lot is None:
                    lot = self.lots[lot_id] = dict(lot_id=lot_id, parent_key=parent_key,
                        parent_set_id=str(parent_set_id), child_set_id=child_set_id, set_id=child_set_id,
                        symbol=str(symbol), side=direction, strategy=str(strategy),
                        client_id=(str(client_id) + f"-e{epoch}" if client_id and epoch else str(client_id or lot_id)),
                        entry_client_id=str(client_id or lot_id),
                        order_id=str(order_id), opened_at=stamp, opened_qty=0.0, remaining_qty=0.0,
                        closed_qty=0.0, notional=0.0, entry=0.0, entry_fee=0.0,
                        remaining_entry_fee=0.0, metadata=meta, entry_fee_known=not _manual_cost(meta))
                    if prior:
                        lot.update(opened_qty=prior_qty, closed_qty=prior_qty,
                                   notional=prior_notional, roundtrip_result=prior,
                                   entry_fee=prior_entry_fee)
                elif lot["symbol"] != symbol or lot["side"] != direction or lot["parent_set_id"] != str(parent_set_id):
                    raise ValueError("DCA lot identity changed")
                remaining_notional = lot["entry"] * lot["remaining_qty"] + delta_notional
                lot["opened_qty"] += delta
                lot["remaining_qty"] += delta
                lot["notional"] += delta_notional
                lot["entry"] = remaining_notional / lot["remaining_qty"]
                lot["entry_fee"] += max(0.0, delta_fee)
                lot["remaining_entry_fee"] += max(0.0, delta_fee)
                lot["entry_fee_known"] = (not _manual_cost(meta) if cumulative
                                           else lot["entry_fee_known"] and not _manual_cost(meta))
                lot["metadata"].update(meta)
            if cumulative:
                self.orders[order_key] = dict(qty=qty, fee=fee, notional=qty * price,
                                              lot_id=lot_id, symbol=symbol, side=direction, parent_key=parent_key)
            elif event_key:
                self.entries[event_key] = True
            self._trim()
            self._commit(previous)
            return dict(duplicate=False, qty=max(0.0, delta), fee=max(0.0, delta_fee),
                        lot_id=lot_id, lot=deepcopy(lot))

    def allocate_close(self, parent_key: str, closed_qty: float, price: float, exit_fee: float,
                       close_fill_id: str, *, t: float | None = None, reported_net_pnl: float | None = None,
                       metadata: dict | None = None) -> dict:
        """Allocate one confirmed close across that parent's own remaining lots.

        Acknowledged duplicates return no rows; unacknowledged closes replay
        their original rows for idempotent journal recovery. Quantity keeps
        the physical venue's average cost for partial closes while retaining
        every child Set's own original price, fee and return evidence.
        """
        parent_key = str(parent_key)
        if not parent_key or not close_fill_id:
            raise ValueError("Confirmed close requires parent and stable fill id")
        closed_qty = _number(closed_qty, "closed quantity", positive=True)
        price, exit_fee = _number(price, "exit price", positive=True), _number(exit_fee, "exit fee")
        stamp = _number(time.time() if t is None else t, "close timestamp")
        if reported_net_pnl is not None and not math.isfinite(float(reported_net_pnl)):
            raise ValueError("Invalid reported net PnL")
        close_key = stable_key(self.connection, parent_key, close_fill_id)
        meta = dict(metadata or {})
        with self.lock:
            if close_key in self.pending:
                replayed = deepcopy(self.pending[close_key])
                rows = replayed["rows"]
                return {**replayed, "duplicate": True, "replay": True,
                        "baseRows": [row for row in rows if row["strategy"] != "dca"],
                        "childRows": [row for row in rows if row["strategy"] == "dca"]}
            if close_key in self.closes:
                saved = self.closes[close_key]
                return {**deepcopy(saved), "duplicate": True, "rows": [], "baseRows": [], "childRows": []}
            lots = sorted((lot for lot in self.lots.values()
                           if lot["parent_key"] == parent_key and lot["remaining_qty"] > EPS),
                          key=lambda lot: (lot["opened_at"], lot["lot_id"]))
            total = sum(lot["remaining_qty"] for lot in lots)
            if not lots or closed_qty > total + max(EPS, total * 1e-8):
                raise ValueError("Close exceeds confirmed DCA lot inventory")
            closed_qty = min(closed_qty, total)
            previous = self._state()
            ratio = closed_qty / total
            rows, used_qty, used_fee = [], 0.0, 0.0
            for i, lot in enumerate(lots):
                last = i == len(lots) - 1
                qty = closed_qty - used_qty if last else lot["remaining_qty"] * ratio
                allocated_exit_fee = exit_fee - used_fee if last else exit_fee * qty / closed_qty
                allocated_entry_fee = lot["remaining_entry_fee"] * qty / lot["remaining_qty"]
                used_qty += qty
                used_fee += allocated_exit_fee
                notion = qty * lot["entry"]
                gross = qty * (price - lot["entry"]) * (1 if lot["side"] == "LONG" else -1)
                total_fee = allocated_entry_fee + allocated_exit_fee
                combined_meta = {**lot["metadata"], **meta}
                measured = lot["entry_fee_known"] and not _manual_cost(meta)
                cost_pct = (total_fee / notion * 100.0 if measured else
                            normalize_position_cost_pct(combined_meta.get("position_cost_pct",
                                combined_meta.get("positionCostPct", POSITION_COST_PCT_DEFAULT))))
                cost_usdt = total_fee if measured else notion * cost_pct / 100.0
                source = ("live-exchange" if measured else
                          str(combined_meta.get("cost_source") or "manual-fallback") if _manual_cost(combined_meta)
                          else "manual-fallback")
                lot["remaining_qty"] = max(0.0, lot["remaining_qty"] - qty)
                lot["remaining_entry_fee"] = max(0.0, lot["remaining_entry_fee"] - allocated_entry_fee)
                lot["closed_qty"] += qty
                row = {**combined_meta, "conn": self.connection, "symbol": lot["symbol"], "side": lot["side"],
                    "t": stamp, "qty": qty, "entry": lot["entry"], "exit": price, "notional": notion,
                    "pnl": gross - cost_usdt, "pnl_pct": gross / notion, "grossPnl": gross,
                    "entry_fee": allocated_entry_fee, "exit_fee": allocated_exit_fee, "fee_total": total_fee,
                    "position_cost_pct": cost_pct, "cost_usdt": cost_usdt,
                    "estimated_cost_usdt": 0.0 if measured else cost_usdt,
                    "cost_source": source,
                    "hold_s": max(0.0, stamp-lot["opened_at"]), "partial": lot["remaining_qty"] > EPS,
                    "roundtrip_qty": lot["opened_qty"], "parent_set_id": lot["parent_set_id"],
                    "set_id": lot["child_set_id"], "child_set_id": lot["child_set_id"],
                    "parent_key": parent_key, "client_id": lot["client_id"],
                    "entry_client_id": lot.get("entry_client_id", lot["client_id"]),
                    "lot_id": lot["lot_id"],
                    "close_fill_id": stable_key(close_fill_id, lot["lot_id"]), "strategy": lot["strategy"],
                    "pack": "dca" if lot["strategy"] == "dca" else str(combined_meta.get("pack") or "general"),
                    "exchange_confirmed": True, "ours": True, "accounting_source": "confirmed-close-proportional-lots"}
                rows.append(row)
            if reported_net_pnl is not None:
                residual = float(reported_net_pnl) - sum(row["pnl"] for row in rows)
                total_notion = sum(row["notional"] for row in rows)
                assigned = 0.0
                for i, row in enumerate(rows):
                    correction = residual - assigned if i == len(rows)-1 else residual * row["notional"] / total_notion
                    assigned += correction
                    row["pnl"] += correction
                    row["grossPnl"] += correction
                    row["pnl_pct"] = row["grossPnl"] / row["notional"]
                    row["pnl_reconciliation"] = correction
            for row in rows:
                lot = self.lots[row["lot_id"]]
                old_trip = lot.get("roundtrip_result") or {}
                qty = float(old_trip.get("qty") or 0) + row["qty"]
                notional = float(old_trip.get("notional") or 0) + row["notional"]
                gross = float(old_trip.get("grossPnl") or 0) + row["grossPnl"]
                cost_usdt = float(old_trip.get("cost_usdt") or 0) + row["cost_usdt"]
                trip = {**row, "qty": qty, "notional": notional, "entry": notional / qty,
                    "exit": (float(old_trip.get("exit") or 0) * float(old_trip.get("qty") or 0)
                             + row["exit"] * row["qty"]) / qty,
                    "grossPnl": gross, "pnl_pct": gross / notional,
                    "pnl": float(old_trip.get("pnl") or 0) + row["pnl"],
                    "cost_usdt": cost_usdt, "position_cost_pct": cost_usdt / notional * 100.0,
                    "entry_fee": float(old_trip.get("entry_fee") or 0) + row["entry_fee"],
                    "exit_fee": float(old_trip.get("exit_fee") or 0) + row["exit_fee"],
                    "fee_total": float(old_trip.get("fee_total") or 0) + row["fee_total"],
                    "estimated_cost_usdt": float(old_trip.get("estimated_cost_usdt") or 0) + row["estimated_cost_usdt"],
                    "closeLegs": int(old_trip.get("closeLegs") or 0) + 1}
                if old_trip and old_trip.get("cost_source") != row["cost_source"]:
                    trip["cost_source"] = "mixed-cost"
                lot["roundtrip_result"] = trip
                row["roundtrip_result"] = deepcopy(trip)
            result = dict(duplicate=False, replay=False, outbox_id=close_key, t=stamp, metadata=meta,
                close_fill_id=str(close_fill_id), parent_key=parent_key, rows=rows,
                baseRows=[row for row in rows if row["strategy"] != "dca"],
                childRows=[row for row in rows if row["strategy"] == "dca"],
                qty=closed_qty, remainingQty=max(0.0, total-closed_qty),
                grossPnl=sum(row["grossPnl"] for row in rows), netPnl=sum(row["pnl"] for row in rows),
                entryFee=sum(row["entry_fee"] for row in rows), exitFee=exit_fee,
                feeTotal=sum(row["fee_total"] for row in rows))
            # The engine's trade journal stores the attributed rows. Ledger
            # receipts need only totals/idempotency, avoiding three copies of
            # every child close in every subsequent atomic checkpoint.
            self.closes[close_key] = {key: value for key, value in result.items()
                                      if key not in ("rows", "baseRows", "childRows")}
            # Pending rows survive a crash between inventory checkpoint and
            # publication. Acknowledged receipts retain only small totals.
            self.pending[close_key] = deepcopy({key: value for key, value in result.items()
                                               if key not in ("baseRows", "childRows")})
            if total - closed_qty <= EPS:
                self.retired[parent_key] = True
                self.lots = {key: lot for key, lot in self.lots.items() if lot["parent_key"] != parent_key}
                self.orders = {key: order for key, order in self.orders.items() if order["parent_key"] != parent_key}
            self._trim()
            self._commit(previous)
            return result
