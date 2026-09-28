/**
 * Sidecar-down lane view from the synced Overall snapshot (public/live-stats.json).
 * Only rows attributed to the lane cross over; Overall aggregates (PF, sets,
 * activity, indications) and the other desk's positions and closes never do.
 */
export function laneFallbackRows(snap, type, id) {
  const mine = (row) => Boolean(row && typeof row === "object" && (row.connType === type || row.connection === id));
  const rows = (value) => (Array.isArray(value) ? value.filter(mine) : []);
  return { open: rows(snap?.open), closed: rows(snap?.closed) };
}
