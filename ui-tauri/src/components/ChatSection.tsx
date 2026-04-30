import { useRef, useEffect, useState, KeyboardEvent } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChatMessage } from "../App";

interface Props {
  messages: ChatMessage[];
  busy: boolean;
  onSend: (text: string) => void;
}

export default function ChatSection({ messages, busy, onSend }: Props) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function submit() {
    const t = input.trim();
    if (!t || busy) return;
    setInput("");
    onSend(t);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      {/* Message list */}
      {messages.length > 0 && (
        <div
          className="flex flex-col gap-1.5 max-h-52 overflow-y-auto pr-1"
          style={{ scrollbarWidth: "thin" }}
        >
          <AnimatePresence initial={false}>
            {messages.map((m) => (
              <motion.div
                key={m.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
                className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className="max-w-[82%] px-3 py-1.5 rounded-2xl text-[10px] leading-relaxed"
                  style={
                    m.role === "user"
                      ? {
                          background: "rgba(99,102,241,0.9)",
                          color: "white",
                          borderBottomRightRadius: 6,
                        }
                      : {
                          background: m.pending
                            ? "rgba(230,230,240,0.6)"
                            : "rgba(240,240,248,0.8)",
                          color: "rgba(20,20,36,0.9)",
                          borderBottomLeftRadius: 6,
                          border: "1px solid rgba(200,200,220,0.4)",
                        }
                  }
                >
                  {m.pending ? <PendingDots /> : m.text}
                </div>
              </motion.div>
            ))}
          </AnimatePresence>
          <div ref={bottomRef} />
        </div>
      )}

      {/* Input */}
      <div
        className="flex items-center gap-1.5 rounded-xl px-3 py-1.5"
        style={{
          background: "rgba(240,240,250,0.7)",
          border: "1px solid rgba(200,200,220,0.5)",
        }}
      >
        <input
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          placeholder={busy ? "working…" : "Ask or tell Marrow anything…"}
          className="flex-1 bg-transparent text-[10px] outline-none placeholder:text-[rgba(140,140,160,0.7)]"
          style={{ color: "rgba(20,20,36,0.9)", minWidth: 0 }}
        />
        {input.trim() && (
          <motion.button
            initial={{ scale: 0, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0, opacity: 0 }}
            onClick={submit}
            disabled={busy}
            className="w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 active:scale-90 transition-transform"
            style={{ background: "rgba(99,102,241,0.9)" }}
          >
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
              <path d="M1 7L7 1M7 1H2M7 1V6" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </motion.button>
        )}
      </div>
    </div>
  );
}

function PendingDots() {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setFrame((f) => (f + 1) % 4), 350);
    return () => clearInterval(t);
  }, []);
  return <span style={{ letterSpacing: "0.1em", opacity: 0.5 }}>{"·".repeat(frame + 1)}</span>;
}
