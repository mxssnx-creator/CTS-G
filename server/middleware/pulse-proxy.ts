/** Provider-independent production reverse proxy for the local Pulse sidecar. */

const PULSE_PATHS = new Set([
  "/stats.json",
  "/stats",
  "/results-export.json",
  "/results-export.md",
  "/control.json",
  "/control",
  "/connections.json",
  "/connection.json",
  "/config.json",
  "/config",
  "/universe.json",
  "/live-stats.json",
  "/hist-calc.json",
  "/user-presets.json",
]);

interface PulseProxyEvent {
  url: URL;
  req: {
    method: string;
    headers: Headers;
    arrayBuffer: () => Promise<ArrayBuffer>;
  };
}

export default async function pulseProxyMiddleware(
  event: PulseProxyEvent,
  next: () => unknown | Promise<unknown>,
): Promise<unknown> {
  if (!PULSE_PATHS.has(event.url.pathname)) return next();

  const base = (process.env.PULSE_URL || "http://127.0.0.1:3015").replace(/\/$/, "");
  const method = (event.req.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "POST", "OPTIONS"].includes(method)) {
    return Response.json({ ok: false, detail: "method not allowed" }, { status: 405 });
  }
  const headers = new Headers(event.req.headers);
  const contentLength = Number(headers.get("content-length") || "0");
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > 1_048_576) {
    return Response.json({ ok: false, detail: "request too large" }, { status: 413 });
  }
  const origin = headers.get("origin");
  if (method === "POST" && origin && origin !== event.url.origin) {
    return Response.json({ ok: false, detail: "cross-origin write denied" }, { status: 403 });
  }
  headers.delete("host");
  headers.delete("content-length");
  try {
    const body = method === "GET" || method === "HEAD" ? undefined : await event.req.arrayBuffer();
    if (body && body.byteLength > 1_048_576) {
      return Response.json({ ok: false, detail: "request too large" }, { status: 413 });
    }
    const response = await fetch(`${base}${event.url.pathname}${event.url.search}`, {
      method,
      headers,
      body,
      signal: AbortSignal.timeout(method === "GET" ? 10_000 : 35_000),
    });
    const outHeaders = new Headers(response.headers);
    outHeaders.delete("content-length");
    outHeaders.delete("content-encoding");
    outHeaders.set("cache-control", "no-store");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: outHeaders,
    });
  } catch (error) {
    return Response.json(
      {
        ok: false,
        running: false,
        halted: true,
        haltReason: "sidecar-down",
        detail: error instanceof Error ? error.message : "Pulse sidecar unavailable",
        code: error instanceof Error && error.cause && typeof error.cause === "object" && "code" in error.cause ? String(error.cause.code) : "upstream-unavailable",
      },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }
}
