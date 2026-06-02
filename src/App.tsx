import { useState, useEffect, useRef } from "react";
import {
  Play, Pause, RotateCcw, ShieldCheck, Sliders,
  Terminal, Settings2, Bell, AlertTriangle,
  CheckCircle, PlusCircle, RefreshCw, Layers, Sparkles, TrendingUp, HelpCircle
} from "lucide-react";
import { ResponsiveContainer, ComposedChart, XAxis, YAxis, Tooltip, CartesianGrid, Line } from "recharts";

// =========================================================
// TYPES
// =========================================================
interface CPRLevels {
  pivot: number;
  bc: number;
  tc: number;
  r1: number;
  s1: number;
}

interface SystemLog {
  timestamp: string;
  level: "INFO" | "WARNING" | "ERROR" | "SUCCESS" | "STRATEGY";
  msg: string;
}

interface SetupState {
  name: string;
  state: number;
  barsElapsed: number;
  retestHigh: number | null;
  retestLow: number | null;
  confirmationHigh: number | null;
  confirmationLow: number | null;
}

interface Candle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  index: number;
}

interface SimulatedTrade {
  id: string;
  setupName: string;
  type: "CE" | "PE";
  strikePrice: number;
  entryPrice: number;
  exitPrice: number | null;
  stopLossIndex: number;
  takeProfitIndex: number;
  pnl: number;
  status: "OPEN" | "CLOSED_TP" | "CLOSED_SL" | "CLOSED_MANUAL";
  entryTime: string;
  exitTime: string | null;
}

// RULE 4 + RULE 5: Strong typing for data source and market status
interface LiveSystemStatus {
  // RULE 4: DATA SOURCE — only these four values are valid
  data_source: "UPSTOX LIVE" | "DISCONNECTED" | "HISTORICAL REPLAY" | "SIMULATION";
  last_live_candle_time: string | null;
  websocket_status: "Connected" | "Disconnected";
  // RULE 5: CMP with source and timestamp
  nifty_ltp: number | null;
  cmp_source: "UPSTOX_LTP" | "DISCONNECTED";
  cmp_last_updated: string | null;
  cpr_levels: CPRLevels | null;
  trading_mode: string;
  // RULE 1: Market status
  market_status: "OPEN" | "CLOSED";
  market_open: boolean;
  market_detail: {
    weekday: string;
    current_ist: string;
    is_holiday: boolean;
  };
  // RULE 2: Real trade counts from DB
  daily_summary: {
    trade_count: number;
    max_trades: number;
    realized_pnl: number;
    is_blocked: boolean;
  } | null;
  // RULE 6: Strategy allowed
  strategy_allowed: boolean;
}

// =========================================================
// DEMO CANDLES — used only for the visual demo player
// Labels make clear this is a DEMO, not live data
// =========================================================
const DEMO_CANDLES: Candle[] = [
  { time: "09:15", open: 24050, high: 24080, low: 24040, close: 24065, index: 0 },
  { time: "09:20", open: 24065, high: 24075, low: 24050, close: 24058, index: 1 },
  { time: "09:25", open: 24058, high: 24062, low: 24020, close: 24025, index: 2 },
  { time: "09:30", open: 24025, high: 24045, low: 24018, close: 24038, index: 3 },
  { time: "09:35", open: 24038, high: 24050, low: 24030, close: 24044, index: 4 },
  { time: "09:40", open: 24044, high: 24055, low: 24038, close: 24042, index: 5 },
  { time: "09:45", open: 24042, high: 24068, low: 24040, close: 24060, index: 6 },
  { time: "09:50", open: 24060, high: 24080, low: 24055, close: 24072, index: 7 },
  { time: "09:55", open: 24072, high: 24085, low: 24068, close: 24078, index: 8 },
  { time: "10:00", open: 24078, high: 24095, low: 24074, close: 24088, index: 9 },
];

// Real trade from the backend DB (not simulated)
interface DbTrade {
  id: number;
  setup_name: string;
  trade_type: string;
  option_symbol: string;
  strike_price: number;
  option_type: string;
  entry_price: number;
  exit_price: number | null;
  stop_loss: number;
  take_profit: number;
  lots: number;
  status: string;
  pnl: number;
  entry_time: string;
  exit_time: string | null;
  is_paper: boolean;
}

// =========================================================
// CONSTANTS
// =========================================================
const MAX_TRADES_PER_DAY = 2;  // mirrors backend settings.MAX_DAILY_TRADES

export default function App() {
  const [tradingMode, setTradingMode] = useState<"paper" | "live">("paper");
  const [isPlaying, setIsPlaying] = useState(false);
  const [speedMs, setSpeedMs] = useState(1500);
  const [currentIdx, setCurrentIdx] = useState(0);
  const [currentTime, setCurrentTime] = useState("--:--:--");

  const [upstoxStatus, setUpstoxStatus] = useState({
    connected: false,
    token_status: "Disconnected",
    expiry_status: "Access token not fetched",
    last_authenticated: null as string | null,
    token_preview: "None",
    loading: true,
    calculated_redirect_uri: "",
    upstox_api_key: "",
    env_redirect_uri: "",
    is_localhost_fallback: false,
  });
  const authPopupRef = useRef<Window | null>(null);
  const authStatusPollRef = useRef<number | null>(null);

  // ── RULE 1/4/5: Live status from backend — all fields initialized to DISCONNECTED/CLOSED
  const [liveStatus, setLiveStatus] = useState<LiveSystemStatus>({
    data_source: "DISCONNECTED",
    last_live_candle_time: null,
    websocket_status: "Disconnected",
    nifty_ltp: null,                   // RULE 5: null until live data arrives, never a fake value
    cmp_source: "DISCONNECTED",
    cmp_last_updated: null,
    cpr_levels: null,
    trading_mode: "paper",
    market_status: "CLOSED",           // RULE 1: default to closed
    market_open: false,
    market_detail: { weekday: "--", current_ist: "--", is_holiday: false },
    daily_summary: null,
    strategy_allowed: false,
  });

  // Real trades from DB — fetched from /api/trades every 15s
  const [liveTradesFromDB, setLiveTradesFromDB] = useState<DbTrade[]>([]);

  const fetchLiveTrades = async () => {
    try {
      const res = await fetch("/api/trades");
      if (res.ok) {
        const data = await res.json();
        setLiveTradesFromDB(data.trades || []);
      }
    } catch {
      // backend unreachable — keep stale state
    }
  };

  const handleManualCloseTrade = async (trade: DbTrade) => {
    const suggested = trade.entry_price.toFixed(2);
    const promptValue = window.prompt(
      `Mark ${trade.option_symbol} closed manually. Enter exit premium (option price):`,
      suggested
    );
    if (promptValue === null) {
      return;
    }

    const trimmed = promptValue.trim();
    if (trimmed.length === 0) {
      addLog("WARNING", "Manual close cancelled: exit price required.");
      return;
    }

    const exitPrice = parseFloat(trimmed);
    if (Number.isNaN(exitPrice)) {
      addLog("ERROR", "Manual close failed: invalid exit price.");
      return;
    }

    try {
      const response = await fetch(`/api/trades/${trade.id}/manual-close`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exit_price: exitPrice }),
      });
      if (!response.ok) {
        const error = await response.text();
        addLog("ERROR", `Manual close failed: ${error}`);
        return;
      }
      addLog("SUCCESS", `Trade ${trade.option_symbol} marked CLOSED_MANUAL.`);
      fetchLiveTrades();
      fetchLiveStatus();
    } catch (e: any) {
      addLog("ERROR", `Manual close error: ${e?.message || e}`);
    }
  };

  const handleToggleTradingPause = async (pause: boolean) => {
    try {
      const endpoint = pause ? "/api/trading/pause" : "/api/trading/resume";
      const response = await fetch(endpoint, { method: "POST" });
      if (!response.ok) {
        const error = await response.text();
        addLog("ERROR", `Trading ${pause ? "pause" : "resume"} failed: ${error}`);
        return;
      }
      addLog("SUCCESS", `Trading ${pause ? "paused" : "resumed"} successfully.`);
      fetchLiveStatus();
    } catch (e: any) {
      addLog("ERROR", `Trading ${pause ? "pause" : "resume"} error: ${e?.message || e}`);
    }
  };

  const fetchLiveStatus = async () => {
    try {
      const res = await fetch("/api/status");
      if (res.ok) {
        const data = await res.json();
        setLiveStatus(prev => ({
          data_source: data.data_source || prev.data_source,
          last_live_candle_time: data.last_live_candle_time || prev.last_live_candle_time,
          websocket_status: data.websocket_status || prev.websocket_status,
          nifty_ltp: data.nifty_ltp != null ? data.nifty_ltp : prev.nifty_ltp,
          cmp_source: data.cmp_source || prev.cmp_source,
          cmp_last_updated: data.cmp_last_updated || prev.cmp_last_updated,
          cpr_levels: data.cpr_levels || prev.cpr_levels,
          trading_mode: data.trading_mode || prev.trading_mode,
          market_status: data.market_status || prev.market_status,
          market_open: data.market_open ?? prev.market_open,
          market_detail: data.market_detail || prev.market_detail,
          daily_summary: data.daily_summary || prev.daily_summary,
          strategy_allowed: data.strategy_allowed ?? prev.strategy_allowed,
        }));
        if (data.cpr_levels) setCprLevels(data.cpr_levels);
        if (data.trading_mode) setTradingMode(data.trading_mode);
      }
    } catch {
      // backend unreachable — keep stale state
    }
  };

  const [inputApiKey, setInputApiKey] = useState("");
  const [inputApiSecret, setInputApiSecret] = useState("");
  const [isSavingCreds, setIsSavingCreds] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  const handleSaveCredentials = async () => {
    if (!inputApiKey.trim() || !inputApiSecret.trim()) {
      setValidationError("Both Client ID and Client Secret are required.");
      return;
    }
    setIsSavingCreds(true);
    setValidationError(null);
    try {
      const response = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upstox_api_key: inputApiKey.trim(), upstox_api_secret: inputApiSecret.trim() })
      });
      if (response.ok) {
        addLog("SUCCESS", "💾 Credentials persisted to server upstox_secrets.json successfully.");
        fetchUpstoxStatus();
      } else {
        const errText = await response.text();
        setValidationError(`Backend refused to save (${response.status}): ${errText}`);
      }
    } catch (e: any) {
      setValidationError(`Network error: ${e?.message || e}`);
    } finally {
      setIsSavingCreds(false);
    }
  };

  const fetchUpstoxStatus = async () => {
    try {
      const response = await fetch("/api/v1/upstox-status");
      if (response.ok) {
        const data = await response.json();
        setUpstoxStatus({
          connected: data.connected,
          token_status: data.token_status,
          expiry_status: data.expiry_status,
          last_authenticated: data.last_authenticated,
          token_preview: data.token_preview,
          loading: false,
          calculated_redirect_uri: data.calculated_redirect_uri || "",
          upstox_api_key: data.upstox_api_key || "",
          env_redirect_uri: data.env_redirect_uri || "",
          is_localhost_fallback: !!data.is_localhost_fallback,
        });
        if (data.upstox_api_key && data.upstox_api_key !== "mock_api_key") {
          setInputApiKey(prev => prev || data.upstox_api_key);
        }
        return data;
      }
    } catch {
      // ignore network failures, keep previous state
    }
    setUpstoxStatus(prev => ({ ...prev, loading: false }));
    return null;
  };

  const stopAuthPopupMonitor = () => {
    if (authStatusPollRef.current !== null) {
      window.clearInterval(authStatusPollRef.current);
      authStatusPollRef.current = null;
    }
  };

  const startAuthPopupMonitor = () => {
    if (authStatusPollRef.current !== null) return;
    authStatusPollRef.current = window.setInterval(async () => {
      try {
        const response = await fetch("/api/v1/upstox-status");
        if (!response.ok) return;
        const data = await response.json();
        setUpstoxStatus({
          connected: data.connected,
          token_status: data.token_status,
          expiry_status: data.expiry_status,
          last_authenticated: data.last_authenticated,
          token_preview: data.token_preview,
          loading: false,
          calculated_redirect_uri: data.calculated_redirect_uri || "",
          upstox_api_key: data.upstox_api_key || "",
          env_redirect_uri: data.env_redirect_uri || "",
          is_localhost_fallback: !!data.is_localhost_fallback,
        });
        if (data.connected) {
          addLog("SUCCESS", "⚡ UPSTOX CONNECTED: detected on server.");
          stopAuthPopupMonitor();
          authPopupRef.current = null;
        } else if (authPopupRef.current?.closed) {
          addLog("WARNING", "Upstox login window was closed before connection finished.");
          stopAuthPopupMonitor();
          authPopupRef.current = null;
        }
      } catch {
        // network error during polling; keep polling
      }
    }, 3000);
  };

  const handleConnectUpstox = async () => {
    setValidationError(null);

    if (inputApiKey.trim() && inputApiSecret.trim() &&
        (inputApiKey.trim() !== upstoxStatus.upstox_api_key || upstoxStatus.upstox_api_key === "mock_api_key")) {
      try {
        const response = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ upstox_api_key: inputApiKey.trim(), upstox_api_secret: inputApiSecret.trim() })
        });
        if (!response.ok) {
          setValidationError(`Auto-save failed (${response.status}). Connection aborted.`);
          return;
        }
        await fetchUpstoxStatus();
      } catch (e: any) {
        setValidationError(`Auto-save failed: ${e?.message}`);
        return;
      }
    }

    let url = "";
    try {
      const response = await fetch("/api/v1/login-url");
      if (response.ok) {
        const data = await response.json();
        url = data.url || "";
      }
    } catch {}

    const uses_mock = !url ||
                     url.includes("client_id=mock_api_key") ||
                     url.includes("client_id=mock_key") ||
                     !inputApiKey.trim();

    if (uses_mock) {
      setValidationError("⚠️ Enter your real Upstox API Key and Secret before connecting.");
      return;
    }

    addLog("INFO", "🔗 Opening Upstox OAuth login...");
    try {
      const width = 600, height = 750;
      const left = window.screen.width / 2 - width / 2;
      const top = window.screen.height / 2 - height / 2;
      const w = window.open(url, "upstox_oauth_popup",
        `width=${width},height=${height},top=${top},left=${left},scrollbars=yes`);
      authPopupRef.current = w || null;
      startAuthPopupMonitor();
      if (!w || w.closed) window.open(url, "_blank");
    } catch {
      authPopupRef.current = null;
      startAuthPopupMonitor();
      window.open(url, "_blank");
    }
  };

  useEffect(() => {
    const updateClock = () => {
      const now = new Date();
      setCurrentTime(now.toLocaleTimeString("en-US", { hour12: false }));
    };
    updateClock();
    const t = setInterval(updateClock, 1000);
    return () => clearInterval(t);
  }, []);

  const [config, setConfig] = useState({
    failWin: 10, retWin: 10, conWin: 10, entWin: 10,
    retTol: 5.0, slBuf: 3.0, tpBuf: 3.0, lossLimit: 2000,
  });

  const todayString = new Date().toISOString().slice(0, 10);
  const sevenDaysAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);

  const [cprLevels, setCprLevels] = useState<CPRLevels>({
    pivot: 0, bc: 0, tc: 0, r1: 0, s1: 0,
  });

  const [reportDate, setReportDate] = useState(todayString);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportResult, setReportResult] = useState<any>(null);
  const [reportError, setReportError] = useState<string | null>(null);

  // Backtest state
  const [btStartDate, setBtStartDate]   = useState(() => { const d = new Date(); d.setMonth(d.getMonth()-1); return d.toISOString().split("T")[0]; });
  const [btEndDate,   setBtEndDate]     = useState(() => new Date().toISOString().split("T")[0]);
  const [btLoading,   setBtLoading]     = useState(false);
  const [btResult,    setBtResult]      = useState<any>(null);
  const [btError,     setBtError]       = useState<string | null>(null);
  const [btTab,       setBtTab]         = useState<"summary"|"trades"|"daily">("summary");

  const handleRunBacktest = async () => {
    setBtError(null);
    setBtResult(null);
    setBtLoading(true);
    try {
      const res = await fetch(`/api/backtest?start=${btStartDate}&end=${btEndDate}`);
      const data = await res.json();
      if (!res.ok) { setBtError(data.detail || "Backtest failed"); }
      else { setBtResult(data); }
    } catch (e: any) {
      setBtError(`Network error: ${e?.message}`);
    } finally {
      setBtLoading(false);
    }
  };

  const [activeTab, setActiveTab] = useState<"cockpit" | "stateMachines" | "reports" | "broker" | "help">("cockpit");

  // ── DEMO ONLY: trades shown in the demo player (never confused with real DB trades)
  const [demoTrades, setDemoTrades] = useState<SimulatedTrade[]>([]);
  const [demoPnL, setDemoPnL] = useState(0);
  const [demoTradeCount, setDemoTradeCount] = useState(0);

  const [systemLogs, setSystemLogs] = useState<SystemLog[]>([
    { timestamp: new Date().toTimeString().split(" ")[0], level: "INFO", msg: "CPR Quantum Dashboard initializing..." },
    { timestamp: new Date().toTimeString().split(" ")[0], level: "INFO", msg: "Polling backend for live market status..." },
  ]);

  const [setupStates, setSetupStates] = useState<Record<string, SetupState>>({
    "SETUP_A": { name: "R1 → TC SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
    "SETUP_B": { name: "S1 → BC LONG",  state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
    "SETUP_C": { name: "TC → R1 LONG",  state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
    "SETUP_D": { name: "BC → S1 SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
  });

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [systemLogs]);

  // ── Demo runner (visual only, does not touch backend) ──────────────────────
  useEffect(() => {
    let interval: NodeJS.Timeout | null = null;
    if (isPlaying) {
      interval = setInterval(() => {
        setCurrentIdx(prev => {
          if (prev >= DEMO_CANDLES.length - 1) {
            setIsPlaying(false);
            addLog("INFO", "[DEMO] Demo playback complete. Backend live engine runs independently.");
            return prev;
          }
          return prev + 1;
        });
      }, speedMs);
    }
    return () => { if (interval) clearInterval(interval); };
  }, [isPlaying, speedMs]);

  const addLog = (level: SystemLog["level"], msg: string) => {
    const ts = new Date().toTimeString().split(" ")[0];
    setSystemLogs(prev => [...prev, { timestamp: ts, level, msg }]);
  };

  useEffect(() => {
    fetchUpstoxStatus();
    fetchLiveStatus();
    const liveTimer   = setInterval(fetchLiveStatus, 10_000);
    const tradesTimer = setInterval(fetchLiveTrades, 15_000);
    fetchLiveTrades(); // immediate fetch on mount

    const params = new URLSearchParams(window.location.search);
    if (params.get("upstox") === "success") {
      addLog("SUCCESS", "⚡ UPSTOX CONNECTED: OAuth completed successfully!");
      window.history.replaceState({}, document.title, window.location.pathname);
      fetchUpstoxStatus();
      fetchLiveStatus();
      fetchLiveTrades();
    }

    const handleMsg = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type === "OAUTH_AUTH_SUCCESS") {
        addLog("SUCCESS", "⚡ UPSTOX CONNECTED: OAuth popup completed successfully!");
        stopAuthPopupMonitor();
        authPopupRef.current = null;
        fetchUpstoxStatus();
        fetchLiveStatus();
        fetchLiveTrades();
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        fetchUpstoxStatus();
        fetchLiveStatus();
      }
    };

    window.addEventListener("message", handleMsg);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.removeEventListener("message", handleMsg);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearInterval(liveTimer);
      clearInterval(tradesTimer);
      stopAuthPopupMonitor();
    };
  }, []);

  const handleRunReport = async () => {
    setReportError(null);
    setReportResult(null);
    if (!reportDate) {
      setReportError("Please select a date.");
      return;
    }
    setReportLoading(true);
    try {
      const res = await fetch(`/api/report/historical?date=${reportDate}`);
      if (!res.ok) {
        const text = await res.text();
        setReportError(`Report failed: ${res.status} ${text}`);
      } else {
        setReportResult(await res.json());
      }
    } catch (e: any) {
      setReportError(`Network error: ${e?.message || e}`);
    } finally {
      setReportLoading(false);
    }
  };

  const handleResetDemo = () => {

    setCurrentIdx(0);
    setIsPlaying(false);
    setDemoTrades([]);
    setDemoPnL(0);
    setDemoTradeCount(0);
    setSetupStates({
      "SETUP_A": { name: "R1 → TC SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
      "SETUP_B": { name: "S1 → BC LONG",  state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
      "SETUP_C": { name: "TC → R1 LONG",  state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
      "SETUP_D": { name: "BC → S1 SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
    });
    setSystemLogs([{ timestamp: new Date().toTimeString().split(" ")[0], level: "INFO", msg: "[DEMO] Demo reset." }]);
  };


  // Real trade count comes from backend, not from frontend simulation
  const realTradeCount = liveStatus.daily_summary?.trade_count ?? 0;
  const realMaxTrades  = liveStatus.daily_summary?.max_trades  ?? MAX_TRADES_PER_DAY;
  const realPnL        = liveStatus.daily_summary?.realized_pnl ?? 0;
  const blockReason = liveStatus.daily_summary
    ? liveStatus.daily_summary.trade_count >= realMaxTrades
      ? `Daily trade limit reached (${realTradeCount}/${realMaxTrades} trades)`
      : realPnL <= -config.lossLimit
        ? "Daily loss limit reached"
        : liveStatus.daily_summary.is_blocked
          ? "Trading paused manually"
          : null
    : null;

  // CMP: only show live value — null if disconnected
  const currentPrice = liveStatus.nifty_ltp;
  const hasCPR = liveStatus.cpr_levels !== null && (liveStatus.cpr_levels?.pivot ?? 0) > 0;
  const displayCpr = liveStatus.cpr_levels || cprLevels;

  return (
    <div className="h-screen w-screen bg-slate-950 text-slate-300 flex flex-col overflow-hidden border-4 border-slate-800 font-sans">
      {/* Header */}
      <header className="h-20 shrink-0 border-b border-slate-800 bg-slate-900/55 px-8 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-10 h-10 bg-indigo-600 rounded flex items-center justify-center font-bold text-white text-lg shadow-lg">CP</div>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-white">CPR QUANTUM V6.1</h1>
            <p className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">Automated Nifty Trading System</p>
          </div>
        </div>

        <div className="flex gap-1 p-0.5 bg-slate-950 rounded border border-slate-800">
          {(["cockpit", "stateMachines", "reports", "broker", "help"] as const).map(tab => (
            <button key={tab} onClick={() => setActiveTab(tab)}
              className={`px-4 py-1.5 rounded text-xs font-bold font-mono transition-all uppercase tracking-wider cursor-pointer ${activeTab === tab ? "bg-indigo-600 text-white" : "text-slate-400 hover:text-white"}`}>
              {tab === "stateMachines" ? "Setups" : tab === "broker" ? "Upstox" : tab.charAt(0).toUpperCase() + tab.slice(1)}
            </button>
          ))}
        </div>

        <div className="flex gap-6 items-center">
          {/* RULE 1: Market status in header */}
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 uppercase font-bold font-mono">Market</span>
            <span className={`text-xs flex items-center gap-1.5 font-bold font-mono ${liveStatus.market_open ? "text-emerald-400" : "text-rose-400"}`}>
              ● {liveStatus.market_status}
            </span>
          </div>
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 uppercase font-bold font-mono">API Status</span>
            <span className={`text-xs flex items-center gap-1.5 font-bold font-mono ${upstoxStatus.connected ? "text-emerald-400" : "text-rose-400"}`}>
              ● {upstoxStatus.connected ? "UPSTOX CONNECTED" : "DISCONNECTED"}
            </span>
          </div>
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 uppercase font-bold font-mono text-right">Engine</span>
            <button onClick={() => {
              const target = tradingMode === "paper" ? "live" : "paper";
              setTradingMode(target);
              addLog("WARNING", `Mode changed to ${target.toUpperCase()}.`);
            }}
              className={`text-xs font-bold font-mono uppercase cursor-pointer transition-all ${tradingMode === "live" ? "text-rose-400 animate-pulse" : "text-sky-400"}`}>
              {tradingMode === "live" ? "⚠️ Live Trading" : "Paper Trading"}
            </button>
          </div>
          <div className="flex flex-col items-end border-l border-slate-800 pl-6 h-10 justify-center">
            <span className="text-2xl font-mono text-white tracking-widest leading-none">{currentTime}</span>
          </div>
        </div>
      </header>

      <main className="flex-1 flex overflow-hidden">
        {/* Left Rail */}
        <aside className="w-64 border-r border-slate-800 p-6 flex flex-col gap-4 bg-slate-950/20 shrink-0">
          <h2 className="text-xs font-bold text-slate-400 uppercase tracking-widest mb-2">Daily CPR Matrix</h2>

          {/* CPR Levels */}
          {hasCPR ? (
            <div className="space-y-3">
              {
                // Display in a fixed semantic order regardless of numeric values.
                [
                  { label: "R1 Level", val: displayCpr.r1, color: "rose" },
                  { label: "TC Level", val: displayCpr.tc, color: "orange" },
                  { label: "PIVOT",    val: displayCpr.pivot, color: "white" },
                  { label: "BC Level", val: displayCpr.bc, color: "cyan" },
                  { label: "S1 Level", val: displayCpr.s1, color: "emerald" },
                ].map(({ label, val, color }) => (
                  <div key={label} className={`p-3 bg-${color}-500/10 border-l-2 border-${color}-500 rounded-r`}>
                    <div className={`flex justify-between text-xs text-${color}-400`}>
                      <span>{label}</span>
                      <span className="font-mono font-bold text-sm">{val.toFixed(2)}</span>
                    </div>
                  </div>
                ))
              }
            </div>
          ) : (
            <div className="p-4 bg-slate-900 rounded border border-slate-700 text-center text-xs text-slate-500 font-mono">
              CPR levels unavailable<br/>
              <span className="text-[10px]">Authenticate Upstox to load live CPR</span>
            </div>
          )}
          {/* Semantic-order warning: show if levels don't follow R1>TC>Pivot>BC>S1 */}
          {hasCPR && (() => {
            const semanticOk = (displayCpr.r1 > displayCpr.tc && displayCpr.tc > displayCpr.pivot && displayCpr.pivot > displayCpr.bc && displayCpr.bc > displayCpr.s1);
            return !semanticOk ? (
              <div className="mt-2 p-2 rounded border border-amber-700 bg-amber-900/10 text-amber-300 text-xs">
                ⚠️ Note: CPR semantic order violated (TC may be below BC). Values are computed correctly from previous-day OHLC.
              </div>
            ) : null;
          })()}

          <div className="mt-auto flex flex-col gap-2">
            {/* RULE 4: DATA SOURCE card */}
            <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
              <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">DATA SOURCE</div>
              <div className={`text-[11px] font-mono font-bold flex items-center gap-1.5 ${
                liveStatus.data_source === "UPSTOX LIVE"   ? "text-emerald-400" :
                liveStatus.data_source === "HISTORICAL REPLAY" ? "text-amber-400"  : "text-rose-400"
              }`}>
                <span className={`w-1.5 h-1.5 rounded-full inline-block ${
                  liveStatus.data_source === "UPSTOX LIVE" ? "bg-emerald-400 animate-pulse" :
                  liveStatus.data_source === "HISTORICAL REPLAY" ? "bg-amber-400" : "bg-rose-400"
                }`} />
                {liveStatus.data_source}
              </div>
            </div>

            {/* RULE 1: MARKET STATUS card */}
            <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
              <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">MARKET STATUS</div>
              <div className={`text-[11px] font-mono font-bold flex items-center gap-1.5 ${liveStatus.market_open ? "text-emerald-400" : "text-rose-400"}`}>
                <span className={`w-1.5 h-1.5 rounded-full inline-block ${liveStatus.market_open ? "bg-emerald-400 animate-pulse" : "bg-rose-400"}`} />
                {liveStatus.market_status}
                {liveStatus.market_detail.is_holiday && <span className="text-amber-400 text-[9px] ml-1">HOLIDAY</span>}
              </div>
              <div className="text-[9px] text-slate-600 mt-0.5 font-mono">{liveStatus.market_detail.weekday}</div>
            </div>

            {/* RULE 6: STRATEGY ALLOWED card */}
            <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
              <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">STRATEGY</div>
              <div className={`text-[11px] font-mono font-bold flex items-center gap-1.5 ${liveStatus.strategy_allowed ? "text-emerald-400" : "text-rose-400"}`}>
                <span className={`w-1.5 h-1.5 rounded-full inline-block ${liveStatus.strategy_allowed ? "bg-emerald-400 animate-pulse" : "bg-rose-400"}`} />
                {liveStatus.strategy_allowed ? "ALLOWED" : "BLOCKED"}
              </div>
              <button onClick={() => handleToggleTradingPause(!liveStatus.daily_summary?.is_blocked)}
                className={`mt-4 w-full text-[10px] font-bold uppercase rounded-lg py-2 transition-all ${liveStatus.daily_summary?.is_blocked ? "bg-emerald-500 text-slate-950 hover:bg-emerald-400" : "bg-rose-500 text-slate-950 hover:bg-rose-400"}`}>
                {liveStatus.daily_summary?.is_blocked ? "Resume Trading" : "Pause Trading"}
              </button>
            </div>

            {/* RULE 5: NIFTY CMP with source */}
            <div className="p-4 bg-slate-900 rounded-lg border border-slate-800 shadow-md">
              <div className="text-[10px] text-slate-500 mb-1 uppercase tracking-widest font-bold font-mono">NIFTY 50 CMP</div>
              {currentPrice !== null ? (
                <>
                  <div className="text-3xl font-mono text-white font-bold tracking-tight">{currentPrice.toFixed(2)}</div>
                  <div className="text-[9px] text-slate-500 font-mono mt-1">
                    SRC: <span className="text-emerald-400">{liveStatus.cmp_source}</span>
                  </div>
                  {liveStatus.cmp_last_updated && (
                    <div className="text-[9px] text-slate-600 font-mono truncate">
                      {liveStatus.cmp_last_updated.slice(11, 19)} UTC
                    </div>
                  )}
                </>
              ) : (
                <div className="text-sm font-mono text-rose-400 font-bold mt-1">
                  — DISCONNECTED —
                  <div className="text-[9px] text-slate-500 font-mono mt-1 font-normal">Authenticate Upstox for live CMP</div>
                </div>
              )}
            </div>
          </div>
        </aside>

        {/* Main Workspace */}
        <section className="flex-1 p-8 overflow-y-auto flex flex-col gap-6 bg-slate-950/40">

          {/* ── RULE 1+2+6: Status Banner (always visible) ── */}
          {(!liveStatus.market_open || !upstoxStatus.connected || !liveStatus.strategy_allowed) && (
            <div className={`rounded-xl border p-4 flex items-start gap-3 ${
              !liveStatus.market_open ? "bg-slate-900/60 border-slate-700" :
              !upstoxStatus.connected ? "bg-rose-950/30 border-rose-800" :
              "bg-amber-950/30 border-amber-800"
            }`}>
              <AlertTriangle className={`h-5 w-5 mt-0.5 shrink-0 ${
                !liveStatus.market_open ? "text-slate-400" :
                !upstoxStatus.connected ? "text-rose-400" : "text-amber-400"
              }`} />
              <div className="text-xs font-mono leading-relaxed">
                {!liveStatus.market_open && (
                  <div className="text-slate-300 font-bold">
                    MARKET CLOSED — Strategy engine is fully suppressed.
                    No candles, no signals, no trades until NSE opens (Mon–Fri 09:15–15:30 IST).
                    <span className="text-slate-500 font-normal ml-2">Today: {liveStatus.market_detail.weekday}</span>
                  </div>
                )}
                {liveStatus.market_open && !upstoxStatus.connected && (
                  <div className="text-rose-300 font-bold">
                    DATA SOURCE: DISCONNECTED — Upstox not authenticated.
                    Strategy will NOT run. No mock or simulation data will be used.
                    Connect via the Upstox tab.
                  </div>
                )}
                {liveStatus.market_open && upstoxStatus.connected && !liveStatus.strategy_allowed && (
                  <div className="text-amber-300 font-bold">
                    STRATEGY BLOCKED — {blockReason ?? "Trading blocked for the day"}.
                    No further trades today.
                  </div>
                )}
              </div>
            </div>
          )}

          {activeTab === "cockpit" && (
            <div className="flex flex-col gap-6">
              {/* Setup State Machines overview */}
              <div className="grid grid-cols-2 gap-6">
                {(["SETUP_A", "SETUP_B", "SETUP_C", "SETUP_D"] as const).map(key => {
                  const ss = setupStates[key];
                  const colorMap: Record<string, string> = {
                    SETUP_A: "rose", SETUP_B: "emerald", SETUP_C: "sky", SETUP_D: "purple"
                  };
                  const c = colorMap[key];
                  return (
                    <div key={key} className={`bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col justify-between h-36 transition-all ${ss.state > 0 ? `border-${c}-500/30 ring-1 ring-${c}-500/15` : ""}`}>
                      <div className="flex justify-between items-start">
                        <div>
                          <span className={`text-[10px] px-2 py-0.5 bg-${c}-600 text-white font-bold rounded uppercase font-mono`}>{key}</span>
                          <h3 className="text-sm font-bold text-white mt-1">{ss.name}</h3>
                        </div>
                        <span className={`text-[10px] font-mono uppercase font-bold ${ss.state > 3 ? `text-${c}-400` : ss.state > 0 ? "text-amber-400 animate-pulse" : "text-slate-500"}`}>
                          {["IDLE","BROKEN","RECOVERED","ARMED"][ss.state] || "IDLE"}
                        </span>
                      </div>
                      <div className="flex gap-1.5 mt-4">
                        {[1,2,3,4,5].map(step => (
                          <div key={step} className={`h-1.5 flex-1 rounded-full transition-all ${ss.state >= step ? `bg-${c}-500` : "bg-slate-800"}`} />
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* DEMO chart — clearly labeled */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col gap-4">
                <div className="flex justify-between items-center pb-2 border-b border-slate-800/40">
                  <h3 className="text-xs font-mono tracking-widest text-slate-400 uppercase flex items-center gap-1.5 font-bold">
                    <Sparkles className="h-4 w-4 text-amber-400" />
                    DEMO PLAYER — Not Live Data
                  </h3>
                  <span className="text-[10px] text-amber-400 font-mono bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 rounded">DEMO ONLY</span>
                </div>

                <div className="h-[200px] w-full bg-slate-950 rounded-lg p-2 border border-slate-900">
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={DEMO_CANDLES.slice(0, currentIdx + 1)}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(51,65,85,0.06)" />
                      <XAxis dataKey="time" stroke="#475569" style={{ fontSize: "10px", fontFamily: "monospace" }} />
                      <YAxis domain={["auto", "auto"]} stroke="#475569" style={{ fontSize: "10px", fontFamily: "monospace" }} />
                      <Tooltip contentStyle={{ backgroundColor: "#0f172a", border: "1px solid #1e293b", borderRadius: "8px" }} />
                      <Line type="monotone" dataKey="close" stroke="#f59e0b" strokeWidth={2} dot={{ r: 3, fill: "#f59e0b" }} name="Demo Close" />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>

                <div className="flex flex-wrap gap-4 items-center justify-between p-3 bg-slate-950 rounded-lg border border-slate-850">
                  <div className="flex items-center gap-2">
                    <button onClick={() => setIsPlaying(!isPlaying)}
                      className={`flex items-center gap-1.5 px-4 py-2 rounded text-xs font-bold uppercase tracking-wider cursor-pointer ${isPlaying ? "bg-amber-500 text-slate-950 animate-pulse" : "bg-amber-500/80 hover:bg-amber-400 text-slate-950"}`}>
                      {isPlaying ? <><Pause className="h-4 w-4 fill-current" />Pause</> : <><Play className="h-4 w-4 fill-current" />Play Demo</>}
                    </button>
                    <button onClick={() => setCurrentIdx(p => Math.min(p + 1, DEMO_CANDLES.length - 1))} disabled={isPlaying}
                      className="p-2 bg-slate-900 border border-slate-800 rounded hover:bg-slate-800 text-slate-300 disabled:opacity-50 cursor-pointer">
                      <PlusCircle className="h-4 w-4" />
                    </button>
                    <button onClick={handleResetDemo}
                      className="p-2 bg-slate-900 border border-slate-800 rounded hover:bg-slate-800 text-slate-300 cursor-pointer">
                      <RotateCcw className="h-4 w-4" />
                    </button>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-[10px] text-slate-500 font-mono uppercase">Speed:</span>
                    <div className="flex bg-slate-900 p-0.5 rounded border border-slate-800">
                      {[2000, 1000, 400].map(ms => (
                        <button key={ms} onClick={() => setSpeedMs(ms)}
                          className={`px-2.5 py-1 rounded text-[10px] font-bold font-mono cursor-pointer ${speedMs === ms ? "bg-slate-800 text-amber-400" : "text-slate-500 hover:text-slate-300"}`}>
                          {ms === 2000 ? "1x" : ms === 1000 ? "2x" : "5x"}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
                <p className="text-[10px] text-slate-600 font-mono italic">
                  ⚠ This demo player uses hardcoded sample candles for illustration only.
                  The live backend engine fetches real Upstox candles independently.
                </p>
              </div>

              {/* Strategy parameters */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col gap-4">
                <div className="flex items-center gap-1.5">
                  <Sliders className="h-4 w-4 text-indigo-400" />
                  <h3 className="text-xs font-mono tracking-widest text-slate-400 uppercase font-bold">Strategy Hyperparameters</h3>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                  {[
                    { label: "Breakout Window (bars)", key: "failWin" as const, type: "number" },
                    { label: "Retest Timeout (bars)",  key: "retWin"  as const, type: "number" },
                    { label: "Entry Window (bars)",    key: "conWin"  as const, type: "number" },
                    { label: "Retest Tolerance (pts)", key: "retTol"  as const, type: "decimal" },
                    { label: "SL Buffer (pts)",        key: "slBuf"   as const, type: "decimal" },
                    { label: "Loss Limit (₹)",         key: "lossLimit" as const, type: "number" },
                  ].map(({ label, key, type }) => (
                    <div key={key} className="flex flex-col gap-1.5">
                      <label className="text-slate-400 font-semibold">{label}</label>
                      <input type="number" step={type === "decimal" ? "0.5" : "1"}
                        value={config[key]}
                        onChange={e => setConfig(p => ({ ...p, [key]: type === "decimal" ? parseFloat(e.target.value) || 0 : parseInt(e.target.value) || 1 }))}
                        className="bg-slate-950 border border-slate-800 rounded-lg p-2 font-mono text-slate-100 focus:outline-none focus:border-indigo-500 font-bold"
                      />
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {activeTab === "reports" && (
            <div className="flex flex-col gap-6 p-4 overflow-y-auto h-full">

              {/* ── HISTORICAL REPORT ── */}
              <div className="bg-slate-900/60 border border-slate-700 rounded-xl p-5">
                <h3 className="text-sm font-bold text-white uppercase tracking-wider font-mono mb-4 flex items-center gap-2">
                  <span>📋</span> Historical Trade Report
                  <span className="text-[10px] text-slate-500 font-normal normal-case ml-1">(live/paper trades your bot placed)</span>
                </h3>
                <div className="flex gap-3 items-end flex-wrap">
                  <div className="flex flex-col gap-1">
                    <span className="text-[10px] text-slate-500 font-mono uppercase">Date</span>
                    <input type="date" value={reportDate} onChange={e => setReportDate(e.target.value)}
                      className="bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-xs font-mono text-slate-200 outline-none focus:border-indigo-500" />
                  </div>
                  <button onClick={handleRunReport}
                    className={`px-4 py-2 rounded uppercase text-xs font-bold tracking-wider cursor-pointer ${reportLoading ? "bg-slate-700 text-slate-400" : "bg-sky-600 text-white hover:bg-sky-500"}`}>
                    {reportLoading ? "Loading…" : "Generate"}
                  </button>
                </div>
                {reportError && <div className="mt-3 text-xs text-rose-400 font-mono bg-rose-900/20 border border-rose-700/30 rounded p-2">{reportError}</div>}
                {reportResult && (
                  <div className="mt-4">
                    <div className="grid grid-cols-3 gap-3 mb-4">
                      {[
                        { label: "Trades", value: reportResult.metrics.total_trades, color: "text-white" },
                        { label: "Win Rate", value: `${reportResult.metrics.win_rate}%`, color: "text-emerald-400" },
                        { label: "Net P&L", value: `₹${reportResult.metrics.net_pnl}`, color: reportResult.metrics.net_pnl >= 0 ? "text-emerald-400" : "text-rose-400" },
                      ].map(m => (
                        <div key={m.label} className="bg-slate-800 rounded-lg p-3 text-center border border-slate-700">
                          <div className="text-[10px] text-slate-500 uppercase font-mono">{m.label}</div>
                          <div className={`text-lg font-bold font-mono mt-1 ${m.color}`}>{m.value}</div>
                        </div>
                      ))}
                    </div>
                    {reportResult.metrics.total_trades === 0
                      ? <div className="text-xs text-slate-500 text-center py-4">No trades placed by the bot on this date.</div>
                      : reportResult.trades.map((t: any) => (
                        <div key={t.id} className="bg-slate-800 border border-slate-700 rounded p-3 mb-2 text-xs font-mono">
                          <div className="flex justify-between">
                            <span className={`font-bold ${t.option_type === "CE" ? "text-sky-400" : "text-amber-400"}`}>{t.setup_name} — {t.option_type}</span>
                            <span className={`font-bold ${t.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{t.pnl >= 0 ? "+" : ""}₹{t.pnl}</span>
                          </div>
                          <div className="text-slate-500 mt-1">Entry ₹{t.entry_price} → Exit ₹{t.exit_price} | {t.status}</div>
                        </div>
                      ))
                    }
                  </div>
                )}
              </div>

              {/* ── BACKTEST ENGINE ── */}
              <div className="bg-slate-900/60 border border-indigo-700/40 rounded-xl p-5">
                <h3 className="text-sm font-bold text-indigo-300 uppercase tracking-wider font-mono mb-1 flex items-center gap-2">
                  <span>🔬</span> Backtest Engine
                  <span className="text-[10px] text-slate-500 font-normal normal-case ml-1">(simulate strategy on past NIFTY data)</span>
                </h3>
                <p className="text-[11px] text-slate-500 mb-4">Fetches real historical NIFTY 5m data from Upstox and replays all 4 setups. Requires Upstox to be connected.</p>

                <div className="flex gap-3 items-end flex-wrap">
                  <div className="flex flex-col gap-1">
                    <span className="text-[10px] text-slate-500 font-mono uppercase">From</span>
                    <input type="date" value={btStartDate} onChange={e => setBtStartDate(e.target.value)}
                      className="bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-xs font-mono text-slate-200 outline-none focus:border-indigo-500" />
                  </div>
                  <div className="flex flex-col gap-1">
                    <span className="text-[10px] text-slate-500 font-mono uppercase">To</span>
                    <input type="date" value={btEndDate} onChange={e => setBtEndDate(e.target.value)}
                      className="bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-xs font-mono text-slate-200 outline-none focus:border-indigo-500" />
                  </div>
                  <button onClick={handleRunBacktest} disabled={btLoading}
                    className={`px-5 py-2 rounded uppercase text-xs font-bold tracking-wider cursor-pointer border transition-all disabled:opacity-50 ${btLoading ? "bg-slate-700 text-slate-400 border-slate-600" : "bg-indigo-600 border-indigo-500 text-white hover:bg-indigo-500"}`}>
                    {btLoading ? "⏳ Running…" : "▶ Run Backtest"}
                  </button>
                </div>

                {btLoading && (
                  <div className="mt-4 text-xs text-indigo-400 font-mono animate-pulse">
                    Fetching historical candles and replaying strategy… this may take 30–60 seconds for longer date ranges.
                  </div>
                )}

                {btError && <div className="mt-3 text-xs text-rose-400 font-mono bg-rose-900/20 border border-rose-700/30 rounded p-2">{btError}</div>}

                {btResult && (
                  <div className="mt-5">

                    {/* Top metrics */}
                    <div className="grid grid-cols-2 gap-3 mb-4 sm:grid-cols-4">
                      {[
                        { label: "Total Trades", value: btResult.metrics.total_trades, color: "text-white" },
                        { label: "Win Rate",     value: `${btResult.metrics.win_rate}%`, color: "text-emerald-400" },
                        { label: "Net P&L",      value: `₹${btResult.metrics.net_pnl}`, color: btResult.metrics.net_pnl >= 0 ? "text-emerald-400" : "text-rose-400" },
                        { label: "Avg/Trade",    value: `₹${btResult.metrics.avg_pnl_per_trade}`, color: btResult.metrics.avg_pnl_per_trade >= 0 ? "text-emerald-400" : "text-rose-400" },
                      ].map(m => (
                        <div key={m.label} className="bg-slate-800 rounded-lg p-3 text-center border border-slate-700">
                          <div className="text-[10px] text-slate-500 uppercase font-mono">{m.label}</div>
                          <div className={`text-xl font-bold font-mono mt-1 ${m.color}`}>{m.value}</div>
                        </div>
                      ))}
                    </div>

                    <div className="grid grid-cols-3 gap-3 mb-4">
                      {[
                        { label: "TP Hits",   value: btResult.metrics.wins,      color: "text-emerald-400" },
                        { label: "SL Hits",   value: btResult.metrics.losses,    color: "text-rose-400" },
                        { label: "EOD Exits", value: btResult.metrics.eod_exits, color: "text-amber-400" },
                      ].map(m => (
                        <div key={m.label} className="bg-slate-800 rounded-lg p-3 text-center border border-slate-700">
                          <div className="text-[10px] text-slate-500 uppercase font-mono">{m.label}</div>
                          <div className={`text-xl font-bold font-mono mt-1 ${m.color}`}>{m.value}</div>
                        </div>
                      ))}
                    </div>

                    {/* Per-setup breakdown */}
                    <div className="mb-4">
                      <div className="text-[11px] text-slate-400 font-mono uppercase font-bold mb-2">Setup Breakdown</div>
                      <div className="grid grid-cols-2 gap-2">
                        {Object.entries(btResult.setup_breakdown).map(([name, s]: [string, any]) => (
                          <div key={name} className={`bg-slate-800 border rounded-lg p-3 ${s.net_pnl >= 0 ? "border-emerald-800/50" : "border-rose-800/50"}`}>
                            <div className="flex justify-between items-center">
                              <span className="text-xs font-bold font-mono text-slate-200">{name}</span>
                              <span className={`text-xs font-bold font-mono ${s.net_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>₹{s.net_pnl}</span>
                            </div>
                            <div className="text-[10px] text-slate-500 font-mono mt-1">
                              {s.trades} trades · {s.win_rate}% WR · {s.wins}W {s.losses}L
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Sub-tabs: Summary / Trades / Daily */}
                    <div className="flex gap-2 mb-3">
                      {(["summary","trades","daily"] as const).map(t => (
                        <button key={t} onClick={() => setBtTab(t)}
                          className={`px-3 py-1 rounded text-[10px] font-bold font-mono uppercase cursor-pointer border transition-all ${btTab === t ? "bg-indigo-600 border-indigo-500 text-white" : "bg-slate-800 border-slate-700 text-slate-400 hover:text-white"}`}>
                          {t === "summary" ? "📊 Summary" : t === "trades" ? "📋 All Trades" : "📅 By Day"}
                        </button>
                      ))}
                    </div>

                    {/* Summary tab */}
                    {btTab === "summary" && (
                      <div className="text-xs font-mono text-slate-400 bg-slate-800 rounded-lg p-4 space-y-1 border border-slate-700">
                        <div>Period: <span className="text-white">{btResult.start_date} → {btResult.end_date}</span></div>
                        <div>Days processed: <span className="text-white">{btResult.days_processed}</span></div>
                        {btResult.days_skipped.length > 0 && <div>Days skipped (no data): <span className="text-amber-400">{btResult.days_skipped.join(", ")}</span></div>}
                        <div>Gross Profit: <span className="text-emerald-400">₹{btResult.metrics.gross_profit}</span></div>
                        <div>Gross Loss: <span className="text-rose-400">₹{btResult.metrics.gross_loss}</span></div>
                        <div>Net P&L: <span className={btResult.metrics.net_pnl >= 0 ? "text-emerald-400 font-bold" : "text-rose-400 font-bold"}>₹{btResult.metrics.net_pnl}</span></div>
                      </div>
                    )}

                    {/* All trades tab */}
                    {btTab === "trades" && (
                      <div className="flex flex-col gap-1.5 max-h-96 overflow-y-auto pr-1">
                        {btResult.trades.length === 0
                          ? <div className="text-xs text-slate-500 text-center py-6">No trades triggered in this period.</div>
                          : btResult.trades.map((t: any, i: number) => (
                            <div key={i} className="bg-slate-800 border border-slate-700 rounded-lg p-3 text-[11px] font-mono">
                              <div className="flex justify-between items-center">
                                <span className="text-slate-300 font-bold">{t.date} — {t.setup_name}</span>
                                <span className={`font-bold ${t.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{t.pnl >= 0 ? "+" : ""}₹{t.pnl}</span>
                              </div>
                              <div className="text-slate-500 mt-0.5">
                                {t.option_type} · Entry ₹{t.entry_price} · Exit ₹{t.exit_price} · SL ₹{t.stop_loss} · TP ₹{t.take_profit}
                              </div>
                              <div className="flex justify-between mt-0.5">
                                <span className="text-slate-600">{t.entry_time?.slice(11,16)} → {t.exit_time?.slice(11,16)}</span>
                                <span className={`font-bold text-[10px] ${t.status === "CLOSED_TP" ? "text-emerald-400" : t.status === "CLOSED_SL" ? "text-rose-400" : "text-amber-400"}`}>{t.status}</span>
                              </div>
                            </div>
                          ))
                        }
                      </div>
                    )}

                    {/* Daily tab */}
                    {btTab === "daily" && (
                      <div className="flex flex-col gap-1.5 max-h-96 overflow-y-auto pr-1">
                        {btResult.day_summaries.filter((d: any) => d.trades > 0).length === 0
                          ? <div className="text-xs text-slate-500 text-center py-6">No trading days with triggered setups.</div>
                          : btResult.day_summaries.filter((d: any) => d.trades > 0).map((d: any, i: number) => (
                            <div key={i} className="bg-slate-800 border border-slate-700 rounded-lg p-3 text-[11px] font-mono flex justify-between items-center">
                              <div>
                                <span className="text-slate-200 font-bold">{d.date}</span>
                                <span className="text-slate-500 ml-2">{d.trades} trade{d.trades > 1 ? "s" : ""} · {d.wins}W {d.losses}L</span>
                              </div>
                              <span className={`font-bold ${d.net_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{d.net_pnl >= 0 ? "+" : ""}₹{d.net_pnl}</span>
                            </div>
                          ))
                        }
                      </div>
                    )}

                  </div>
                )}
              </div>

            </div>
          )}

          {activeTab === "stateMachines" && (
            <div className="flex flex-col gap-6">
              {Object.entries(setupStates).map(([key, item]) => {
                const setup = item as SetupState;
                return (
                  <div key={key} className="bg-slate-900/40 p-5 rounded-xl border border-slate-800">
                    <div className="flex items-center justify-between mb-4 border-b border-slate-800 pb-3">
                      <h4 className="text-sm font-bold text-slate-100">{key}: {setup.name}</h4>
                      <span className="text-xs px-2.5 py-1 rounded font-bold font-mono bg-slate-800 text-slate-400">
                        STAGE {setup.state} — {["IDLE","BROKEN","RECOVERED","RETESTED (ARMED)"][setup.state] || "IDLE"}
                      </span>
                    </div>
                    <div className="grid grid-cols-4 gap-2">
                      {[
                        { label: "Breakout",     desc: "hi+cl beyond level" },
                        { label: "Recovery",     desc: "close back inside"   },
                        { label: "Retest",       desc: "touches level again" },
                        { label: "Entry Armed",  desc: "cross retest hi/lo"  },
                      ].map(({ label, desc }, sIdx) => {
                        const isDone    = setup.state >= sIdx + 1;
                        const isCurrent = setup.state === sIdx + 1;
                        return (
                          <div key={label} className={`p-3 rounded border flex flex-col gap-1 transition-all ${
                            isDone
                              ? "bg-emerald-950/20 text-emerald-400 border-emerald-900/60"
                              : isCurrent
                              ? "bg-amber-950/20 text-amber-400 border-amber-900/60 animate-pulse"
                              : "bg-slate-950/40 text-slate-600 border-slate-900/40"
                          }`}>
                            <span className="text-[10px] font-mono font-bold">Step 0{sIdx+1}</span>
                            <span className="text-xs font-bold">{label}</span>
                            <span className="text-[9px] text-slate-500 font-mono leading-tight">{desc}</span>
                          </div>
                        );
                      })}
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-3 text-xs font-mono bg-slate-950 p-3 rounded border border-slate-900">
                      <div><span className="text-slate-500">Retest High: </span><span className="text-emerald-400 font-bold">{setup.retestHigh || "--"}</span></div>
                      <div><span className="text-slate-500">Retest Low: </span><span className="text-emerald-400 font-bold">{setup.retestLow || "--"}</span></div>
                      <div className="col-span-2 text-[10px] text-slate-600">Entry triggers when next candle crosses retest low (shorts) or retest high (longs)</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {activeTab === "broker" && (
            <div className="flex flex-col gap-6">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col justify-between">
                  <div>
                    <span className="text-[10px] text-slate-500 uppercase font-bold font-mono block mb-1">Status Panel</span>
                    <h3 className="text-sm font-bold text-slate-200 mb-4 font-mono">CONNECTION INTEGRATION</h3>
                    <div className="space-y-3">
                      <div className="flex justify-between items-center bg-slate-950/40 p-2.5 rounded border border-slate-850">
                        <span className="text-slate-400 text-xs">Broker:</span>
                        <span className="text-white font-mono font-bold text-xs">Upstox API v2</span>
                      </div>
                      <div className="flex justify-between items-center bg-slate-950/40 p-2.5 rounded border border-slate-850">
                        <span className="text-slate-400 text-xs">State:</span>
                        <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-bold font-mono ${upstoxStatus.connected ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20" : "bg-rose-500/10 text-rose-400 border border-rose-500/20"}`}>
                          <span className={`h-1.5 w-1.5 rounded-full ${upstoxStatus.connected ? "bg-emerald-400" : "bg-rose-400 animate-pulse"}`}></span>
                          {upstoxStatus.connected ? "CONNECTED" : "DISCONNECTED"}
                        </span>
                      </div>
                      {/* RULE 4: Data source */}
                      <div className="flex justify-between items-center bg-slate-950/40 p-2.5 rounded border border-slate-850">
                        <span className="text-slate-400 text-xs">Data Source:</span>
                        <span className={`text-[11px] font-bold font-mono ${liveStatus.data_source === "UPSTOX LIVE" ? "text-emerald-400" : "text-rose-400"}`}>
                          {liveStatus.data_source}
                        </span>
                      </div>
                      {/* RULE 1: Market status */}
                      <div className="flex justify-between items-center bg-slate-950/40 p-2.5 rounded border border-slate-850">
                        <span className="text-slate-400 text-xs">Market:</span>
                        <span className={`text-[11px] font-bold font-mono ${liveStatus.market_open ? "text-emerald-400" : "text-rose-400"}`}>
                          {liveStatus.market_status}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="mt-4 pt-3 border-t border-slate-800/60">
                    {validationError && (
                      <div className="mb-3 bg-rose-500/10 border border-rose-500/20 text-rose-300 p-3 rounded text-xs leading-relaxed">
                        {validationError}
                      </div>
                    )}
                    <div className="flex gap-2">
                      <button onClick={handleConnectUpstox}
                        className="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white font-semibold font-mono text-xs py-2 px-3 rounded uppercase tracking-wider cursor-pointer">
                        {upstoxStatus.connected ? "Reconnect" : "Connect Upstox"}
                      </button>
                      <button onClick={fetchUpstoxStatus}
                        className="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded p-2 cursor-pointer">
                        <RefreshCw className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                </div>

                <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5">
                  <span className="text-[10px] text-slate-500 uppercase font-bold font-mono block mb-1">OAuth Token</span>
                  <h3 className="text-sm font-bold text-slate-200 mb-4 font-mono">SECURE OAUTH STATE</h3>
                  <div className="space-y-2 text-xs font-mono">
                    {[
                      ["Token Status", upstoxStatus.token_status, upstoxStatus.token_status === "Active" ? "text-emerald-400" : "text-amber-400"],
                      ["Token Preview", upstoxStatus.token_preview, "text-indigo-300"],
                      ["Last Synced", upstoxStatus.last_authenticated ? new Date(upstoxStatus.last_authenticated).toLocaleTimeString() : "--", "text-slate-300"],
                    ].map(([label, val, cls]) => (
                      <div key={String(label)} className="flex justify-between py-1 border-b border-slate-800">
                        <span className="text-slate-400">{label}:</span>
                        <span className={String(cls)}>{val}</span>
                      </div>
                    ))}
                  </div>
                  <div className="text-[10px] text-slate-400 bg-slate-950/60 border border-slate-905 p-2.5 rounded mt-4">
                    ⚡ {upstoxStatus.expiry_status}
                  </div>
                </div>

                <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5">
                  <span className="text-[10px] text-slate-500 uppercase font-bold font-mono block mb-1">Environment</span>
                  <h3 className="text-sm font-bold text-slate-200 mb-4 font-mono">SAFEGUARD ENV</h3>
                  <div className="space-y-3">
                    <div className="bg-rose-500/10 border border-rose-500/20 text-rose-400 p-3 rounded text-xs">
                      <span className="font-bold flex items-center gap-1.5 uppercase font-mono">
                        <AlertTriangle className="h-4 w-4 animate-pulse" />PAPER TRADING ACTIVE
                      </span>
                      <p className="mt-1.5 text-[11px] text-slate-400 font-sans">Orders simulated. No real capital at risk.</p>
                    </div>
                  </div>
                  <div className="text-[10px] text-slate-500 font-mono italic text-right mt-3">DB: SQLite Local</div>
                </div>
              </div>

              {/* Credentials setup */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5">
                <div className="flex items-center gap-2 border-b border-slate-800 pb-3 mb-4">
                  <Sliders className="h-4 w-4 text-amber-400" />
                  <h3 className="text-xs font-mono tracking-widest text-slate-300 uppercase font-bold">
                    🔑 UPSTOX CREDENTIALS & REDIRECT URI
                  </h3>
                </div>
                <div className="bg-slate-950/60 p-4 border border-slate-850 rounded-lg text-xs space-y-3">
                  <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3.5 space-y-3">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5">
                      <div className="space-y-1">
                        <label className="text-[10px] text-slate-400 font-mono block">Upstox API Key (Client ID):</label>
                        <input type="text" placeholder="Paste your Upstox Client ID..."
                          value={inputApiKey} onChange={e => setInputApiKey(e.target.value)}
                          className="w-full text-indigo-300 font-mono text-xs bg-slate-950 border border-slate-800 px-2.5 py-1.5 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 placeholder-slate-600" />
                      </div>
                      <div className="space-y-1">
                        <label className="text-[10px] text-slate-400 font-mono block">Upstox API Secret:</label>
                        <input type="password" placeholder="Paste your Client Secret..."
                          value={inputApiSecret} onChange={e => setInputApiSecret(e.target.value)}
                          className="w-full text-indigo-300 font-mono text-xs bg-slate-950 border border-slate-800 px-2.5 py-1.5 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 placeholder-slate-600" />
                      </div>
                    </div>
                    <div className="flex justify-end">
                      <button onClick={handleSaveCredentials} disabled={isSavingCreds}
                        className="text-xs bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 text-white px-4 py-1.5 rounded cursor-pointer font-bold transition-all">
                        {isSavingCreds ? "Saving..." : "Save & Persist API Keys"}
                      </button>
                    </div>
                  </div>
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-2">
                    <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
                      <span className="text-[10px] text-slate-500 font-mono font-bold block mb-1">CLIENT ID (Sent by Bot)</span>
                      <input readOnly type="text" value={upstoxStatus.upstox_api_key || "Not configured"}
                        onClick={e => e.currentTarget.select()}
                        className="w-full text-indigo-300 font-mono text-xs bg-slate-950 border border-slate-850 px-2.5 py-1.5 rounded focus:outline-none" />
                    </div>
                    <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
                      <span className="text-[10px] text-slate-500 font-mono font-bold block mb-1">REDIRECT URI (copy to Upstox console)</span>
                      <div className="flex gap-2">
                        <input readOnly type="text" value={upstoxStatus.calculated_redirect_uri || (window.location.origin + "/callback")}
                          onClick={e => e.currentTarget.select()}
                          className="flex-1 text-emerald-400 font-mono text-xs bg-slate-950 border border-slate-850 px-2.5 py-1.5 rounded focus:outline-none" />
                        <button onClick={() => {
                          const val = upstoxStatus.calculated_redirect_uri || (window.location.origin + "/callback");
                          navigator.clipboard?.writeText(val).then(() => addLog("SUCCESS", "Redirect URI copied."));
                        }} className="text-[11px] bg-emerald-950/40 text-emerald-400 border border-emerald-500/20 px-3 py-1.5 rounded font-mono cursor-pointer font-bold">
                          Copy
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {activeTab === "help" && (
            <div className="bg-slate-900/40 p-6 rounded-xl border border-slate-800 flex flex-col gap-4 text-xs leading-relaxed overflow-y-auto">
              <h3 className="text-sm font-bold text-slate-100 flex items-center gap-1.5 border-b border-slate-800 pb-3">
                <HelpCircle className="h-5 w-5 text-emerald-400" />
                CPR Trading Bot — System Rules & Guardrails
              </h3>
              <div className="flex flex-col gap-4 text-slate-300">
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">RULE 1 — Market Hours Gate</h4>
                  <p className="mt-1 text-slate-400">Strategy executes ONLY Mon–Fri between 09:15–15:30 IST. Weekends and NSE holidays are blocked. The market status badge (header and sidebar) shows OPEN or CLOSED.</p>
                </div>
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">RULE 2 — MAX_TRADES_PER_DAY = 2</h4>
                  <p className="mt-1 text-slate-400">Backend enforces a hard limit of 2 trades per calendar day. Once reached, all strategy evaluation stops. The DAILY TRADES counter shows the live DB value — not a simulated number.</p>
                </div>
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">RULE 3 — No Mock / Simulation Data</h4>
                  <p className="mt-1 text-slate-400">If Upstox is not authenticated, the strategy engine receives zero candles and produces zero signals. There is no mock, random, or historical playback fallback. DATA SOURCE shows DISCONNECTED.</p>
                </div>
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">RULE 4 — DATA SOURCE Indicator</h4>
                  <p className="mt-1 text-slate-400">Sidebar shows: UPSTOX LIVE (green) / HISTORICAL REPLAY (amber) / DISCONNECTED (red). Never shows "Simulation" in production.</p>
                </div>
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">RULE 5 — CMP Source & Timestamp</h4>
                  <p className="mt-1 text-slate-400">NIFTY CMP panel shows the value, its source (UPSTOX_LTP), and last update time. Shows "— DISCONNECTED —" if no live token.</p>
                </div>
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">RULE 6 — Structured Logs</h4>
                  <p className="mt-1 text-slate-400">Every backend tick logs: MARKET_OPEN=, DATA_SOURCE=, TRADES_TODAY=, STRATEGY_ALLOWED=, CMP_SOURCE=.</p>
                </div>
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">CPR Calculation</h4>
                  <p className="mt-1 text-slate-400">Pivot = (H+L+C)/3 · BC = (H+L)/2 · TC = Pivot+(Pivot-BC) · R1 = 2×Pivot-Low · S1 = 2×Pivot-High. Computed from live Upstox previous-day OHLC only.</p>
                </div>
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono">Demo Player (Cockpit tab)</h4>
                  <p className="mt-1 text-slate-400">The chart player in the Cockpit tab uses hardcoded sample candles for illustration only. It is clearly labeled DEMO ONLY and has no connection to the live backend engine.</p>
                </div>
              </div>
            </div>
          )}
        </section>

        {/* Right Rail: Risk + Live Positions + Logs */}
        <aside className="w-[310px] border-l border-slate-800 bg-slate-900/20 p-6 flex flex-col gap-6 shrink-0 overflow-y-auto">
          <h2 className="text-xs font-bold text-slate-400 uppercase tracking-widest font-bold mb-1">Risk Control</h2>

          {/* Token expiry warning */}
          {upstoxStatus.connected && upstoxStatus.expiry_status.includes("h") && (
            <div className={`rounded-lg border p-3 text-[11px] font-mono leading-snug ${
              upstoxStatus.expiry_status.includes("0h") || upstoxStatus.expiry_status.includes("1h")
                ? "bg-rose-950/40 border-rose-700 text-rose-300"
                : "bg-amber-950/30 border-amber-700/50 text-amber-300"
            }`}>
              ⏱ Token expires at Upstox midnight IST daily.<br/>
              <span className="text-slate-400">{upstoxStatus.expiry_status}</span>
              {(upstoxStatus.expiry_status.includes("0h") || upstoxStatus.expiry_status.includes("1h")) && (
                <div className="mt-1 text-rose-400 font-bold">⚠ Reconnect now before expiry!</div>
              )}
            </div>
          )}

          {/* RULE 2: Daily trades from backend DB */}
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-slate-900 border border-slate-800 p-4 rounded-lg text-center shadow-sm">
              <div className="text-[10px] text-slate-500 mb-1 font-bold tracking-wider font-mono uppercase">DAILY TRADES</div>
              <div className={`text-xl font-mono font-bold ${realTradeCount >= realMaxTrades ? "text-rose-400" : "text-white"}`}>
                {liveStatus.daily_summary !== null ? `${realTradeCount} / ${realMaxTrades}` : "— / —"}
              </div>
              {realTradeCount >= realMaxTrades && (
                <div className="text-[9px] text-rose-400 font-mono mt-0.5">LIMIT REACHED</div>
              )}
            </div>
            <div className="bg-slate-900 border border-slate-800 p-4 rounded-lg text-center shadow-sm">
              <div className="text-[10px] text-slate-500 mb-1 font-bold tracking-wider font-mono uppercase">TOTAL LOTS</div>
              <div className="text-xl font-mono text-white font-bold">1 LOT</div>
            </div>
          </div>

          {/* Daily PnL from backend */}
          <div>
            <div className="flex justify-between text-xs mb-2">
              <span className="text-slate-400 uppercase font-bold tracking-wider text-[10px]">Daily PnL (Live DB)</span>
              {liveStatus.daily_summary !== null ? (
                <span className={`font-mono font-bold ${realPnL >= 0 ? "text-emerald-400" : "text-rose-500"}`}>
                  {realPnL >= 0 ? "+" : ""}₹{realPnL.toLocaleString()}
                </span>
              ) : (
                <span className="text-slate-500 font-mono">—</span>
              )}
            </div>
            <div className="w-full h-2 bg-slate-800 rounded-full overflow-hidden">
              <div className={`h-full transition-all duration-500 ${realPnL >= 0 ? "bg-emerald-500" : "bg-rose-500"}`}
                style={{ width: `${Math.min(100, Math.max(0, (realPnL + config.lossLimit) / (config.lossLimit * 2) * 100))}%` }}
              />
            </div>
            <div className="flex justify-between text-[10px] text-slate-600 mt-2 font-mono">
              <span>-₹{config.lossLimit}</span>
              <span>+₹{config.lossLimit}</span>
            </div>
          </div>

          {/* LIVE DB POSITIONS — fetched from /api/trades every 15s */}
          <div className="flex flex-col gap-2">
            <div className="flex justify-between items-center pb-1 border-b border-slate-800/40">
              <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                Live Positions
                <span className="text-slate-600 ml-1 font-normal">({liveTradesFromDB.length})</span>
              </h3>
              <button onClick={fetchLiveTrades}
                className="text-[9px] text-indigo-400 hover:text-indigo-300 font-mono cursor-pointer bg-transparent border-0 flex items-center gap-0.5">
                ↻ refresh
              </button>
            </div>

            {liveTradesFromDB.length === 0 ? (
              <div className="p-3 rounded border border-dashed border-slate-800 text-center text-slate-600 text-[10px] font-mono">
                {upstoxStatus.connected ? "No trades today" : "Connect Upstox to trade"}
              </div>
            ) : (
              <div className="space-y-2 max-h-[200px] overflow-y-auto pr-0.5">
                {liveTradesFromDB.slice(0, 10).map(t => (
                  <div key={t.id} className="p-2.5 bg-slate-900 border border-slate-800 rounded-lg text-[10px] font-mono">
                    <div className="flex justify-between items-center mb-1">
                      <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold ${
                        t.option_type === "CE"
                          ? "bg-sky-500/10 text-sky-400 border border-sky-500/20"
                          : "bg-amber-500/10 text-amber-400 border border-amber-500/20"
                      }`}>
                        {t.option_symbol.slice(-12)}
                      </span>
                      <span className={`font-bold ${
                        t.status === "OPEN" ? "text-indigo-400 animate-pulse" :
                        t.status === "CLOSED_TP" ? "text-emerald-400" : "text-rose-400"
                      }`}>{t.status.replace("CLOSED_","")}</span>
                    </div>
                    <div className="grid grid-cols-2 gap-x-2 text-[9px]">
                      <span className="text-slate-500">Entry: <span className="text-slate-300">₹{t.entry_price.toFixed(1)}</span></span>
                      <span className="text-slate-500">SL: <span className="text-slate-300">{t.stop_loss.toFixed(0)}</span></span>
                      {t.status !== "OPEN" && (
                        <span className={`col-span-2 font-bold mt-0.5 ${t.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                          PnL: {t.pnl >= 0 ? "+" : ""}₹{t.pnl.toFixed(0)}
                          {t.is_paper && <span className="text-slate-600 ml-1">[paper]</span>}
                        </span>
                      )}
                    </div>
                    {t.status === "OPEN" && (
                      <button onClick={() => handleManualCloseTrade(t)}
                        className="mt-3 w-full text-[10px] font-bold uppercase rounded-lg py-2 bg-rose-500 text-slate-950 hover:bg-rose-400 transition-all">
                        Manual Close
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* RULE 6: Live activity logs */}
          <div className="flex flex-col flex-1 min-h-0">
            <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2 flex justify-between items-center pb-1.5 border-b border-slate-800/40">
              <span>Live Activity Logs</span>
              <button onClick={() => setSystemLogs([{ timestamp: new Date().toTimeString().split(" ")[0], level: "INFO", msg: "Console cleared." }])}
                className="text-[9px] text-indigo-400 hover:text-indigo-300 font-mono underline cursor-pointer bg-transparent border-0">Clear</button>
            </h3>
            <div ref={scrollRef}
              className="flex-1 overflow-y-auto bg-slate-900/40 border border-slate-800 rounded-lg p-3 font-mono text-[10px] leading-relaxed space-y-1.5 max-h-[200px] shadow-inner">
              {systemLogs.map((log, idx) => {
                const cls: Record<SystemLog["level"], string> = {
                  INFO: "text-slate-500",
                  WARNING: "text-amber-400 font-bold",
                  ERROR: "text-red-400 font-bold",
                  SUCCESS: "text-emerald-400 font-semibold",
                  STRATEGY: "text-indigo-400",
                };
                return (
                  <div key={idx} className="border-b border-slate-800/25 pb-0.5 last:border-none">
                    <span className="text-slate-600">[{log.timestamp}]</span>{" "}
                    <span className={cls[log.level]}>{log.msg}</span>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="mt-2 pt-4 border-t border-slate-800">
            <div className="flex items-center gap-3 text-xs select-none">
              <div className="w-2.5 h-2.5 bg-emerald-500 rounded-full animate-pulse"></div>
              <span className="text-slate-400 italic font-mono text-[10px] uppercase tracking-wider">Telegram Dispatcher Active</span>
            </div>
          </div>
        </aside>
      </main>

      {/* Footer */}
      <footer className="h-10 bg-indigo-900/10 border-t border-slate-800 px-8 flex items-center justify-between text-[10px] font-mono tracking-wider shrink-0 text-slate-500 select-none">
        <div className="flex gap-6">
          <span>DB: <span className="text-slate-300 font-bold">SQLITE</span></span>
          <span>SYMBOL: <span className="text-slate-300 font-bold">NIFTY_50</span></span>
          <span>MAX_TRADES: <span className="text-slate-300 font-bold">{realMaxTrades}</span></span>
          <span>MARKET: <span className={`font-bold ${liveStatus.market_open ? "text-emerald-400" : "text-rose-400"}`}>{liveStatus.market_status}</span></span>
        </div>
        <div className="flex gap-4">
          <span>DATA: <span className={`font-bold ${liveStatus.data_source === "UPSTOX LIVE" ? "text-emerald-400" : "text-rose-400"}`}>{liveStatus.data_source}</span></span>
          <span className="text-emerald-400 animate-pulse font-bold flex items-center gap-1">● SYSTEM PULSE OK</span>
        </div>
      </footer>
    </div>
  );
}
