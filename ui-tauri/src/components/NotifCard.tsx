import { motion } from "framer-motion";
import { ProactiveNotif } from "../App";

interface Props {
  notif: ProactiveNotif;
  onDismiss: () => void;
}

const URGENCY_COLORS: Record<number, string> = {
  1: "rgba(148,163,184,0.8)",
  2: "rgba(99,102,241,0.8)",
  3: "rgba(234,179,8,0.9)",
  4: "rgba(249,115,22,0.9)",
  5: "rgba(239,68,68,0.9)",
};

export default function NotifCard({ notif, onDismiss }: Props) {
  const color = URGENCY_COLORS[Math.min(5, Math.max(1, notif.urgency))] ?? URGENCY_COLORS[2];

  return (
    <motion.div
      initial={{ opacity: 0, y: -8, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -6, scale: 0.96 }}
      transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
      className="flex items-start gap-2 rounded-xl px-3 py-2"
      style={{
        background: "rgba(245,247,255,0.85)",
        border: "1px solid rgba(200,200,230,0.5)",
        backdropFilter: "blur(12px)",
      }}
    >
      {/* Color strip */}
      <div
        className="w-1 self-stretch rounded-full flex-shrink-0"
        style={{ background: color, minHeight: 16 }}
      />

      <div className="flex-1 min-w-0">
        <p
          className="text-[8px] font-semibold mb-0.5"
          style={{ color, letterSpacing: "0.06em" }}
        >
          INSIGHT
        </p>
        <p
          className="text-[10px] leading-snug"
          style={{ color: "rgba(30,30,50,0.85)" }}
        >
          {notif.text.length > 120 ? notif.text.slice(0, 120) + "…" : notif.text}
        </p>
      </div>

      <button
        onClick={onDismiss}
        className="text-[12px] leading-none flex-shrink-0 mt-0.5 transition-opacity hover:opacity-100 opacity-40"
        style={{ color: "rgba(80,80,100,1)" }}
      >
        ×
      </button>
    </motion.div>
  );
}
