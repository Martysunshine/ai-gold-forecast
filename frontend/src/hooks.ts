import { useCallback, useEffect, useRef, useState } from "react";
import type { Conn } from "./types";

/** Width of an element, kept in sync with layout changes (used to choose axis tick counts). */
export function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

/**
 * Poll ``fn`` every ``ms`` (paused while the tab is hidden). Data is tied to ``key`` so a result for one instrument/range can never
 * be shown for another. A failed poll keeps the last good data and reports "reconnecting" (or "offline" with no network).
 */
export function usePoll<T>(fn: () => Promise<T>, ms: number, key: string, enabled = true) {
  const [state, setState] = useState<{ key: string; data: T | null; error: string | null; lastOk: number | null }>({ key, data: null, error: null, lastOk: null });
  const [conn, setConn] = useState<Conn>("ok");
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const run = useCallback(async () => {
    try {
      const data = await fnRef.current();
      setState({ key, data, error: null, lastOk: Date.now() });
      setConn("ok");
    } catch (e) {
      setState((s) => ({ ...(s.key === key ? s : { key, data: null, lastOk: null }), error: e instanceof Error ? e.message : "Request failed" }));
      setConn(typeof navigator !== "undefined" && navigator.onLine === false ? "offline" : "reconnecting");
    }
  }, [key]);
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    const tick = () => { if (!stopped && !document.hidden) void run(); };
    void run();
    const id = window.setInterval(tick, ms);
    const vis = () => { if (!document.hidden) tick(); };
    document.addEventListener("visibilitychange", vis);
    window.addEventListener("online", tick);
    return () => { stopped = true; window.clearInterval(id); document.removeEventListener("visibilitychange", vis); window.removeEventListener("online", tick); };
  }, [run, ms, enabled]);
  const cur = state.key === key ? state : { key, data: null as T | null, error: null as string | null, lastOk: null as number | null };
  return { data: cur.data, error: cur.error, lastOk: cur.lastOk, conn, refresh: run };
}
