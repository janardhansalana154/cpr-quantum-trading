/**
 * NiftyCMP.tsx
 * Drop-in replacement for wherever Nifty CMP is displayed in src/main.tsx
 *
 * Changes from original:
 *   - Shows "Prev Close" label when market is closed (instead of showing 0 or stale price)
 *   - Colour-codes the change: green for positive, red for negative, gray for flat
 *   - Shows a pulsing green dot when market is live, gray dot when closed
 *   - Handles null/loading/error states cleanly
 */

import { useEffect, useState } from "react";

interface NiftyData {
  cmp: number | null;
  prev_close: number;
  change: number;
  change_pct: number;
  price_source: "live_ltp" | "prev_close" | "ohlc_close" | "stale_ltp";
  market_open: boolean;
  price_label: string;
}

interface StatusResponse {
  nifty: NiftyData;
}

function usePollStatus(intervalMs = 15000) {
  const [data, setData] = useState<NiftyData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function fetch_status() {
      try {
        const res = await fetch("/api/status");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json: StatusResponse = await res.json();
        if (!cancelled) {
          setData(json.nifty);
          setError(null);
        }
      } catch (e: unknown) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Fetch failed");
      }
    }

    fetch_status();
    const id = setInterval(fetch_status, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);

  return { data, error };
}

export function NiftyCMP() {
  const { data, error } = usePollStatus(15000);

  if (error) {
    return (
      <div style={styles.container}>
        <span style={styles.label}>NIFTY</span>
        <span style={{ color: "#888", fontSize: 13 }}>Unavailable</span>
      </div>
    );
  }

  if (!data || data.cmp === null) {
    return (
      <div style={styles.container}>
        <span style={styles.label}>NIFTY</span>
        <span style={{ color: "#888", fontSize: 18 }}>—</span>
      </div>
    );
  }

  const changeColor =
    data.change > 0 ? "#22c55e" : data.change < 0 ? "#ef4444" : "#888";
  const changeSign = data.change > 0 ? "+" : "";

  return (
    <div style={styles.container}>
      {/* Market status dot */}
      <span
        title={data.market_open ? "Market open" : "Market closed"}
        style={{
          ...styles.dot,
          background: data.market_open ? "#22c55e" : "#888",
          animation: data.market_open ? "pulse 1.5s infinite" : "none",
        }}
      />

      <span style={styles.label}>NIFTY</span>

      {/* CMP */}
      <span style={styles.price}>
        {data.cmp.toLocaleString("en-IN", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })}
      </span>

      {/* Change */}
      <span style={{ ...styles.change, color: changeColor }}>
        {changeSign}{data.change.toFixed(2)} ({changeSign}{data.change_pct.toFixed(2)}%)
      </span>

      {/* Source label — tells user where the price came from */}
      <span style={styles.sourceLabel}>
        {data.market_open ? "Live" : `Prev Close`}
      </span>

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0.4; }
        }
      `}</style>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "6px 12px",
    borderRadius: 8,
    background: "rgba(255,255,255,0.05)",
    border: "0.5px solid rgba(255,255,255,0.12)",
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    flexShrink: 0,
  },
  label: {
    fontSize: 12,
    fontWeight: 500,
    letterSpacing: "0.05em",
    color: "#aaa",
  },
  price: {
    fontSize: 20,
    fontWeight: 600,
    color: "#f0f0f0",
    fontVariantNumeric: "tabular-nums",
  },
  change: {
    fontSize: 13,
    fontVariantNumeric: "tabular-nums",
  },
  sourceLabel: {
    fontSize: 11,
    color: "#666",
    marginLeft: 2,
  },
};
