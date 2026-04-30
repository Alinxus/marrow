import { useEffect, useRef, useCallback } from "react";

const WS_URL = "ws://127.0.0.1:7734";
const RECONNECT_DELAY = 1500;

export type MarrowState = "idle" | "thinking" | "acting" | "speaking" | "error";

export interface BridgeMessage {
  type: string;
  data: any;
}

type Handler = (data: any) => void;

export function useMarrowBridge(handlers: Record<string, Handler>) {
  const wsRef = useRef<WebSocket | null>(null);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  const send = useCallback((action: string, payload?: any) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "command", action, payload }));
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let retryTimer: ReturnType<typeof setTimeout>;

    function connect() {
      if (!alive) return;
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onmessage = (e) => {
        try {
          const msg: BridgeMessage = JSON.parse(e.data);
          handlersRef.current[msg.type]?.(msg.data);
        } catch {}
      };

      ws.onclose = () => {
        if (alive) retryTimer = setTimeout(connect, RECONNECT_DELAY);
      };

      ws.onerror = () => ws.close();
    }

    connect();
    return () => {
      alive = false;
      clearTimeout(retryTimer);
      wsRef.current?.close();
    };
  }, []);

  return { send };
}
