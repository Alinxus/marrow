import { motion } from "framer-motion";
import { ProactiveNotif } from "../App";

interface Props {
  notif: ProactiveNotif;
  onDismiss: () => void;
  onOpen?: () => void;
}

const URGENCY_COLORS: Record<number, string> = {
  1: "rgba(148,163,184,0.95)",
  2: "rgba(59,130,246,0.95)",
  3: "rgba(245,158,11,0.95)",
  4: "rgba(249,115,22,0.95)",
  5: "rgba(239,68,68,0.95)",
};

const URGENCY_LABELS: Record<number, string> = {
  1: "LOW",
  2: "NORMAL",
  3: "ELEVATED",
  4: "HIGH",
  5: "CRITICAL",
};

export default function NotifCard({ notif, onDismiss, onOpen }: Props) {
  const level = Math.min(5, Math.max(1, notif.urgency));
  const color = URGENCY_COLORS[level] ?? URGENCY_COLORS[2];
  const label = URGENCY_LABELS[level] ?? "NORMAL";
  const ageSeconds = Math.max(0, Math.floor((Date.now() - notif.createdAt) / 1000));
  const ageText = ageSeconds < 60 ? `${ageSeconds}s` : `${Math.floor(ageSeconds / 60)}m`;

  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.18 }}
      className="rounded-md p-2.5 flex flex-col gap-2"
      style={{
        background: "rgba(22,28,40,0.96)",
        border: "1px solid rgba(86,99,129,0.65)",
        boxShadow: "0 6px 18px rgba(0,0,0,0.35)",
      }}
    >
      <div className="flex items-center gap-2">
        <div className="w-1 h-4 rounded-sm" style={{ background: color }} />
        <span
          className="text-[8px] font-semibold px-1.5 py-0.5 rounded-sm"
          style={{ color, border: `1px solid ${color}`, background: "rgba(255,255,255,0.04)", letterSpacing: "0.04em" }}
        >
          {label}
        </span>
        <span className="text-[8px]" style={{ color: "rgba(170,183,210,0.85)" }}>
          {ageText}
        </span>
        <div className="flex-1" />
        <button
          onClick={onDismiss}
          className="text-[8px] px-1.5 py-0.5 rounded-sm"
          style={{ color: "rgba(185,197,224,0.95)", border: "1px solid rgba(95,109,139,0.7)", background: "rgba(255,255,255,0.04)" }}
        >
          Dismiss
        </button>
      </div>

      <p className="text-[11px] leading-snug" style={{ color: "rgba(228,236,252,0.95)" }}>
        {notif.text.length > 220 ? notif.text.slice(0, 220) + "..." : notif.text}
      </p>

      {onOpen && (
        <div className="flex">
          <button
            onClick={onOpen}
            className="text-[9px] px-2 py-1 rounded-sm font-semibold"
            style={{ color: "white", background: "rgba(37,99,235,0.9)", border: "1px solid rgba(96,165,250,0.45)" }}
          >
            Open In Chat
          </button>
        </div>
      )}
    </motion.div>
  );
}
