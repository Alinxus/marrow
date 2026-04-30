import { useState, useCallback, useRef } from "react";
import { AnimatePresence } from "framer-motion";
import ControlBar from "./components/ControlBar";
import { useMarrowBridge, MarrowState } from "./hooks/useMarrowBridge";

export interface ProactiveNotif {
  id: string;
  text: string;
  urgency: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "marrow";
  text: string;
  pending?: boolean;
}

export default function App() {
  const [state, setState] = useState<MarrowState>("idle");
  const [focus, setFocus] = useState({ app: "", title: "" });
  const [micActive, setMicActive] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [notifs, setNotifs] = useState<ProactiveNotif[]>([]);
  const [connected, setConnected] = useState(false);
  const pendingIdRef = useRef<string | null>(null);

  const addMessage = useCallback((role: "user" | "marrow", text: string, pending = false) => {
    const id = `${Date.now()}-${Math.random()}`;
    setMessages((prev) => [...prev, { id, role, text, pending }]);
    return id;
  }, []);

  const { send } = useMarrowBridge({
    state_changed: (data) => setState(data as MarrowState),
    focus_changed: (data) => {
      const [app, title] = Array.isArray(data) ? data : [data, ""];
      setFocus({ app, title });
    },
    mic_active: (data) => setMicActive(Boolean(data)),
    transcript_heard: (data) => setTranscript(String(data || "")),
    message_spoken: (data) => {
      const text = Array.isArray(data) ? data[0] : String(data || "");
      const urgency = Array.isArray(data) ? Number(data[1]) || 2 : 2;
      const id = `notif-${Date.now()}`;
      setNotifs((prev) => [{ id, text, urgency }, ...prev].slice(0, 4));
    },
    task_response: (data) => {
      const text = String(data || "Done.");
      setMessages((prev) => {
        if (pendingIdRef.current) {
          return prev.map((m) =>
            m.id === pendingIdRef.current ? { ...m, text, pending: false } : m
          );
        }
        return [...prev, { id: `${Date.now()}`, role: "marrow", text, pending: false }];
      });
      pendingIdRef.current = null;
    },
    // Open WebSocket = backend is up
    open: () => setConnected(true),
  });

  // Also detect connection via any message
  const handleSend = useCallback(
    (text: string) => {
      setConnected(true);
      addMessage("user", text);
      const pid = `pending-${Date.now()}`;
      pendingIdRef.current = pid;
      setMessages((prev) => [
        ...prev,
        { id: pid, role: "marrow", text: "…", pending: true },
      ]);
      send("text_task_submitted", text);
    },
    [send, addMessage]
  );

  const handleAsk = useCallback(() => send("ask_requested"), [send]);

  const dismissNotif = useCallback((id: string) => {
    setNotifs((prev) => prev.filter((n) => n.id !== id));
  }, []);

  return (
    <ControlBar
      state={state}
      focus={focus}
      micActive={micActive}
      transcript={transcript}
      messages={messages}
      notifs={notifs}
      connected={connected}
      onSend={handleSend}
      onAsk={handleAsk}
      onDismissNotif={dismissNotif}
    />
  );
}
