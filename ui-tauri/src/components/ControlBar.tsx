import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ChatMessage, ProactiveNotif } from "../App";
import { MarrowState } from "../hooks/useMarrowBridge";
import ChatSection from "./ChatSection";
import NotifCard from "./NotifCard";
import Orb from "./Orb";

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
  onClearNotifs: () => void;
  lastSpoken: string;
  stats: { screens: number; speaks: number; actions: number };
  worldFacts: string[];
  audioTrace: string[];
}

const PILL_W = 40;
const PILL_H = 14;
const HOVER_W = 210;
const HOVER_H = 50;
const PANEL_W = 430;

export default function ControlBar({
  state,
  focus,
  micActive,
  transcript,
  messages,
  notifs,
  connected,
  onSend,
  onAsk,
  onDismissNotif,
  onClearNotifs,
  lastSpoken,
  stats,
  worldFacts,
  audioTrace,
}: Props) {
  const [hovered, setHovered] = useState(true);
  const [conversationOpen, setConversationOpen] = useState(false);
  const [intelTab, setIntelTab] = useState(false);
  const [notifContext, setNotifContext] = useState<string>("");
  const collapseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const currentNotif = useMemo(
    () => (notifs.length > 0 ? notifs[0] : null),
    [notifs]
  );

  const focusText = focus.app
    ? focus.title
      ? `${focus.app} - ${focus.title.slice(0, 36)}`
      : focus.app
    : "Desktop";

  useEffect(() => {
    if (currentNotif && !conversationOpen) {
      setHovered(true);
    }
  }, [currentNotif, conversationOpen]);

  useEffect(() => {
    if (!currentNotif || conversationOpen) return;
    const t = setTimeout(() => onDismissNotif(currentNotif.id), 6000);
    return () => clearTimeout(t);
  }, [currentNotif, conversationOpen, onDismissNotif]);

  function openConversation() {
    if (collapseTimer.current) clearTimeout(collapseTimer.current);
    setConversationOpen(true);
    setHovered(true);
  }

  function closeConversation() {
    setConversationOpen(false);
    setIntelTab(false);
    setNotifContext("");
  }

  function handleMouseEnter() {
    if (collapseTimer.current) clearTimeout(collapseTimer.current);
    setHovered(true);
  }

  function handleMouseLeave() {
    // Keep expanded for reliable click targets on Windows WebEngine.
  }

  const shellWidth = conversationOpen
    ? PANEL_W
    : currentNotif
      ? PANEL_W
      : hovered
        ? HOVER_W
        : PILL_W;
  const shellHeight = conversationOpen ? undefined : hovered ? HOVER_H : PILL_H;

  const busy = state === "thinking" || state === "acting";

  function handleSend(text: string) {
    if (notifContext) {
      onSend(`${text}\n\nNotification context: ${notifContext}`);
      setNotifContext("");
      return;
    }
    onSend(text);
  }

  return (
    <div className="select-none" style={{ width: shellWidth }} onMouseEnter={handleMouseEnter} onMouseLeave={handleMouseLeave}>
      <motion.div
        layout
        transition={{ duration: 0.2, ease: [0.3, 0, 0.2, 1] }}
        className="overflow-hidden"
        style={{
          width: shellWidth,
          height: shellHeight,
          background: "linear-gradient(180deg, rgba(17,23,34,0.96), rgba(12,17,28,0.95))",
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
          border: "1px solid rgba(86,100,129,0.72)",
          borderRadius: conversationOpen ? 14 : 6,
          boxShadow: "0 10px 24px rgba(0,0,0,0.36), 0 1px 0 rgba(255,255,255,0.06) inset",
        }}
      >
        {!hovered && !conversationOpen ? (
          <button
            onClick={() => setHovered(true)}
            className="w-full h-full flex items-center justify-center cursor-pointer"
            title="Open bar"
          >
            <div style={{ width: 28, height: 6, borderRadius: 3, background: "rgba(215,225,240,0.6)" }} />
          </button>
        ) : (
          <>
            <div
              className="flex items-center gap-2 px-2"
              style={{ height: 50 }}
            >
              <Orb state={state} connected={connected} size={9} />
              <span className="text-[10px] font-semibold" style={{ color: "rgba(230,236,248,0.95)", letterSpacing: "0.02em" }}>
                MARROW
              </span>

              {!conversationOpen && (
                <span className="text-[9px] truncate" style={{ color: "rgba(177,190,214,0.88)", maxWidth: 92 }}>
                  {focusText}
                </span>
              )}

              <div className="flex-1" />

              <span className="text-[8px]" style={{ color: connected ? "rgba(134,239,172,0.95)" : "rgba(252,165,165,0.95)" }}>
                {connected ? "online" : "offline"}
              </span>

              {conversationOpen ? (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    closeConversation();
                  }}
                  className="text-[10px] w-5 h-5 rounded-sm"
                  style={{ color: "rgba(202,214,236,0.9)", border: "1px solid rgba(87,101,130,0.8)", background: "rgba(255,255,255,0.04)" }}
                >
                  x
                </button>
              ) : (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onAsk();
                    openConversation();
                  }}
                  className="text-[9px] px-2 py-1 rounded-sm font-semibold"
                  style={{ color: "white", background: "rgba(37,99,235,0.9)", border: "1px solid rgba(96,165,250,0.45)" }}
                >
                  Ask
                </button>
              )}
            </div>

            {!conversationOpen && (
              <div className="px-2 pb-2 flex items-center justify-between text-[9px]" style={{ color: "rgba(166,180,208,0.9)" }}>
                <span>{busy ? "thinking" : state}</span>
                <span className="truncate" style={{ maxWidth: 116 }}>{focusText}</span>
              </div>
            )}

            {conversationOpen && (
              <div className="px-3 pb-3 flex flex-col gap-2">
                <div className="flex items-center gap-1 rounded-md p-1" style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(84,97,126,0.7)" }}>
                  <button
                    onClick={() => setIntelTab(false)}
                    className="text-[9px] px-2 py-1 rounded-sm font-semibold"
                    style={{ color: !intelTab ? "white" : "rgba(173,188,215,0.9)", background: !intelTab ? "rgba(37,99,235,0.9)" : "transparent" }}
                  >
                    Chat
                  </button>
                  <button
                    onClick={() => setIntelTab(true)}
                    className="text-[9px] px-2 py-1 rounded-sm font-semibold"
                    style={{ color: intelTab ? "white" : "rgba(173,188,215,0.9)", background: intelTab ? "rgba(37,99,235,0.9)" : "transparent" }}
                  >
                    Intel {notifs.length > 0 ? `(${notifs.length})` : ""}
                  </button>
                  <div className="flex-1" />
                  {notifs.length > 0 && (
                    <button
                      onClick={onClearNotifs}
                      className="text-[8px] px-2 py-1 rounded-sm"
                      style={{ color: "rgba(173,188,215,0.95)", border: "1px solid rgba(84,97,126,0.7)" }}
                    >
                      Clear
                    </button>
                  )}
                </div>

                {intelTab ? (
                  <div className="flex flex-col gap-2">
                    <Panel title="Watching">
                      <Line k="Focus" v={focusText} />
                      <Line k="Mic" v={micActive ? "Listening" : "Idle"} />
                      <Line k="Last" v={lastSpoken || "No response yet"} />
                      {transcript && <Line k="Heard" v={transcript.slice(0, 72)} />}
                    </Panel>
                    <Panel title="Runtime">
                      <Line k="Screens" v={String(stats.screens)} />
                      <Line k="Messages" v={String(stats.speaks)} />
                      <Line k="Actions" v={String(stats.actions)} />
                    </Panel>
                    {worldFacts.length > 0 && (
                      <Panel title="World">
                        {worldFacts.slice(0, 3).map((fact) => (
                          <div key={fact} className="text-[9px]" style={{ color: "rgba(196,209,232,0.95)" }}>
                            - {fact}
                          </div>
                        ))}
                      </Panel>
                    )}
                    {audioTrace.length > 0 && (
                      <Panel title="Trace">
                        {audioTrace.slice(0, 4).map((trace) => (
                          <div key={trace} className="text-[8px] truncate" style={{ color: "rgba(149,166,196,0.88)" }}>
                            {trace}
                          </div>
                        ))}
                      </Panel>
                    )}
                  </div>
                ) : (
                  <>
                    {notifContext && (
                      <div className="text-[9px] rounded-md px-2 py-1.5" style={{ background: "rgba(37,99,235,0.16)", border: "1px solid rgba(96,165,250,0.4)", color: "rgba(206,224,252,0.95)" }}>
                        Context linked from notification
                      </div>
                    )}
                    <ChatSection messages={messages} busy={busy} onSend={handleSend} />
                  </>
                )}
              </div>
            )}
          </>
        )}
      </motion.div>

      <AnimatePresence>
        {currentNotif && !conversationOpen && (
          <motion.div
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.18 }}
            className="mt-2"
            style={{ width: PANEL_W }}
          >
            <NotifCard
              notif={currentNotif}
              onDismiss={() => onDismissNotif(currentNotif.id)}
              onOpen={() => {
                setNotifContext(currentNotif.text);
                onDismissNotif(currentNotif.id);
                openConversation();
                setIntelTab(false);
              }}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md px-2.5 py-2 flex flex-col gap-1" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(84,97,126,0.65)" }}>
      <div className="text-[8px] uppercase font-semibold" style={{ color: "rgba(146,162,190,0.92)", letterSpacing: "0.06em" }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function Line({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-start justify-between gap-2 text-[9px]">
      <span style={{ color: "rgba(146,162,190,0.92)" }}>{k}</span>
      <span className="truncate text-right" style={{ color: "rgba(222,232,250,0.96)", maxWidth: 240 }}>{v}</span>
    </div>
  );
}
