/**
 * One poll chain for a desk view. At most one request is in flight. The next request is scheduled only after the
 * current one settles, so slow responses never stack up. kick() asks for a fresh request now; if one is already in
 * flight, exactly one more runs right after it, never a second one in parallel. A failed or null response is not
 * published, so the view keeps its last good value. stop() aborts the request in flight and drops late results.
 */

export type PollOptions<T> = {
  /** One request. Resolve with the value, or with null when it failed. */
  fetch: (signal: AbortSignal) => Promise<T | null>;
  /** Called only with a value that arrived while the chain was running. */
  onData: (value: T) => void;
  /** Gap between the end of one request and the start of the next. */
  intervalMs: number;
  /** Called with the count of consecutive failures; the count resets on the next value. */
  onFailure?: (failures: number) => void;
};

export type Poller = {
  start: () => void;
  kick: () => void;
  stop: () => void;
  /** Requests started so far. For tests and diagnostics. */
  readonly requests: number;
};

export function createPoller<T>(opts: PollOptions<T>): Poller {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inflight: Promise<void> | null = null;
  let controller: AbortController | null = null;
  let stopped = true;
  let kickedWhileBusy = false;
  let failures = 0;
  let requests = 0;

  const clearTimer = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  const schedule = (ms: number) => {
    if (stopped) return;
    clearTimer();
    timer = setTimeout(() => {
      timer = null;
      void run();
    }, ms);
  };

  const run = (): Promise<void> => {
    if (stopped) return Promise.resolve();
    if (inflight) {
      kickedWhileBusy = true;
      return inflight;
    }
    clearTimer();
    requests += 1;
    const ac = new AbortController();
    controller = ac;
    inflight = (async () => {
      let value: T | null = null;
      try {
        value = await opts.fetch(ac.signal);
      } catch {
        value = null;
      }
      inflight = null;
      controller = null;
      if (stopped) return;
      if (value !== null) {
        failures = 0;
        opts.onData(value);
      } else {
        failures += 1;
        opts.onFailure?.(failures);
      }
      const again = kickedWhileBusy;
      kickedWhileBusy = false;
      schedule(again ? 0 : opts.intervalMs);
    })();
    return inflight;
  };

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      void run();
    },
    kick() {
      if (stopped) return;
      clearTimer();
      void run();
    },
    stop() {
      stopped = true;
      clearTimer();
      kickedWhileBusy = false;
      controller?.abort();
    },
    get requests() {
      return requests;
    },
  };
}

/**
 * Write-after-read guard for a view that both polls and saves. A poll takes a snapshot when it starts; a save marks
 * the gate before it writes. A poll whose snapshot is older than the latest mark must not overwrite what the user saved.
 */
export function createSaveGate() {
  let seq = 0;
  return {
    /** A poll records this when it starts. */
    snapshot: (): number => seq,
    /** A save calls this before it writes. */
    mark: (): number => {
      seq += 1;
      return seq;
    },
    /** True only when no save started since the snapshot was taken. */
    fresh: (snap: number): boolean => snap === seq,
  };
}
