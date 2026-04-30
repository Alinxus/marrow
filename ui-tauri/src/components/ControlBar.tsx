import { useState, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { MarrowState } from "../hooks/useMarrowBridge";
import { ProactiveNotif, ChatMessage } from "../App";
import Orb from "./Orb";
import ChatSection from "./ChatSection";
import NotifCard from "./NotifCard";

interface Props {
  state: MarrowState;
  focus: { app: string; title: string };
  micActive: boolean;
  transcript: string;
  messages: ChatMessage[];
  notifs: ProactiveNotif[];
  connected: boolean;
  onSend: (text: string) => void;
  onAsk: () => void;
  onDismissNotif: (id: string) => void;
}

const STATE_LABEL: Record<MarrowState, string> = {
  idle: "ready",
  thinking: "thinking",
  acting: "working",
  speaking: "speaking",
  error: "error",
};

export default function ControlBar({
  state, focus, micActive, transcript, messages,
  notifs, connected, onSend, onAsk, onDismissNotif,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [dragging, setDragging] = useState(false);
  const dragStart = useRef<{ mx: number; my: number } | null>(null);
  const collapseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busy = state === "thinking" || state === "acting";

  // Auto-expand on notif or response
  useEffect(() => {
    if (notifs.length > 0) expand(true);
  }, [notifs.length]);


  function expand(transient = false) {
    if (collapseTimer.current) clearTimeout(collapseTimer.current);
    setExpanded(true);
    if (transient && !pinned) {
      collapseTimer.current = setTimeout(() => setExpanded(false), 6000);
    }
  }

  function collapse() {
    if (pinned) return;
    if (collapseTimer.current) clearTimeout(collapseTimer.current);
    setExpanded(false);
  }

  function handleMouseDown(e: React.MouseEvent) {
    if ((e.target as HTMLElement).closest("input,button,textarea")) return;
    setDragging(true);
  }

  function handleMouseUp() {
    setDragging(false);
  }

  const focusText = focus.app
    ? focus.title ? `${focus.app} · ${focus.title.slice(0, 22)}` : focus.app
    : "ready";

  return (
    <div
      className="flex flex-col w-full select-none"
      onMouseEnter={() => expand()}
      onMouseLeave={() => collapse()}
    >
      {/* ── Glass container ── */}
      <div
        style={{
          background: "rgba(255,255,255,0.82)",
          backdropFilter: "blur(28px) saturate(180%)",
          WebkitBackdropFilter: "blur(28px) saturate(180%)",
          borderRadius: 18,
          border: "1px solid rgba(255,255,255,0.7)",
          boxShadow:
            "0 2px 32px rgba(0,0,0,0.13), 0 1px 0 rgba(255,255,255,0.9) inset",
          overflow: "hidden",
        }}
      >
        {/* ── Top bar ── */}
        <div
          className="flex items-center gap-2 px-3 cursor-grab active:cursor-grabbing"
          style={{ height: 52, WebkitAppRegion: "drag" } as unknown as React.CSSProperties}
          onMouseDown={handleMouseDown}
          onMouseUp={handleMouseUp}
        >
          {/* Orb */}
          <Orb state={state} connected={connected} />

          {/* Title */}
          <span
            className="font-semibold tracking-widest text-[10px]"
            style={{ color: "rgba(12,12,22,0.85)", letterSpacing: "0.18em" }}
          >
            MARROW
          </span>

          {/* Focus */}
          <span
            className="text-[9px] truncate max-w-[100px]"
            style={{ color: "rgba(90,90,110,0.8)" }}
          >
            {focusText}
          </span>

          {/* State */}
          <AnimatePresence mode="wait">
            <motion.span
              key={state}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.18 }}
              className="text-[9px]"
              style={{ color: stateColor(state) }}
            >
              {busy ? <DotLoader /> : STATE_LABEL[state]}
            </motion.span>
          </AnimatePresence>

          <div className="flex-1" />

          {/* Mic dot */}
          <div
            className="w-2 h-2 rounded-full transition-all duration-300"
            style={{
              background: micActive
                ? "rgba(34,197,94,1)"
                : "rgba(150,150,170,0.35)",
              boxShadow: micActive ? "0 0 6px rgba(34,197,94,0.6)" : "none",
            }}
          />

          {/* Ask button */}
          <button
            onClick={(e) => { e.stopPropagation(); onAsk(); }}
            className="text-[9px] font-semibold px-3 py-1 rounded-full transition-all duration-150 active:scale-95"
            style={{ ...({ WebkitAppRegion: "no-drag" } as unknown as React.CSSProperties),
              background: "rgba(99,102,241,0.9)",
              color: "white",
              border: "1px solid rgba(99,102,241,0.4)",
              boxShadow: "0 1px 8px rgba(99,102,241,0.35)",
            }}
          >
            Ask
          </button>

          {/* Pin */}
          <button
            onClick={(e) => {
              e.stopPropagation();
              setPinned((p) => !p);
              if (!expanded) expand();
            }}
            className="text-[11px] w-5 h-5 flex items-center justify-center rounded transition-colors"
            style={{ color: pinned ? "rgba(99,102,241,0.9)" : "rgba(140,140,160,0.7)" }}
            title={pinned ? "Unpin" : "Pin open"}
          >
            {pinned ? "◆" : "◇"}
          </button>
        </div>

        {/* ── Expandable body ── */}
        <AnimatePresence>
          {expanded && (
            <motion.div
              key="body"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
              style={{ overflow: "hidden" }}
            >
              <div className="px-3 pb-3 flex flex-col gap-2">
                {/* Notifications */}
                <AnimatePresence>
                  {notifs.map((n) => (
                    <NotifCard
                      key={n.id}
                      notif={n}
                      onDismiss={() => onDismissNotif(n.id)}
                    />
                  ))}
                </AnimatePresence>

                {/* Transcript hint */}
                {transcript && (
                  <p className="text-[8px] truncate px-1" style={{ color: "rgba(80,80,100,0.6)" }}>
                    heard: "{transcript.slice(0, 80)}"
                  </p>
                )}

                {/* Chat */}
                <ChatSection
                  messages={messages}
                  busy={busy}
                  onSend={onSend}
                />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}

function DotLoader() {
  const [dots, setDots] = useState(1);
  useEffect(() => {
    const t = setInterval(() => setDots((d) => (d % 3) + 1), 380);
    return () => clearInterval(t);
  }, []);
  return <span>{"·".repeat(dots)}</span>;
}

function stateColor(state: MarrowState): string {
  switch (state) {
    case "thinking": return "rgba(234,179,8,0.9)";
    case "acting":   return "rgba(99,102,241,0.9)";
    case "speaking": return "rgba(34,197,94,0.9)";
    case "error":    return "rgba(239,68,68,0.9)";
    default:         return "rgba(140,140,160,0.7)";
  }
}
