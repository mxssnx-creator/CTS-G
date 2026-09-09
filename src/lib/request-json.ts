/** Bounded reads: aborts also settle when a stalled transport ignores its signal. */
export function requestJson(url: string, signal?: AbortSignal, timeoutMs = 4000): Promise<unknown | null> {
  return new Promise((resolve) => {
    const controller = new AbortController();
    let settled = false;
    const finish = (value: unknown | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
      controller.abort();
      resolve(value);
    };
    const cancel = () => finish(null);
    const timer = setTimeout(cancel, timeoutMs);
    if (signal?.aborted) { cancel(); return; }
    signal?.addEventListener("abort", cancel, { once: true });
    void (async () => {
      try {
        const response = await fetch(url, { cache: "no-store", signal: controller.signal });
        const value: unknown = response.ok ? await response.json() : null;
        finish(value && typeof value === "object" && !Array.isArray(value) ? value : null);
      } catch { finish(null); }
    })();
  });
}

/** Healthy primary = one request. A stalled primary gets a fallback after 750 ms. */
export function requestPreferredJson<T>(
  primary: string,
  fallback: string,
  select: (value: unknown) => T | null,
  signal?: AbortSignal,
): Promise<T | null> {
  return new Promise((resolve) => {
    const controller = new AbortController();
    let settled = false;
    let fallbackStarted = false;
    let completed = 0;
    const finish = (value: T | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(hedge);
      clearTimeout(deadline);
      signal?.removeEventListener("abort", cancel);
      controller.abort();
      resolve(value);
    };
    const cancel = () => finish(null);
    const read = async (url: string) => {
      const value = await requestJson(url, controller.signal, 8000);
      if (settled) return;
      let selected: T | null = null;
      try { if (value) selected = select(value); } catch { /* malformed response */ }
      if (selected !== null) { finish(selected); return; }
      completed++;
      if (completed === 2) finish(null);
      else startFallback();
    };
    const startFallback = () => {
      if (settled || fallbackStarted) return;
      fallbackStarted = true;
      clearTimeout(hedge);
      void read(fallback);
    };
    const hedge = setTimeout(startFallback, 750);
    const deadline = setTimeout(cancel, 8000);
    if (signal?.aborted) { cancel(); return; }
    signal?.addEventListener("abort", cancel, { once: true });
    void read(primary);
  });
}
