import { motion, AnimatePresence } from "framer-motion";
import { MarrowState } from "../hooks/useMarrowBridge";

const ORB_COLORS: Record<MarrowState, string[]> = {
  idle:     ["rgba(180,180,200,0.65)", "rgba(200,200,220,0.36)"],
  thinking: ["rgba(234,179,8,0.9)",   "rgba(251,191,36,0.5)"],
  acting:   ["rgba(99,102,241,0.95)", "rgba(139,92,246,0.5)"],
  speaking: ["rgba(34,197,94,0.9)",   "rgba(74,222,128,0.5)"],
  error:    ["rgba(239,68,68,0.9)",   "rgba(252,165,165,0.5)"],
};

interface Props {
  state: MarrowState;
  connected: boolean;
  size?: number;
}

export default function Orb({ state, connected, size = 10 }: Props) {
  const [core, glow] = ORB_COLORS[state];
  const pulse = state === "thinking" || state === "acting" || state === "speaking";

  return (
    <div className="relative flex items-center justify-center" style={{ width: size, height: size }}>
      {/* Pulse ring */}
      <AnimatePresence>
        {pulse && (
          <motion.div
            key="ring"
            className="absolute rounded-full"
            style={{ width: size * 2.4, height: size * 2.4, background: glow }}
            initial={{ opacity: 0.7, scale: 0.8 }}
            animate={{ opacity: 0, scale: 2.2 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 1.2, repeat: Infinity, ease: "easeOut" }}
          />
        )}
      </AnimatePresence>

      {/* Core */}
      <motion.div
        className="rounded-full"
        style={{
          width: size,
          height: size,
          background: connected ? core : "rgba(150,150,170,0.3)",
          boxShadow: connected ? `0 0 ${size * 0.8}px ${glow}` : "none",
          border: "1px solid rgba(255,255,255,0.65)",
        }}
        animate={{ scale: pulse ? [1, 1.15, 1] : 1 }}
        transition={{ duration: 0.9, repeat: pulse ? Infinity : 0, ease: "easeInOut" }}
      />
    </div>
  );
}
