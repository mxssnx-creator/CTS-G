import { useEffect, useState } from "react";
import { fetchSystemStatus, type SystemStatus } from "./system-settings";

export function useSystemStatus(conn: string) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController;
    setStatus(null);
    const pull = async () => {
      controller = new AbortController();
      const deadline = setTimeout(() => controller.abort(), 4000);
      const result = await fetchSystemStatus(conn, controller.signal);
      clearTimeout(deadline);
      if (active) {
        setStatus(result);
        timer = setTimeout(pull, 5000);
      }
    };
    void pull();
    return () => { active = false; clearTimeout(timer); controller?.abort(); };
  }, [conn]);
  return status;
}
