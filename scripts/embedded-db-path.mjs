/**
 * Resolve managed Linux persistence without silently opening an empty memory DB.
 * @param {Record<string, string | undefined>} env
 */
export function embeddedDbDataDir(env) {
  const state = env.CTS_STATE_DIR?.trim();
  const dataDir = env.CTS_DB_DIR?.trim() || (state ? `${state.replace(/\/+$/, '')}/db` : undefined);
  if (dataDir && (!dataDir.startsWith('/') || dataDir.includes('\0'))) {
    throw new Error('CTS_DB_DIR / CTS_STATE_DIR must name an absolute filesystem directory');
  }
  if (!dataDir && env.CTS_REQUIRE_PERSISTENT_DB === '1') {
    throw new Error('Managed desk requires CTS_DB_DIR or CTS_STATE_DIR; refusing an empty in-memory database');
  }
  return dataDir;
}
