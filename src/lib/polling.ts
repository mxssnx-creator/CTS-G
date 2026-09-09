/** One request at a time, one timer, and at most one queued manual refresh. */
export function startPolling(task: (signal: AbortSignal) => Promise<unknown>, delay: () => number) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let running = false;
  let queued = false;
  const refresh = () => {
    if (controller.signal.aborted) return;
    clearTimeout(timer);
    if (running) { queued = true; return; }
    running = true;
    void (async () => {
      try { await task(controller.signal); }
      catch { /* The next poll retries; transient failure must not end the loop. */ }
      finally {
        running = false;
        if (!controller.signal.aborted) {
          timer = setTimeout(refresh, queued ? 0 : delay());
          queued = false;
        }
      }
    })();
  };
  refresh();
  return {
    refresh,
    stop: () => { controller.abort(); clearTimeout(timer); },
  };
}
