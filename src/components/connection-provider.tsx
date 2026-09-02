import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
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
    let alive = true;
    const pull = async () => {
      const c = await fetchConnections();
      if (alive && c) setCatalog(c);
    };
    let timer: ReturnType<typeof setTimeout> | null = null;
    const chain = async () => {
      await pull();
      if (alive) timer = setTimeout(chain, 4000);
    };
    void chain();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
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
