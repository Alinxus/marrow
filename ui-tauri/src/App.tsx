import { useState, useCallback, useRef } from "react";
import ControlBar from "./components/ControlBar";
import { useMarrowBridge, MarrowState } from "./hooks/useMarrowBridge";

export interface ProactiveNotif {
  id: string;
  text: string;
  urgency: number;
  createdAt: number;
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
  const [lastSpoken, setLastSpoken] = useState("");
  const [audioTrace, setAudioTrace] = useState<string[]>([]);
  const [worldFacts, setWorldFacts] = useState<string[]>([]);
  const [stats, setStats] = useState({ screens: 0, speaks: 0, actions: 0 });
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
      setLastSpoken(text);
      const id = `notif-${Date.now()}`;
      setNotifs((prev) => [{ id, text, urgency, createdAt: Date.now() }, ...prev].slice(0, 6));
    },
    toast_requested: (data) => {
      const arr = Array.isArray(data) ? data : ["", String(data || ""), 2];
      const text = String(arr[1] || arr[0] || "");
      const urgency = Number(arr[2]) || 2;
      if (!text) return;
      const id = `toast-${Date.now()}`;
      setNotifs((prev) => [{ id, text, urgency, createdAt: Date.now() }, ...prev].slice(0, 6));
    },
    notify: (data) => {
      const arr = Array.isArray(data) ? data : ["", String(data || "")];
      const text = String(arr[1] || arr[0] || "");
      if (!text) return;
      const id = `notify-${Date.now()}`;
      setNotifs((prev) => [{ id, text, urgency: 2, createdAt: Date.now() }, ...prev].slice(0, 6));
    },
    audio_debug: (data) => {
      const line = String(data || "").trim();
      if (!line) return;
      setAudioTrace((prev) => [line, ...prev].slice(0, 6));
    },
    world_model_updated: (data) => {
      try {
        const parsed = typeof data === "string" ? JSON.parse(data) : data;
        if (!Array.isArray(parsed)) return;
        const facts = parsed
          .slice(0, 6)
          .map((item: any) => Array.isArray(item) ? String(item[1] || "") : String(item?.content || ""))
          .filter(Boolean);
        setWorldFacts(facts);
      } catch {
        // ignore malformed payload
      }
    },
    stats_updated: (data) => {
      try {
        const parsed = typeof data === "string" ? JSON.parse(data) : data;
        setStats({
          screens: Number(parsed?.screens) || 0,
          speaks: Number(parsed?.speaks) || 0,
          actions: Number(parsed?.actions) || 0,
        });
      } catch {
        // ignore malformed payload
      }
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

  const clearNotifs = useCallback(() => {
    setNotifs([]);
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
      onClearNotifs={clearNotifs}
      lastSpoken={lastSpoken}
      stats={stats}
      worldFacts={worldFacts}
      audioTrace={audioTrace}
    />
  );
}
