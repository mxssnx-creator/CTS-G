import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { startPolling } from "@/lib/polling";
import {
  fetchConnections,
  readStoredConn,
  storeConn,
  type ConnCatalog,
  type ConnType,
} from "@/lib/connections";

type Ctx = {
  conn: ConnType;
  setConn: (v: ConnType) => void;
  catalog: ConnCatalog | null;
};

const ConnectionCtx = createContext<Ctx>({
  conn: "overall",
  setConn: () => undefined,
  catalog: null,
});

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [conn, setConnState] = useState<ConnType>("overall");
  const [catalog, setCatalog] = useState<ConnCatalog | null>(null);

  useEffect(() => {
    setConnState(readStoredConn());
    const poll = startPolling(async (signal) => {
      const c = await fetchConnections(signal);
      if (!signal.aborted && c) setCatalog(c);
    }, () => document.hidden ? 8000 : 4000);
    return poll.stop;
  }, []);

  const setConn = (v: ConnType) => {
    setConnState(v);
    storeConn(v);
  };

  const value = useMemo(() => ({ conn, setConn, catalog }), [conn, catalog]);
  return <ConnectionCtx.Provider value={value}>{children}</ConnectionCtx.Provider>;
}

// This hook intentionally shares the provider module so the context remains private.
// eslint-disable-next-line react-refresh/only-export-components
export function useConnection() {
  return useContext(ConnectionCtx);
}
