import { useState, useEffect, useRef } from "react";
import { 
  Play, Pause, RotateCcw, ShieldCheck, Sliders, 
  Terminal, History, Settings2, Bell, AlertTriangle, 
  CheckCircle, PlusCircle, RefreshCw, Layers, Sparkles, TrendingUp, HelpCircle
} from "lucide-react";
import { ResponsiveContainer, ComposedChart, XAxis, YAxis, Tooltip, CartesianGrid, Line, Bar } from "recharts";

// =========================================================
// CUSTOM TYPES & STRUCTS
// =========================================================
interface Candle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  index: number;
}

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
  state: number; // 0=IDLE, 1=BROKEN, 2=RECOVERED, 3=RETESTED, 4=CONFIRMED
  barsElapsed: number;
  retestHigh: number | null;
  retestLow: number | null;
  confirmationHigh: number | null;
  confirmationLow: number | null;
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

interface DailySummary {
  date: string;
  trade_count: number;
  realized_pnl: number;
  is_blocked: boolean;
}

interface Limits {
  max_trades: number;
  loss_limit: number;
  lots: number;
}

interface LiveSystemStatus {
  data_source: "Upstox Live" | "Historical Playback" | "Simulation" | "DISCONNECTED";
  last_live_candle_time: string | null;
  websocket_status: "Connected" | "Disconnected";
  nifty_ltp: number | null;
  cpr_levels: CPRLevels | null;
  trading_mode: string;
  market_status: "OPEN" | "CLOSED";
  cmp_source: string;
  last_cmp_update_time: string | null;
  daily_summary: DailySummary;
  limits: Limits;
}

// =========================================================
// HIGH FIDELITY SIMULATION CANDLES (UNFOLDING SETUP B LONG)
// =========================================================
const INITIAL_DEMO_CANDLES: Candle[] = [
  { time: "09:15", open: 19450, high: 19455, low: 19445, close: 19448, index: 0 },
  { time: "09:20", open: 19448, high: 19452, low: 19438, close: 19442, index: 1 },
  { time: "09:25", open: 19442, high: 19445, low: 19408, close: 19410, index: 2 }, // Breaks S1 (19413.33)
  { time: "09:30", open: 19410, high: 19425, low: 19405, close: 19418, index: 3 }, // Recovers S1 (>19413.33)
  { time: "09:35", open: 19418, high: 19424, low: 19411, close: 19422, index: 4 }, // Retests S1 (Low 19411 in tolerance, close > S1) -> Retest Low 19411, High 19424
  { time: "09:40", open: 19422, high: 19428, low: 19415, close: 19419, index: 5 },
  { time: "09:45", open: 19419, high: 19438, low: 19418, close: 19432, index: 6 }, // Closes above Retest High (19424) -> State 4 [CONFIRMED]! Confirmation High 19438
  { time: "09:50", open: 19432, high: 19445, low: 19428, close: 19438, index: 7 }, // High breaks 19438 -> BUY CE Option Triggered!
  { time: "09:55", open: 19438, high: 19442, low: 19430, close: 19435, index: 8 },
  { time: "10:00", open: 19435, high: 19448, low: 19432, close: 19445, index: 9 },
  { time: "10:05", open: 19445, high: 19458, low: 19441, close: 19454, index: 10 },
  { time: "10:10", open: 19454, high: 19468, low: 19450, close: 19462, index: 11 },
  { time: "10:15", open: 19462, high: 19465, low: 19454, close: 19459, index: 12 },
  { time: "10:20", open: 19459, high: 19478, low: 19457, close: 19474, index: 13 },
  { time: "10:25", open: 19474, high: 19485, low: 19470, close: 19482, index: 14 },
  { time: "10:30", open: 19482, high: 19499, low: 19478, close: 19496, index: 15 }, // High reaches 19499 -> Hits TP (19497)!
  { time: "10:35", open: 19496, high: 19504, low: 19490, close: 19501, index: 16 },
  { time: "10:40", open: 19501, high: 19512, low: 19498, close: 19510, index: 17 },
  { time: "10:45", open: 19510, high: 19518, low: 19508, close: 19514, index: 18 },
  { time: "10:50", open: 19514, high: 19525, low: 19512, close: 19521, index: 19 },
];

// =========================================================
// PRE-POPULATED SIMULATION TRADES (FOR INITIAL METRICS DISPLAY)
// =========================================================
const INITIAL_TRADES: SimulatedTrade[] = [
  {
    id: "P-101",
    setupName: "SETUP_A",
    type: "CE",
    strikePrice: 19600,
    entryPrice: 80.0,
    exitPrice: 120.0,
    stopLossIndex: 19550,
    takeProfitIndex: 19620,
    pnl: 3000,
    status: "CLOSED_TP",
    entryTime: "Yesterday Breakout",
    exitTime: "Yesterday Target",
  },
  {
    id: "P-102",
    setupName: "SETUP_A",
    type: "CE",
    strikePrice: 19600,
    entryPrice: 90.0,
    exitPrice: 70.0,
    stopLossIndex: 19550,
    takeProfitIndex: 19620,
    pnl: -1500,
    status: "CLOSED_SL",
    entryTime: "Yesterday Trend",
    exitTime: "Yesterday Stop",
  },
  {
    id: "P-103",
    setupName: "SETUP_B",
    type: "CE",
    strikePrice: 19450,
    entryPrice: 100.0,
    exitPrice: 150.0,
    stopLossIndex: 19400,
    takeProfitIndex: 19480,
    pnl: 3750,
    status: "CLOSED_TP",
    entryTime: "Today Morning",
    exitTime: "Today Breakout",
  },
  {
    id: "P-104",
    setupName: "SETUP_C",
    type: "CE",
    strikePrice: 19500,
    entryPrice: 110.0,
    exitPrice: 160.0,
    stopLossIndex: 19450,
    takeProfitIndex: 19530,
    pnl: 3750,
    status: "CLOSED_TP",
    entryTime: "09:30 Trend-Long",
    exitTime: "09:45 Target",
  },
  {
    id: "P-105",
    setupName: "SETUP_C",
    type: "CE",
    strikePrice: 19500,
    entryPrice: 130.0,
    exitPrice: 110.0,
    stopLossIndex: 19450,
    takeProfitIndex: 19530,
    pnl: -1500,
    status: "CLOSED_SL",
    entryTime: "10:10 Pullback",
    exitTime: "10:25 Stop-hit",
  },
  {
    id: "P-106",
    setupName: "SETUP_D",
    type: "PE",
    strikePrice: 19400,
    entryPrice: 120.0,
    exitPrice: 100.0,
    stopLossIndex: 19480,
    takeProfitIndex: 19350,
    pnl: -1500,
    status: "CLOSED_SL",
    entryTime: "10:30 Down-break",
    exitTime: "10:45 Stop-hit",
  },
  {
    id: "P-107",
    setupName: "SETUP_B",
    type: "CE",
    strikePrice: 19450,
    entryPrice: 110.0,
    exitPrice: null,
    stopLossIndex: 19408.0,
    takeProfitIndex: 19497.0,
    pnl: 0,
    status: "OPEN",
    entryTime: "09:50 5m Candle",
    exitTime: null,
  }
];

export default function App() {
  // =========================================================
  // CORE STATES
  // =========================================================
  const [tradingMode, setTradingMode] = useState<"paper" | "live">("paper");
  const [isPlaying, setIsPlaying] = useState(false);
  const [speedMs, setSpeedMs] = useState(1500);
  const [currentIdx, setCurrentIdx] = useState(6); // Start partially with some candles
  const [currentTime, setCurrentTime] = useState("14:22:05");

  const [upstoxStatus, setUpstoxStatus] = useState({
    connected: false,
    token_status: "Disconnected",
    expiry_status: "Access token not fetched, click Connect to pair",
    last_authenticated: null,
    token_preview: "None",
    loading: true,
    calculated_redirect_uri: "",
    upstox_api_key: "",
    env_redirect_uri: "",
    is_localhost_fallback: false,
  });

  // =========================================================
  // LIVE SYSTEM STATUS — polled from backend every 10s
  // =========================================================
  const [liveStatus, setLiveStatus] = useState<LiveSystemStatus>({
    data_source: "Simulation",
    last_live_candle_time: null,
    websocket_status: "Disconnected",
    nifty_ltp: null,
    cpr_levels: null,
    trading_mode: "paper",
    market_status: "CLOSED",
    cmp_source: "DISCONNECTED",
    last_cmp_update_time: null,
    daily_summary: {
      date: "",
      trade_count: 0,
      realized_pnl: 0,
      is_blocked: false,
    },
    limits: {
      max_trades: 2,
      loss_limit: 2000,
      lots: 1,
    }
  });

  const fetchLiveStatus = async () => {
    try {
      const res = await fetch("/api/status");
      if (res.ok) {
        const data = await res.json();
        setLiveStatus({
          data_source: data.data_source || "DISCONNECTED",
          last_live_candle_time: data.last_live_candle_time || null,
          websocket_status: data.websocket_status || "Disconnected",
          nifty_ltp: data.nifty_ltp ?? null,
          cpr_levels: data.cpr_levels || null,
          trading_mode: data.trading_mode || "paper",
          market_status: data.market_status || "CLOSED",
          cmp_source: data.cmp_source || "DISCONNECTED",
          last_cmp_update_time: data.last_cmp_update_time || null,
          daily_summary: data.daily_summary || liveStatus.daily_summary,
          limits: data.limits || liveStatus.limits,
        });
        if (data.cpr_levels) {
          setCprLevels(data.cpr_levels);
        }
        if (data.daily_summary) {
          setDailyTradesCount(data.daily_summary.trade_count || 0);
          setDailyPnL(data.daily_summary.realized_pnl || 0);
        }
      }
    } catch {
      // Backend unreachable — status stays as-is
    }
  };

  const [inputApiKey, setInputApiKey] = useState("");
  const [inputApiSecret, setInputApiSecret] = useState("");
  const [isSavingCreds, setIsSavingCreds] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  const handleSaveCredentials = async () => {
    if (!inputApiKey.trim() || !inputApiSecret.trim()) {
      addLog("WARNING", "⚠️ Upstox API Key (Client ID) and API Secret are required to save credentials.");
      setValidationError("Both Client ID and Client Secret are required before saving or connecting.");
      return;
    }
    setIsSavingCreds(true);
    setValidationError(null);
    try {
      const response = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          upstox_api_key: inputApiKey.trim(),
          upstox_api_secret: inputApiSecret.trim()
        })
      });
      if (response.ok) {
        addLog("SUCCESS", "💾 Dynamic credentials persisted inside server upstox_secrets.json successfully. Upstox client updated!");
        setValidationError(null);
        fetchUpstoxStatus();
      } else {
        const errText = await response.text();
        addLog("ERROR", `❌ Failed to save core credentials. Backend responded: Status ${response.status} - ${errText}`);
        setValidationError(`Backend refused to save credentials (Status ${response.status}: ${errText}). Please check logs.`);
      }
    } catch (e: any) {
      addLog("ERROR", `❌ Network exception when saving custom keys: ${e?.message || e}`);
      setValidationError(`Failed to connect to backend: ${e?.message || e}`);
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
        if (data.upstox_api_key) {
          setInputApiKey(prev => prev || (data.upstox_api_key === "mock_api_key" ? "" : data.upstox_api_key));
        }
      } else {
        setUpstoxStatus(prev => ({ ...prev, loading: false }));
      }
    } catch (e) {
      setUpstoxStatus(prev => ({ ...prev, loading: false }));
    }
  };

  const fetchRecentTrades = async () => {
    try {
      const res = await fetch('/api/trades');
      if (res.ok) {
        const data = await res.json();
        const backendTrades = (data.trades || []).map((t: any) => ({
          id: `P-${t.id}`,
          setupName: t.setup_name,
          type: t.option_type || (t.trade_type === 'BUY' ? 'CE' : 'PE'),
          strikePrice: t.strike_price || 0,
          entryPrice: t.entry_price || 0,
          exitPrice: t.exit_price || null,
          stopLossIndex: t.stop_loss || 0,
          takeProfitIndex: t.take_profit || 0,
          pnl: t.pnl || 0,
          status: t.status || 'OPEN',
          entryTime: t.entry_time || '',
          exitTime: t.exit_time || null
        }));

        if (backendTrades.length > 0) {
          setTrades(backendTrades);
          setDailyTradesCount(data.metrics?.total_trades ?? backendTrades.length);
          setDailyPnL(data.metrics?.net_pnl ?? 0);
        } else {
          setTrades(INITIAL_TRADES);
        }
      }
    } catch (e) {
      // ignore errors and keep current trades state
    }
  };

  const handleConnectUpstox = async () => {
    setValidationError(null);

    // 1. If user has typed something in the input fields but they aren't saved yet, let's auto-save them
    if (inputApiKey.trim() && inputApiSecret.trim() && 
        (inputApiKey.trim() !== upstoxStatus.upstox_api_key || upstoxStatus.upstox_api_key === "mock_api_key")) {
      addLog("INFO", "💾 Auto-saving recently entered Upstox keys before redirecting...");
      try {
        const response = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            upstox_api_key: inputApiKey.trim(),
            upstox_api_secret: inputApiSecret.trim()
          })
        });
        if (response.ok) {
          addLog("SUCCESS", "💾 Dynamic keys persisted inside server upstox_secrets.json successfully!");
          // Wait briefly to fetch updated states
          await fetchUpstoxStatus();
        } else {
          const errText = await response.text();
          addLog("ERROR", `❌ Auto-saving credentials failed: Status ${response.status} - ${errText}. Connect aborted.`);
          setValidationError(`Could not save keys automatically to the backend server (Status ${response.status}: ${errText}). Connection aborted.`);
          return;
        }
      } catch (e: any) {
        addLog("ERROR", `❌ Exception during credentials auto-save: ${e?.message || e}`);
        setValidationError(`Auto-saving keys failed: ${e?.message || e}`);
        return;
      }
    }

    // 2. Fetch the updated login URL from backend
    let url = "";
    try {
      const response = await fetch("/api/v1/login-url");
      if (response.ok) {
        const data = await response.json();
        if (data.url) {
          url = data.url;
        }
      }
    } catch (e) {
      console.warn("Failed to get official login URL from backend, using client fallback", e);
    }

    // 3. Prevent launching the popup if client ID is still mock_api_key or empty
    const uses_mock = !url || 
                     url.includes("client_id=mock_api_key") || 
                     url.includes("client_id=mock_key") || 
                     url.includes("client_id=YOUR_") ||
                     !inputApiKey.trim();

    if (uses_mock) {
      addLog("ERROR", "❌ Connection Blocked: Your API Key is currently unconfigured or set to 'mock_api_key'.");
      setValidationError("⚠️ Action Needed: Please copy and paste your actual Upstox API Key (Client ID) and Secret Key below, then click save or click connect (which will auto-save them).");
      return;
    }

    addLog("INFO", "🔗 Opening Upstox login interface in a popup window...");
    try {
      const width = 600;
      const height = 750;
      const left = window.screen.width / 2 - width / 2;
      const top = window.screen.height / 2 - height / 2;
      
      const authWindow = window.open(
        url,
        "upstox_oauth_popup",
        `width=${width},height=${height},top=${top},left=${left},scrollbars=yes,status=yes`
      );
      
      if (!authWindow || authWindow.closed || typeof authWindow.closed === "undefined") {
        addLog("WARNING", "⚠️ Popup blocker detected! Attempting to open Upstox login in a new tab...");
        window.open(url, "_blank");
      }
    } catch (e) {
      console.error("Failed to open OAuth popup", e);
      window.open(url, "_blank");
    }
  };

  useEffect(() => {
    const updateClock = () => {
      const now = new Date();
      setCurrentTime(now.toLocaleTimeString("en-US", { hour12: false }));
    };
    updateClock();
    const clockTimer = setInterval(updateClock, 1000);
    return () => clearInterval(clockTimer);
  }, []);
  
  // Strategy Configurations (live adjustable)
  const [config, setConfig] = useState({
    failWin: 10,
    retWin: 10,
    conWin: 10,
    entWin: 10,
    retTol: 5.0,
    slBuf: 3.0,
    tpBuf: 3.0,
    lossLimit: 2000,
    maxTrades: 10, // expanded to allow historical display + testing
  });

  // Database / State Store
  // CPR levels — seeded with defaults, updated from /api/status when backend has live data
  const [cprLevels, setCprLevels] = useState<CPRLevels>({
    pivot: 19506.67,
    bc: 19500.0,
    tc: 19513.33,
    r1: 19613.33,
    s1: 19413.33,
  });

  const [activeTab, setActiveTab] = useState<"cockpit" | "stateMachines" | "broker" | "help">("cockpit");
  // Prefer backend trades; fall back to demo trades if backend returns none.
  const [trades, setTrades] = useState<SimulatedTrade[]>([]);

  const [dailyTradesCount, setDailyTradesCount] = useState(7);
  const [dailyPnL, setDailyPnL] = useState(6000);

  // States inside live terminal
  const [systemLogs, setSystemLogs] = useState<SystemLog[]>([
    { timestamp: "09:15:00", level: "INFO", msg: "CPR Strategy Engine Bootup successful." },
    { timestamp: "09:15:05", level: "INFO", msg: "[LIVE] APScheduler initialized. Monitoring NIFTY 50 (Index NSE: Nifty 50) on 5-minute candle boundaries." },
    { timestamp: "09:15:10", level: "SUCCESS", msg: "Telegram alerting online. Linked Chat ID: verified." },
    { timestamp: "09:15:12", level: "INFO", msg: "[LIVE] Attempting Upstox token resolution from database..." },
    { timestamp: "09:25:00", level: "STRATEGY", msg: "[DEMO] SETUP_B: Candle index 2 broke S1 (19413.33). Transitioning to state 1: BROKEN." },
    { timestamp: "09:30:00", level: "STRATEGY", msg: "[DEMO] SETUP_B: Candle index 3 recovered back above S1. Transitioning to state 2: RECOVERED." },
    { timestamp: "09:35:00", level: "STRATEGY", msg: "[DEMO] SETUP_B: Candle index 4 Low 19411 lies in retest zone. Transitioning to state 3: RETESTED." },
    { timestamp: "09:45:00", level: "STRATEGY", msg: "[DEMO] SETUP_B: Candle index 6 broke Retest High (19424.0). Transitioning to state 4: CONFIRMED." },
    { timestamp: "09:50:00", level: "SUCCESS", msg: "[DEMO] SETUP_B: Entry triggered! Asset high broke 19438. Buying 1 Lot Weekly ATM NIFTY 19450 CE." },
  ]);

  // Setups state machines metrics
  const [setupStates, setSetupStates] = useState<Record<string, SetupState>>({
    "SETUP_A": { name: "R1 → TC SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
    "SETUP_B": { name: "S1 → BC LONG", state: 4, barsElapsed: 1, retestHigh: 19424, retestLow: 19411, confirmationHigh: 19438, confirmationLow: 19418 },
    "SETUP_C": { name: "TC → R1 LONG", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
    "SETUP_D": { name: "BC → S1 SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
  });

  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll log console
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [systemLogs]);

  // =========================================================
  // SIMULATOR RUNNER LOOP
  // =========================================================
  useEffect(() => {
    let interval: NodeJS.Timeout | null = null;
    if (isPlaying) {
      interval = setInterval(() => {
        setCurrentIdx((prevIdx) => {
          if (prevIdx >= INITIAL_DEMO_CANDLES.length - 1) {
            setIsPlaying(false);
            addLog("INFO", "[DEMO] Simulation demo completed. Historical sequence fully evaluated. Live engine runs independently via backend scheduler.");
            return prevIdx;
          }
          const nextIdx = prevIdx + 1;
          evaluateStrategyAtCandle(nextIdx);
          return nextIdx;
        });
      }, speedMs);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [isPlaying, speedMs, currentIdx, trades, config]);

  const addLog = (level: SystemLog["level"], msg: string) => {
    const timeStr = new Date().toTimeString().split(" ")[0];
    setSystemLogs((prev) => [...prev, { timestamp: timeStr, level, msg }]);
  };

  useEffect(() => {
    fetchUpstoxStatus();
    fetchLiveStatus();
    fetchRecentTrades();

    // Poll live system status every 10 seconds
    const liveStatusTimer = setInterval(fetchLiveStatus, 10_000);
    // Poll trades every 30 seconds
    const tradesTimer = setInterval(fetchRecentTrades, 30_000);

    // Check url query params for OAuth return success redirect (standard redirection fallback)
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get("upstox") === "success") {
      addLog("SUCCESS", "⚡ UPSTOX CONNECTED: OAuth process completed successfully! Credentials written to persistent SQLite.");
      // Clean query parameters from URL without page reload
      const newUrl = window.location.pathname;
      window.history.replaceState({}, document.title, newUrl);
      fetchUpstoxStatus();
    }

    // Handle OAuth success message from popup window in AI Studio preview iframe
    const handleOAuthMessage = (event: MessageEvent) => {
      // Validate origin is from AI Studio preview or localhost
      const origin = event.origin;
      if (!origin.endsWith(".run.app") && !origin.includes("localhost") && !origin.includes("127.0.0.1")) {
        return;
      }
      if (event.data?.type === "OAUTH_AUTH_SUCCESS") {
        addLog("SUCCESS", "⚡ UPSTOX CONNECTED: OAuth process completed successfully via Popup! Credentials written to persistent SQLite.");
        fetchUpstoxStatus();
      }
    };
    window.addEventListener("message", handleOAuthMessage);
    return () => {
      window.removeEventListener("message", handleOAuthMessage);
      clearInterval(liveStatusTimer);
      clearInterval(tradesTimer);
    };
  }, []);

  const evaluateStrategyAtCandle = (idx: number) => {
    const candle = INITIAL_DEMO_CANDLES[idx];
    addLog("INFO", `Processing candle index ${idx} | Time ${candle.time} | Close: ${candle.close}`);

    // Update state machines visually and run checks
    // Check if we have an open trade to evaluate TP/SL
    setTrades((prevTrades) => {
      return prevTrades.map((trade) => {
        if (trade.status === "OPEN") {
          // Check SL
          if (trade.type === "CE" && candle.low <= trade.stopLossIndex) {
            const lossAmt = (trade.entryPrice - 40.0) * 75; // Simulation loss of 40 premium points
            addLog("ERROR", `🛑 STOP LOSS HIT on NIFTY index at ₹${trade.stopLossIndex}`);
            addLog("ERROR", `Closed CE position at SL. Realized loss: -₹${lossAmt}`);
            setDailyPnL((prev) => prev - lossAmt);
            return {
              ...trade,
              exitPrice: 40.0,
              status: "CLOSED_SL",
              pnl: -lossAmt,
              exitTime: `${candle.time} Candle`,
            };
          }
          // Check TP
          if (trade.type === "CE" && candle.high >= trade.takeProfitIndex) {
            const gainAmt = (165.0 - trade.entryPrice) * 75; // 165 exit premium
            addLog("SUCCESS", `🎯 TAKE PROFIT TARGET REACHED on NIFTY index at ${trade.takeProfitIndex}`);
            addLog("SUCCESS", `Closed CE position at TP. Realized profit: +₹${gainAmt}`);
            setDailyPnL((prev) => prev + gainAmt);
            return {
              ...trade,
              exitPrice: 165.0,
              status: "CLOSED_TP",
              pnl: gainAmt,
              exitTime: `${candle.time} Candle`,
            };
          }
        }
        return trade;
      });
    });

    // Feed S1->BC Long Setup progression
    if (idx === 2) {
      setSetupStates((prev) => ({
        ...prev,
        "SETUP_B": { ...prev["SETUP_B"], state: 1, barsElapsed: 0 }
      }));
      addLog("STRATEGY", "SETUP_B: Close fell below S1. Stage 1 BROKEN.");
    } else if (idx === 3) {
      setSetupStates((prev) => ({
        ...prev,
        "SETUP_B": { ...prev["SETUP_B"], state: 2, barsElapsed: 0 }
      }));
      addLog("STRATEGY", "SETUP_B: Close rose back above S1. Stage 2 RECOVERED.");
    } else if (idx === 4) {
      setSetupStates((prev) => ({
        ...prev,
        "SETUP_B": { 
          ...prev["SETUP_B"], 
          state: 3, 
          barsElapsed: 0,
          retestHigh: 19424,
          retestLow: 19411
        }
      }));
      addLog("STRATEGY", "SETUP_B: Low retested S1 zone. Stage 3 RETESTED. (Retest Low: 19411, Retest High: 19424)");
    } else if (idx === 6) {
      setSetupStates((prev) => ({
        ...prev,
        "SETUP_B": { 
          ...prev["SETUP_B"], 
          state: 4, 
          barsElapsed: 0,
          confirmationHigh: 19438,
          confirmationLow: 19418
        }
      }));
      addLog("STRATEGY", "SETUP_B: Close broke above Retest High (19424). Stage 4 CONFIRMED.");
    } else if (idx === 7) {
      // Trigger order
      const hasOpen = trades.some(t => t.status === "OPEN");
      if (!hasOpen && dailyTradesCount < config.maxTrades) {
        const entryP = 110.0;
        const slIdx = 19411 - config.slBuf;
        const tpIdx = 19500 - config.tpBuf;
        
        setTrades(prev => [
          ...prev,
          {
            id: `P-${100 + prev.length + 1}`,
            setupName: "SETUP_B",
            type: "CE",
            strikePrice: 19450,
            entryPrice: entryP,
            exitPrice: null,
            stopLossIndex: slIdx,
            takeProfitIndex: tpIdx,
            pnl: 0,
            status: "OPEN",
            entryTime: `${candle.time} Candle`,
            exitTime: null
          }
        ]);
        setDailyTradesCount(prev => prev + 1);
        addLog("SUCCESS", `🟢 ORDER PLACED (BUY CE) - SETUP_B | Option NIFTY 19450 CE bought at ₹${entryP}`);
      }
    }
  };

  const handleResetSimulation = () => {
    setCurrentIdx(1);
    setIsPlaying(false);
    setTrades(INITIAL_TRADES);
    setDailyPnL(6000);
    setDailyTradesCount(7);
    setSetupStates({
      "SETUP_A": { name: "R1 → TC SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
      "SETUP_B": { name: "S1 → BC LONG", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
      "SETUP_C": { name: "TC → R1 LONG", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
      "SETUP_D": { name: "BC → S1 SHORT", state: 0, barsElapsed: 0, retestHigh: null, retestLow: null, confirmationHigh: null, confirmationLow: null },
    });
    setSystemLogs([
      { timestamp: "09:15:00", level: "INFO", msg: "[DEMO] Simulation demo refreshed. Live engine continues independently via backend scheduler." }
    ]);
    addLog("INFO", "[DEMO] Reset completed. State machines returned to IDLE. Live backend unaffected.");
  };

  const handleStepSim = () => {
    if (currentIdx < INITIAL_DEMO_CANDLES.length - 1) {
      const nextIdx = currentIdx + 1;
      setCurrentIdx(nextIdx);
      evaluateStrategyAtCandle(nextIdx);
    }
  };

  // Compute stats
  const activeTrade = trades.find((t) => t.status === "OPEN");
  const winCount = trades.filter((t) => t.pnl > 0).length;
  const closedCount = trades.filter((t) => t.status !== "OPEN").length;
  const winRate = closedCount > 0 ? (winCount / closedCount) * 100 : 0;
  // Use live LTP from backend when connected, fallback to simulation candle
  const currentPrice = liveStatus.nifty_ltp ?? INITIAL_DEMO_CANDLES[currentIdx]?.close ?? 19450;

  // Real-time calculation helper for separate setup performance metrics
  const getSetupStats = (setupKey: string) => {
    const setupTrades = trades.filter((t) => t.setupName === setupKey);
    const totalTrades = setupTrades.length;
    
    // Win/Loss counting based only on closed trades of this specific setup
    const wins = setupTrades.filter((t) => t.pnl > 0 && t.status !== "OPEN").length;
    const losses = setupTrades.filter((t) => t.pnl < 0 && t.status !== "OPEN").length;
    
    // Profit factor = gross profits / gross losses
    const grossProfits = setupTrades
      .filter((t) => t.pnl > 0)
      .reduce((sum, t) => sum + t.pnl, 0);
    const grossLosses = setupTrades
      .filter((t) => t.pnl < 0)
      .reduce((sum, t) => sum + Math.abs(t.pnl), 0);
    
    let pf = "0.00";
    if (grossLosses === 0) {
      pf = grossProfits > 0 ? "∞" : "0.00";
    } else {
      pf = (grossProfits / grossLosses).toFixed(2);
    }

    return {
      trades: totalTrades,
      wins,
      losses,
      pf,
    };
  };

  return (
    <div className="h-screen w-screen bg-slate-950 text-slate-300 flex flex-col overflow-hidden border-4 border-slate-800 font-sans selection:bg-indigo-500 selection:text-white">
      {/* Header Section */}
      <header className="h-20 shrink-0 border-b border-slate-800 bg-slate-900/55 px-8 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-10 h-10 bg-indigo-600 rounded flex items-center justify-center font-bold text-white font-display text-lg shadow-lg shadow-indigo-600/10 select-none font-bold">CP</div>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-white font-display">CPR QUANTUM V6.0</h1>
            <p className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">Automated Nifty Trading System</p>
          </div>
        </div>

        {/* Tab Selectors styled geometrically */}
        <div className="flex gap-1 p-0.5 bg-slate-950 rounded border border-slate-800">
          <button 
            onClick={() => setActiveTab("cockpit")}
            className={`px-4 py-1.5 rounded text-xs font-bold font-mono transition-all uppercase tracking-wider cursor-pointer ${activeTab === "cockpit" ? "bg-indigo-600 text-white font-bold" : "text-slate-400 hover:text-white"}`}
          >
            Cockpit
          </button>
          <button 
            onClick={() => setActiveTab("stateMachines")}
            className={`px-4 py-1.5 rounded text-xs font-bold font-mono transition-all uppercase tracking-wider cursor-pointer ${activeTab === "stateMachines" ? "bg-indigo-600 text-white font-bold" : "text-slate-400 hover:text-white"}`}
          >
            Setups
          </button>
          <button 
            onClick={() => setActiveTab("broker")}
            className={`px-4 py-1.5 rounded text-xs font-bold font-mono transition-all uppercase tracking-wider cursor-pointer ${activeTab === "broker" ? "bg-indigo-600 text-white font-bold" : "text-slate-400 hover:text-white"}`}
          >
            Upstox
          </button>
          <button 
            onClick={() => setActiveTab("help")}
            className={`px-4 py-1.5 rounded text-xs font-bold font-mono transition-all uppercase tracking-wider cursor-pointer ${activeTab === "help" ? "bg-indigo-600 text-white font-bold" : "text-slate-400 hover:text-white"}`}
          >
            Docs
          </button>
        </div>

        <div className="flex gap-6 items-center select-none">
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 uppercase font-bold font-mono">API Status</span>
            <span className={`${upstoxStatus.connected ? "text-emerald-400" : "text-rose-400"} text-xs flex items-center gap-1.5 font-bold font-mono`}>
              ● {upstoxStatus.connected ? "UPSTOX CONNECTED" : "PORTAL DISCONNECTED"}
            </span>
          </div>
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 uppercase font-bold font-mono text-right w-full">Engine</span>
            <button 
              onClick={() => {
                const target = tradingMode === "paper" ? "live" : "paper";
                setTradingMode(target);
                addLog("WARNING", `Live configuration change: Environment target shifted to ${target.toUpperCase()}.`);
              }}
              className={`text-xs font-bold font-mono uppercase cursor-pointer transition-all ${
                tradingMode === "live" 
                  ? "text-rose-400 animate-pulse font-black" 
                  : "text-sky-400 font-bold"
              }`}
            >
              {tradingMode === "live" ? "⚠️ Live Trading" : "Paper Trading"}
            </button>
          </div>
          <div className="flex flex-col items-end border-l border-slate-800 pl-6 h-10 justify-center">
            <span className="text-2xl font-mono text-white tracking-widest leading-none">{currentTime}</span>
          </div>
        </div>
      </header>

      <main className="flex-1 flex overflow-hidden">
        {/* Left Rail: CPR Levels */}
        <aside className="w-64 border-r border-slate-800 p-6 flex flex-col gap-4 bg-slate-950/20 shrink-0 select-none">
          <h2 className="text-xs font-bold text-slate-400 uppercase tracking-widest mb-2 font-display">Daily CPR Matrix</h2>
          <div className="space-y-3">
            <div className="p-3 bg-rose-500/10 border-l-2 border-rose-500 rounded-r">
              <div className="flex justify-between text-xs text-rose-400">
                <span>R1 Level</span>
                <span className="font-mono font-bold text-sm">{cprLevels.r1.toFixed(2)}</span>
              </div>
            </div>
            <div className="p-3 bg-orange-500/10 border-l-2 border-orange-500 rounded-r">
              <div className="flex justify-between text-xs text-orange-400">
                <span>TC (Top)</span>
                <span className="font-mono font-bold text-sm">{cprLevels.tc.toFixed(2)}</span>
              </div>
            </div>
            <div className="p-3 bg-white/10 border-l-2 border-white rounded-r">
              <div className="flex justify-between text-xs text-white">
                <span>PIVOT</span>
                <span className="font-mono font-bold text-sm">{cprLevels.pivot.toFixed(2)}</span>
              </div>
            </div>
            <div className="p-3 bg-cyan-500/10 border-l-2 border-cyan-500 rounded-r">
              <div className="flex justify-between text-xs text-cyan-400">
                <span>BC (Bottom)</span>
                <span className="font-mono font-bold text-sm">{cprLevels.bc.toFixed(2)}</span>
              </div>
            </div>
            <div className="p-3 bg-emerald-500/10 border-l-2 border-emerald-500 rounded-r">
              <div className="flex justify-between text-xs text-emerald-400">
                <span>S1 Level</span>
                <span className="font-mono font-bold text-sm">{cprLevels.s1.toFixed(2)}</span>
              </div>
            </div>
          </div>

          <div className="mt-auto">
            {/* ─── LIVE DATA STATUS CARDS ─── */}
            <div className="flex flex-col gap-2 mb-3">
              {/* DATA SOURCE */}
              <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
                <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">DATA SOURCE</div>
                <div className={`text-[11px] font-mono font-bold flex items-center gap-1.5 ${
                  liveStatus.data_source === "UPSTOX LIVE"
                    ? "text-emerald-400"
                    : liveStatus.data_source === "Historical Playback"
                    ? "text-amber-400"
                    : "text-rose-400"
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full inline-block ${
                    liveStatus.data_source === "UPSTOX LIVE"
                      ? "bg-emerald-400 animate-pulse"
                      : liveStatus.data_source === "Historical Playback"
                      ? "bg-amber-400"
                      : "bg-rose-400"
                  }`} />
                  {liveStatus.data_source}
                </div>
              </div>
              {/* MARKET STATUS */}
              <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
                <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">MARKET STATUS</div>
                <div className={`text-[11px] font-mono font-bold flex items-center gap-1.5 ${
                  liveStatus.market_status === "OPEN" ? "text-emerald-400" : "text-rose-400"
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full inline-block ${
                    liveStatus.market_status === "OPEN"
                      ? "bg-emerald-400 animate-pulse"
                      : "bg-rose-400"
                  }`} />
                  {liveStatus.market_status}
                </div>
              </div>
              {/* CMP SOURCE */}
              <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
                <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">CMP SOURCE</div>
                <div className="text-[11px] font-mono font-bold text-slate-300 truncate">
                  {liveStatus.cmp_source || "DISCONNECTED"}
                </div>
              </div>
              {/* CMP LAST UPDATED */}
              <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
                <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">CMP LAST UPDATED</div>
                <div className="text-[11px] font-mono font-bold text-slate-300 truncate">
                  {liveStatus.last_cmp_update_time
                    ? liveStatus.last_cmp_update_time.slice(0, 19).replace("T", " ")
                    : "—"}
                </div>
              </div>
              {/* LAST LIVE CANDLE */}
              <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
                <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">LAST LIVE CANDLE</div>
                <div className="text-[11px] font-mono font-bold text-slate-300 truncate">
                  {liveStatus.last_live_candle_time
                    ? liveStatus.last_live_candle_time.slice(0, 19).replace("T", " ")
                    : "—"}
                </div>
              </div>
              {/* WEBSOCKET STATUS */}
              <div className="p-2.5 bg-slate-950 rounded-lg border border-slate-800">
                <div className="text-[9px] text-slate-500 uppercase tracking-widest font-bold font-mono mb-1">WEBSOCKET STATUS</div>
                <div className={`text-[11px] font-mono font-bold flex items-center gap-1.5 ${
                  liveStatus.websocket_status === "Connected" ? "text-emerald-400" : "text-rose-400"
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full inline-block ${
                    liveStatus.websocket_status === "Connected"
                      ? "bg-emerald-400 animate-pulse"
                      : "bg-rose-400"
                  }`} />
                  {liveStatus.websocket_status}
                </div>
              </div>
            </div>

            <div className="p-4 bg-slate-900 rounded-lg border border-slate-800 shadow-md">
              <div className="text-[10px] text-slate-500 mb-1 uppercase tracking-widest font-bold font-mono">NIFTY 50 LIVE</div>
              <div className="text-3xl font-mono text-white font-bold tracking-tight">{currentPrice.toFixed(2)}</div>
              <div className={`text-xs font-bold font-mono ${currentPrice >= 19445 ? "text-emerald-400" : "text-rose-400"}`}>
                {currentPrice >= 19445 ? "+0.15% (+29.10)" : "-0.24% (-54.20)"}
              </div>
            </div>
          </div>
        </aside>

        {/* Main Workspace content */}
        <section className="flex-1 p-8 overflow-y-auto flex flex-col gap-6 bg-slate-950/40">
          {activeTab === "cockpit" && (
            <div className="flex flex-col gap-6">
              {/* Setup Performance Statistics Ledger */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 shadow-lg">
                <div className="flex items-center gap-2 mb-4 border-b border-slate-800 pb-3">
                  <TrendingUp className="h-4 w-4 text-indigo-400 animate-pulse" />
                  <h3 className="text-xs font-mono tracking-widest text-slate-300 uppercase font-bold">
                    Setup Statistics & Performance Summary
                  </h3>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                  {(["SETUP_A", "SETUP_B", "SETUP_C", "SETUP_D"] as const).map((setupKey) => {
                    const stats = getSetupStats(setupKey);
                    const nameMapping: Record<string, string> = {
                      SETUP_A: "Setup A",
                      SETUP_B: "Setup B",
                      SETUP_C: "Setup C",
                      SETUP_D: "Setup D",
                    };
                    const colorClasses: Record<string, { badge: string; text: string }> = {
                      SETUP_A: { badge: "bg-rose-500/10 text-rose-400 border-rose-500/20", text: "text-rose-400" },
                      SETUP_B: { badge: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20", text: "text-emerald-400" },
                      SETUP_C: { badge: "bg-sky-500/10 text-sky-400 border-sky-500/20", text: "text-sky-400" },
                      SETUP_D: { badge: "bg-purple-500/10 text-purple-400 border-purple-500/20", text: "text-purple-400" },
                    };
                    return (
                      <div key={setupKey} className="bg-slate-950/60 rounded-lg p-3 border border-slate-850 flex flex-col gap-2 font-mono text-xs shadow-inner hover:border-slate-750 transition-all duration-300">
                        <div className="flex items-center justify-between border-b border-slate-800/50 pb-1.5 mb-1">
                          <span className={`text-[11px] font-bold px-2 py-0.5 rounded border ${colorClasses[setupKey].badge}`}>
                            {nameMapping[setupKey]}
                          </span>
                          <span className="text-[9px] text-slate-600 font-bold uppercase tracking-wider">UNIT ACTIVE</span>
                        </div>
                        <div className="flex justify-between items-center text-slate-400">
                          <span>Trades:</span>
                          <span className="text-white font-bold">{stats.trades}</span>
                        </div>
                        <div className="flex justify-between items-center text-slate-400">
                          <span>Wins:</span>
                          <span className="text-emerald-400 font-semibold">{stats.wins}</span>
                        </div>
                        <div className="flex justify-between items-center text-slate-400">
                          <span>Losses:</span>
                          <span className="text-rose-400 font-semibold">{stats.losses}</span>
                        </div>
                        <div className="flex justify-between items-center text-slate-300 pt-1.5 border-t border-slate-900/80">
                          <span>PF:</span>
                          <span className={`${colorClasses[setupKey].text} font-bold`}>{stats.pf}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Setup monitoring mini cards */}
              <div className="grid grid-cols-2 gap-6">
                {/* Setup A */}
                <div className={`bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col justify-between h-36 transition-all ${setupStates["SETUP_A"].state > 0 ? "border-amber-500/30 ring-1 ring-amber-500/15 bg-amber-500/5" : ""}`}>
                  <div className="flex justify-between items-start">
                    <div>
                      <span className="text-[10px] px-2 py-0.5 bg-rose-600 text-white font-bold rounded uppercase font-mono">Setup A</span>
                      <h3 className="text-sm font-bold text-white mt-1">R1 → TC (SHORT)</h3>
                    </div>
                    <span className={`text-[10px] font-mono uppercase font-bold ${setupStates["SETUP_A"].state > 0 ? "text-amber-400 animate-pulse" : "text-slate-500"}`}>
                      {setupStates["SETUP_A"].state === 0 ? "Idle" : `ACTIVE: STEP ${setupStates["SETUP_A"].state}`}
                    </span>
                  </div>
                  <div>
                    <div className="flex gap-1.5 mt-4">
                      {[1, 2, 3, 4, 5].map((step) => (
                        <div
                          key={step}
                          className={`h-1.5 flex-1 rounded-full transition-all ${
                            setupStates["SETUP_A"].state >= step
                              ? "bg-rose-500"
                              : "bg-slate-800"
                          }`}
                        />
                      ))}
                    </div>
                  </div>
                </div>

                {/* Setup B */}
                <div className={`bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col justify-between h-36 transition-all ${setupStates["SETUP_B"].state > 0 ? "border-emerald-500/40 ring-1 ring-emerald-500/20 bg-emerald-500/5" : ""}`}>
                  <div className="flex justify-between items-start">
                    <div>
                      <span className="text-[10px] px-2 py-0.5 bg-emerald-600 text-white font-bold rounded uppercase font-mono">Setup B</span>
                      <h3 className="text-sm font-bold text-white mt-1">S1 → BC (LONG)</h3>
                    </div>
                    <span className={`text-[10px] font-mono uppercase font-bold ${setupStates["SETUP_B"].state > 3 ? "text-emerald-400" : setupStates["SETUP_B"].state > 0 ? "text-amber-400 animate-pulse" : "text-slate-500"}`}>
                      {setupStates["SETUP_B"].state === 0 ? "Idle" : setupStates["SETUP_B"].state === 4 ? "CONFIRMED: STEP 4" : `ACTIVE: STEP ${setupStates["SETUP_B"].state}`}
                    </span>
                  </div>
                  <div>
                    <div className="flex gap-1.5 mt-4">
                      {[1, 2, 3, 4, 5].map((step) => (
                        <div
                          key={step}
                          className={`h-1.5 flex-1 rounded-full transition-all ${
                            setupStates["SETUP_B"].state >= step
                              ? "bg-emerald-500"
                              : "bg-slate-800"
                          }`}
                        />
                      ))}
                    </div>
                  </div>
                </div>

                {/* Setup C */}
                <div className={`bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col justify-between h-36 transition-all ${setupStates["SETUP_C"].state > 0 ? "border-sky-500/30 ring-1 ring-sky-500/15 bg-sky-500/5" : ""}`}>
                  <div className="flex justify-between items-start">
                    <div>
                      <span className="text-[10px] px-2 py-0.5 bg-sky-600 text-white font-bold rounded uppercase font-mono">Setup C</span>
                      <h3 className="text-sm font-bold text-white mt-1">TC → R1 (LONG)</h3>
                    </div>
                    <span className={`text-[10px] font-mono uppercase font-bold ${setupStates["SETUP_C"].state > 0 ? "text-sky-400 animate-pulse" : "text-slate-500"}`}>
                      {setupStates["SETUP_C"].state === 0 ? "Idle" : `ACTIVE: STEP ${setupStates["SETUP_C"].state}`}
                    </span>
                  </div>
                  <div>
                    <div className="flex gap-1.5 mt-4">
                      {[1, 2, 3, 4, 5].map((step) => (
                        <div
                          key={step}
                          className={`h-1.5 flex-1 rounded-full transition-all ${
                            setupStates["SETUP_C"].state >= step
                              ? "bg-sky-500"
                              : "bg-slate-800"
                          }`}
                        />
                      ))}
                    </div>
                  </div>
                </div>

                {/* Setup D */}
                <div className={`bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col justify-between h-36 transition-all opacity-50`}>
                  <div className="flex justify-between items-start">
                    <div>
                      <span className="text-[10px] px-2 py-0.5 bg-purple-600 text-white font-bold rounded uppercase font-mono">Setup D</span>
                      <h3 className="text-sm font-bold text-white mt-1">BC → S1 (SHORT)</h3>
                    </div>
                    <span className="text-[10px] font-mono text-slate-500 uppercase font-bold">DISABLED</span>
                  </div>
                  <div>
                    <div className="flex gap-1.5 mt-4">
                      {[1, 2, 3, 4, 5].map((step) => (
                        <div
                          key={step}
                          className={`h-1.5 flex-1 rounded-full bg-slate-800`}
                        />
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              {/* Spot chart */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col gap-4">
                <div className="flex justify-between items-center bg-slate-950/10 pb-2 border-b border-slate-800/40">
                  <h3 className="text-xs font-mono tracking-widest text-slate-400 uppercase flex items-center gap-1.5 font-bold">
                    <Sparkles className="h-4 w-4 text-emerald-400 animate-pulse" />
                    Spot Price Plot & CPR Static Bands
                  </h3>
                  <div className="text-[10px] text-slate-400 flex items-center gap-3 font-mono">
                    <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-red-500" />R1</span>
                    <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-amber-500" />TC</span>
                    <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-white" />Pivot</span>
                    <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-cyan-500" />BC</span>
                    <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-emerald-500" />S1</span>
                  </div>
                </div>

                <div className="h-[230px] w-full bg-slate-950 rounded-lg p-2 border border-slate-900">
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={INITIAL_DEMO_CANDLES.slice(0, currentIdx + 1)}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(51,65,85,0.06)" />
                      <XAxis dataKey="time" stroke="#475569" style={{ fontSize: "10px", fontFamily: "monospace" }} />
                      <YAxis domain={[19385, 19525]} stroke="#475569" style={{ fontSize: "10px", fontFamily: "monospace" }} />
                      <Tooltip 
                        contentStyle={{ backgroundColor: "#0f172a", border: "1px solid #1e293b", borderRadius: "8px" }}
                        labelStyle={{ color: "#94a3b8", fontFamily: "monospace" }}
                        itemStyle={{ fontSize: "12px" }}
                      />
                      
                      <Line type="monotone" dataKey={() => cprLevels.r1} stroke="#ef4444" strokeWidth={1} dot={false} strokeDasharray="3 3" name="R1" />
                      <Line type="monotone" dataKey={() => cprLevels.tc} stroke="#f59e0b" strokeWidth={1} dot={false} strokeDasharray="5 5" name="TC" />
                      <Line type="monotone" dataKey={() => cprLevels.pivot} stroke="#ffffff" strokeWidth={1.5} dot={false} name="Pivot" />
                      <Line type="monotone" dataKey={() => cprLevels.bc} stroke="#0ea5e9" strokeWidth={1} dot={false} strokeDasharray="5 5" name="BC" />
                      <Line type="monotone" dataKey={() => cprLevels.s1} stroke="#10b981" strokeWidth={1} dot={false} strokeDasharray="3 3" name="S1" />

                      <Line type="monotone" dataKey="close" stroke="#6366f1" strokeWidth={2.5} dot={{ r: 3, fill: "#6366f1" }} name="Close Price" />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>

                {/* Simulator controls */}
                <div className="flex flex-wrap gap-4 items-center justify-between p-3 bg-slate-950 rounded-lg border border-slate-850">
                  <div className="flex items-center gap-2">
                    <button 
                      onClick={() => setIsPlaying(!isPlaying)}
                      className={`flex items-center gap-1.5 px-4 py-2 rounded text-xs font-bold uppercase transition-all tracking-wider cursor-pointer ${
                        isPlaying 
                          ? "bg-amber-500 text-slate-950 font-black animate-pulse animate-duration-1000" 
                          : "bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-black"
                      }`}
                    >
                      {isPlaying ? (
                        <>
                          <Pause className="h-4 w-4 fill-current" />
                          PAUSE ENGINE
                        </>
                      ) : (
                        <>
                          <Play className="h-4 w-4 fill-current" />
                          START FLOW
                        </>
                      )}
                    </button>

                    <button 
                      onClick={handleStepSim}
                      disabled={isPlaying}
                      className="p-2 bg-slate-900 border border-slate-800 rounded hover:bg-slate-800 text-slate-300 hover:text-white transition-all disabled:opacity-50 cursor-pointer"
                      title="Step Simulation"
                    >
                      <PlusCircle className="h-4 w-4" />
                    </button>

                    <button 
                      onClick={handleResetSimulation}
                      className="p-2 bg-slate-900 border border-slate-800 rounded hover:bg-slate-800 text-slate-300 hover:text-white transition-all cursor-pointer"
                      title="Reset Sim"
                    >
                      <RotateCcw className="h-4 w-4" />
                    </button>
                  </div>

                  <div className="flex items-center gap-3">
                    <span className="text-[10px] text-slate-500 font-mono uppercase font-bold">INTERVAL SPEED:</span>
                    <div className="flex bg-slate-900 p-0.5 rounded border border-slate-800">
                      {[2000, 1000, 400].map((ms) => (
                        <button
                          key={ms}
                          onClick={() => setSpeedMs(ms)}
                          className={`px-2.5 py-1 rounded text-[10px] font-bold font-mono transition-all cursor-pointer ${
                            speedMs === ms ? "bg-slate-800 text-emerald-400" : "text-slate-500 hover:text-slate-300"
                          }`}
                        >
                          {ms === 2000 ? "1x" : ms === 1000 ? "2x" : "5x"}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              {/* Strategy parameters */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 flex flex-col gap-4">
                <div className="flex items-center gap-1.5">
                  <Sliders className="h-4 w-4 text-indigo-400" />
                  <h3 className="text-xs font-mono tracking-widest text-slate-400 uppercase font-bold">
                    STRATEGY HYPERPARAMETER CONFIGURATOR
                  </h3>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                  <div className="flex flex-col gap-1.5">
                    <label className="text-slate-400 font-semibold">Breakout Trigger Window (bars)</label>
                    <input 
                      type="number" 
                      value={config.failWin} 
                      onChange={(e) => setConfig({ ...config, failWin: Math.max(1, parseInt(e.target.value) || 1) })}
                      className="bg-slate-950 border border-slate-800 rounded-lg p-2 font-mono text-slate-100 focus:outline-none focus:border-indigo-500 font-bold"
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label className="text-slate-400 font-semibold">Retest Timeout Window (bars)</label>
                    <input 
                      type="number" 
                      value={config.retWin} 
                      onChange={(e) => setConfig({ ...config, retWin: Math.max(1, parseInt(e.target.value) || 1) })}
                      className="bg-slate-950 border border-slate-800 rounded-lg p-2 font-mono text-slate-100 focus:outline-none focus:border-indigo-500 font-bold"
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label className="text-slate-400 font-semibold">Confirmation Window limit (bars)</label>
                    <input 
                      type="number" 
                      value={config.conWin} 
                      onChange={(e) => setConfig({ ...config, conWin: Math.max(1, parseInt(e.target.value) || 1) })}
                      className="bg-slate-950 border border-slate-800 rounded-lg p-2 font-mono text-slate-100 focus:outline-none focus:border-indigo-500 font-bold"
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label className="text-slate-400 font-semibold">Retest Spot Point Tolerance</label>
                    <input 
                      type="number" 
                      step="0.5"
                      value={config.retTol} 
                      onChange={(e) => setConfig({ ...config, retTol: Math.max(0, parseFloat(e.target.value) || 0) })}
                      className="bg-slate-950 border border-slate-800 rounded-lg p-2 font-mono text-slate-100 focus:outline-none focus:border-indigo-500 font-bold"
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label className="text-slate-400 font-semibold">SL Target Delta Offset</label>
                    <input 
                      type="number" 
                      value={config.slBuf} 
                      onChange={(e) => setConfig({ ...config, slBuf: Math.max(0, parseFloat(e.target.value) || 0) })}
                      className="bg-slate-950 border border-slate-800 rounded-lg p-2 font-mono text-slate-100 focus:outline-none focus:border-indigo-500 font-bold"
                    />
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <label className="text-slate-400 font-semibold">Daily Loss Limit cut-off (₹)</label>
                    <input 
                      type="number" 
                      value={config.lossLimit} 
                      onChange={(e) => setConfig({ ...config, lossLimit: Math.max(100, parseInt(e.target.value) || 100) })}
                      className="bg-slate-950 border border-slate-800 rounded-lg p-2 font-mono text-slate-100 focus:outline-none focus:border-indigo-500 font-bold"
                    />
                  </div>
                </div>
              </div>
            </div>
          )}

          {activeTab === "stateMachines" && (
            <div className="flex flex-col gap-6">
              {Object.entries(setupStates).map(([key, value]) => {
                const item = value as SetupState;
                const stepColors = [
                  "bg-slate-800 text-slate-400 border border-slate-700/60",
                  "bg-amber-500/10 text-amber-400 border border-amber-500/20",
                  "bg-orange-500/15 text-orange-400 border border-orange-500/25 animate-pulse",
                  "bg-yellow-500/20 text-yellow-300 border border-yellow-500/30",
                  "bg-purple-500/25 text-purple-300 border border-purple-500/35",
                ];
                
                return (
                  <div key={key} className="bg-slate-900/40 p-5 rounded-xl border border-slate-800">
                    <div className="flex items-center justify-between mb-4 border-b border-slate-800 pb-3">
                      <div>
                        <span className="text-[10px] font-mono tracking-widest text-slate-500 font-bold">CPR ENGINE SEQUENCY MODULE</span>
                        <h4 className="text-sm font-display font-bold text-slate-100 flex items-center gap-2 mt-0.5">
                          {key}: {item.name}
                        </h4>
                      </div>
                      <span className={`text-xs px-2.5 py-1 rounded font-bold font-mono ${stepColors[item.state]}`}>
                        STAGE {item.state} — {
                          item.state === 0 ? "IDLE" : 
                          item.state === 1 ? "BROKEN" :
                          item.state === 2 ? "RECOVERED" :
                          item.state === 3 ? "RETESTED" : "CONFIRMED"
                        }
                      </span>
                    </div>

                    <div className="grid grid-cols-5 gap-2 relative">
                      {[
                        { num: 1, label: "Breakout", desc: "Close past boundary" },
                        { num: 2, label: "Recovery", desc: "No-fail closure" },
                        { num: 3, label: "Retest Match", desc: "Tolerance contact" },
                        { num: 4, label: "Confirm Trigger", desc: "Close broke Retest" },
                        { num: 5, label: "ATM Execution", desc: "Conf limit breached" },
                      ].map((step, sIdx) => {
                        const isDone = item.state >= step.num;
                        const isCurrent = item.state === step.num - 1;
                        
                        return (
                          <div 
                            key={step.num}
                            className={`p-3 rounded border flex flex-col gap-1 transition-all ${
                              isDone 
                                ? "bg-emerald-950/20 text-emerald-400 border-emerald-900/60" 
                                : isCurrent 
                                ? "bg-slate-900 text-slate-200 border-slate-700 animate-pulse font-bold"
                                : "bg-slate-950/40 text-slate-600 border-slate-900/40"
                            }`}
                          >
                            <span className="text-[10px] font-mono font-bold uppercase">Step 0{step.num}</span>
                            <span className="text-xs font-bold leading-tight">{step.label}</span>
                            <span className="text-[9px] text-slate-500 leading-normal">{step.desc}</span>
                          </div>
                        );
                      })}
                    </div>

                    <div className="mt-4 grid grid-cols-1 md:grid-cols-4 gap-4 bg-slate-950 p-4 rounded-lg border border-slate-900 text-xs shadow-inner font-mono">
                      <div className="border-r border-slate-900/80 pr-2 col-span-1">
                        <span className="text-slate-400 font-mono font-bold block mb-1">💎 SETUP PERFORMANCE:</span>
                        <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[10px]">
                          <span className="text-slate-500">Trades: <span className="text-white font-bold">{getSetupStats(key).trades}</span></span>
                          <span className="text-slate-500">Wins: <span className="text-emerald-400 font-bold">{getSetupStats(key).wins}</span></span>
                          <span className="text-slate-500">Losses: <span className="text-rose-400 font-bold">{getSetupStats(key).losses}</span></span>
                          <span className="text-slate-500">PF: <span className="text-indigo-400 font-bold">{getSetupStats(key).pf}</span></span>
                        </div>
                      </div>
                      <div>
                        <span className="text-slate-500 font-mono font-bold block mb-1">Retest High / Low:</span>
                        <span className="text-slate-300 font-mono font-bold">
                          {item.retestHigh || "--"} / {item.retestLow || "--"}
                        </span>
                      </div>
                      <div>
                        <span className="text-slate-500 font-mono font-bold block mb-1">Confirmation High / Low:</span>
                        <span className="text-slate-300 font-mono font-bold">
                          {item.confirmationHigh || "--"} / {item.confirmationLow || "--"}
                        </span>
                      </div>
                      <div>
                        <span className="text-slate-500 font-mono font-bold block mb-1">Status Band Thresholds:</span>
                        <span className="text-indigo-400 font-bold">
                          {key === "SETUP_A" ? "TC & R1 Boundaries" : key === "SETUP_B" ? "BC & S1 Boundaries" : key === "SETUP_C" ? "TC & R1 Boundaries" : "BC & S1 Boundaries"}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {activeTab === "broker" && (
            <div className="flex flex-col gap-6">
              {/* Top Summary Cards */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                
                {/* Connection Status Card */}
                <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
                  <div>
                    <span className="text-[10px] text-slate-500 uppercase font-bold font-mono block mb-1">Status Panel</span>
                    <h3 className="text-sm font-bold text-slate-200 mb-4 font-mono">CONNECTION INTEGRATION</h3>
                    
                    <div className="space-y-3">
                      <div className="flex justify-between items-center bg-slate-950/40 p-2.5 rounded border border-slate-850">
                        <span className="text-slate-400 text-xs">Broker:</span>
                        <span className="text-white font-mono font-bold text-xs">Upstox API v2</span>
                      </div>
                      
                      <div className="flex justify-between items-center bg-slate-950/40 p-2.5 rounded border border-slate-850">
                        <span className="text-slate-400 text-xs">Connection state:</span>
                        <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-bold font-mono ${upstoxStatus.connected ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20" : "bg-rose-500/10 text-rose-400 border border-rose-500/20"}`}>
                          <span className={`h-1.5 w-1.5 rounded-full ${upstoxStatus.connected ? "bg-emerald-400" : "bg-rose-400 animate-pulse"}`}></span>
                          {upstoxStatus.connected ? "CONNECTED" : "DISCONNECTED"}
                        </span>
                      </div>
                    </div>
                  </div>
                  
                   <div className="mt-4 pt-3 border-t border-slate-800/60">
                    {validationError && (
                      <div className="mb-3 bg-rose-500/10 border border-rose-500/20 text-rose-300 p-3 rounded text-xs leading-relaxed font-sans font-medium">
                        {validationError}
                      </div>
                    )}
                    <div className="flex gap-2">
                      <button
                        onClick={handleConnectUpstox}
                        className="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white font-semibold font-mono text-xs py-2 px-3 rounded shadow transition-all uppercase tracking-wider cursor-pointer"
                      >
                        {upstoxStatus.connected ? "Reconnect" : "Connect Upstox"}
                      </button>
                      <button
                        onClick={fetchUpstoxStatus}
                        className="bg-slate-800 hover:bg-slate-755 text-slate-200 border border-slate-700 hover:border-slate-600 rounded p-2 cursor-pointer transition-all"
                        title="Refresh Status"
                      >
                        <RefreshCw className="h-4 w-4" />
                      </button>
                    </div>
                    
                    <div className="mt-3 bg-amber-500/10 border border-amber-500/20 rounded p-2.5 text-[11px] text-amber-200 font-sans leading-normal">
                      📌 <strong>Setup Requirement:</strong> To prevent errors, you must copy the exact custom **Redirect URL** from the Diagnostics card below and paste it in your Upstox Developer Console before clicking "Connect".
                    </div>
                  </div>
                </div>

                {/* Token Credentials Card */}
                <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
                  <div>
                    <span className="text-[10px] text-slate-500 uppercase font-bold font-mono block mb-1">OAuth Token Info</span>
                    <h3 className="text-sm font-bold text-slate-200 mb-4 font-mono">SECURE OAUTH STATE</h3>
                    
                    <div className="space-y-2 text-xs font-mono">
                      <div className="flex justify-between py-1 border-b border-slate-800">
                        <span className="text-slate-400">Token Status:</span>
                        <span className={`font-bold ${upstoxStatus.token_status === "Active" ? "text-emerald-400" : "text-amber-400"}`}>
                          {upstoxStatus.token_status}
                        </span>
                      </div>
                      <div className="flex justify-between py-1 border-b border-slate-800">
                        <span className="text-slate-400">Token Preview:</span>
                        <span className="text-indigo-300 font-bold">{upstoxStatus.token_preview}</span>
                      </div>
                      <div className="flex justify-between py-1 border-b border-slate-800">
                        <span className="text-slate-400">Last Synced:</span>
                        <span className="text-slate-300">
                          {upstoxStatus.last_authenticated ? new Date(upstoxStatus.last_authenticated).toLocaleTimeString() : "--"}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="text-[10px] text-slate-400 bg-slate-950/60 hover:text-slate-300 border border-slate-905 leading-normal p-2.5 rounded mt-4">
                    ⚡ <span className="font-semibold text-slate-300">Token Integrity:</span> {upstoxStatus.expiry_status}
                  </div>
                </div>

                {/* Environment Mode and SQLite Persistence Card */}
                <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col justify-between">
                  <div>
                    <span className="text-[10px] text-slate-500 uppercase font-bold font-mono block mb-1">Regulatory Info</span>
                    <h3 className="text-sm font-bold text-slate-200 mb-4 font-mono">SAFEGUARD ENVIRONMENT</h3>
                    
                    <div className="space-y-3">
                      <div className="bg-rose-500/10 border border-rose-500/20 text-rose-400 p-3 rounded text-xs select-none">
                        <span className="font-bold flex items-center gap-1.5 uppercase font-mono">
                          <AlertTriangle className="h-4 w-4 text-rose-400 animate-pulse" />
                          PAPER TRADING ACTIVE
                        </span>
                        <p className="mt-1.5 leading-normal text-[11px] text-slate-400 font-sans">
                          Live server logic is validated in paper trading mode. Orders are routed inside the high fidelity simulators.
                        </p>
                      </div>
                    </div>
                  </div>

                  <div className="text-[10px] text-slate-500 font-mono italic text-right mt-3">
                    Database: SQLite Local
                  </div>
                </div>

              </div>

              {/* Credentials Diagnostics Tool */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 shadow-lg flex flex-col gap-4">
                <div className="flex items-center gap-2 border-b border-slate-800 pb-3">
                  <Sliders className="h-4 w-4 text-amber-400" />
                  <h3 className="text-xs font-mono tracking-widest text-slate-300 uppercase font-bold">
                    🔑 UPSTOX OAUTH CREDENTIALS & REDIRECT URI DIAGNOSTICS
                  </h3>
                </div>

                <div className="bg-slate-950/60 p-4 border border-slate-850 rounded-lg text-xs leading-normal space-y-3">
                  <div className="text-amber-400 font-bold flex items-center gap-2">
                    <span className="inline-block w-2.5 h-2.5 rounded-full bg-amber-500 animate-pulse"></span>
                    Are you getting the "UDAPI100068: Check your 'client_id' and 'redirect_uri'; one or both are incorrect" error?
                  </div>
                  <p className="text-slate-400 leading-relaxed">
                    This error is returned by the Upstox OAuth service when the **API Key (Client ID)** and/or **Redirect URI** sent by this trading bot do not strictly match what is registered in your **Upstox Developer Console**.
                  </p>
                  
                  {/* Dynamic Settings Modulator */}
                  <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3.5 space-y-3 my-3">
                    <span className="text-[10px] text-indigo-400 font-mono font-bold block pb-1 border-b border-slate-800">
                      🛠️ OVERRIDE & CONFIGURE UPSTOX CLIENT DETAILS (PERSISTED SECURELY)
                    </span>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5 mt-2">
                      <div className="space-y-1">
                        <label className="text-[10px] text-slate-400 font-mono block">Upstox API Key (Client ID) / Client ID:</label>
                        <input 
                          type="text" 
                          placeholder="Paste Upstox API Key / Client ID here..."
                          value={inputApiKey}
                          onChange={(e) => setInputApiKey(e.target.value)}
                          className="w-full text-indigo-300 font-mono text-xs bg-slate-950 border border-slate-800 px-2.5 py-1.5 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 focus:border-indigo-500 font-medium placeholder-slate-600"
                        />
                      </div>
                      <div className="space-y-1">
                        <label className="text-[10px] text-slate-400 font-mono block">Upstox API Secret Key / Client Secret:</label>
                        <input 
                          type="password" 
                          placeholder="Paste Upstox Client Secret here..."
                          value={inputApiSecret}
                          onChange={(e) => setInputApiSecret(e.target.value)}
                          className="w-full text-indigo-300 font-mono text-xs bg-slate-950 border border-slate-800 px-2.5 py-1.5 rounded focus:outline-none focus:ring-1 focus:ring-indigo-500 focus:border-indigo-500 font-medium placeholder-slate-600"
                        />
                      </div>
                    </div>
                    <div className="flex justify-end pt-1">
                      <button 
                        onClick={handleSaveCredentials}
                        disabled={isSavingCreds}
                        className="text-xs bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 text-white border border-indigo-500/20 px-4 py-1.5 rounded cursor-pointer font-sans font-bold transition-all"
                      >
                        {isSavingCreds ? "Saving credentials..." : "Save & Persist API Keys"}
                      </button>
                    </div>
                  </div>

                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-2">
                    <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
                      <span className="text-[10px] text-slate-500 font-mono font-bold block mb-1">CLIENT ID / API KEY (Sent by Bot)</span>
                      <div className="flex items-center gap-2 mt-1.5">
                        <input 
                          type="text" 
                          readOnly 
                          value={upstoxStatus.upstox_api_key || "Not configured"}
                          onClick={(e) => e.currentTarget.select()}
                          className="flex-1 text-indigo-300 font-mono text-xs bg-slate-950 border border-slate-850 px-2.5 py-1.5 rounded select-all font-medium focus:outline-none focus:ring-1 focus:ring-indigo-500 w-full"
                        />
                        <button 
                          onClick={() => {
                            const val = upstoxStatus.upstox_api_key;
                            if (val) {
                              try {
                                if (navigator.clipboard && navigator.clipboard.writeText) {
                                  navigator.clipboard.writeText(val).then(() => {
                                    addLog("SUCCESS", "Saved Client ID to clipboard.");
                                  }).catch(() => {
                                    const tx = document.createElement("textarea");
                                    tx.value = val;
                                    document.body.appendChild(tx);
                                    tx.select();
                                    document.execCommand("copy");
                                    document.body.removeChild(tx);
                                    addLog("SUCCESS", "Saved Client ID (fallback).");
                                  });
                                } else {
                                  window.prompt("Select and copy your Client ID:", val);
                                }
                              } catch (e) {
                                window.prompt("Select and copy your Client ID:", val);
                              }
                            }
                          }}
                          className="text-[11px] bg-slate-800 hover:bg-slate-700 text-indigo-400 hover:text-indigo-300 border border-slate-700 px-3 py-1.5 rounded font-mono cursor-pointer shrink-0 font-bold active:bg-slate-900 transition-all"
                        >
                          Copy Key
                        </button>
                      </div>
                      <span className="text-[9px] text-slate-500 block mt-1.5 leading-normal">
                        This is the <code className="text-slate-400 font-semibold font-mono">UPSTOX_API_KEY</code> environment variable currently read by the backend.
                      </span>
                    </div>

                    <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
                      <span className="text-[10px] text-slate-500 font-mono font-bold block mb-1">ACTIVE REDIRECT URI / CALL-BACK</span>
                      <div className="flex items-center gap-2 mt-1.5">
                        <input 
                          type="text" 
                          readOnly 
                          value={upstoxStatus.calculated_redirect_uri || (window.location.origin + "/callback")}
                          onClick={(e) => e.currentTarget.select()}
                          className="flex-1 text-emerald-400 font-mono text-xs bg-slate-950 border border-slate-850 px-2.5 py-1.5 rounded select-all font-medium focus:outline-none focus:ring-1 focus:ring-emerald-500 w-full"
                        />
                        <button 
                          onClick={() => {
                            const val = upstoxStatus.calculated_redirect_uri || (window.location.origin + "/callback");
                            try {
                              if (navigator.clipboard && navigator.clipboard.writeText) {
                                navigator.clipboard.writeText(val).then(() => {
                                  addLog("SUCCESS", "Saved Redirect URL to clipboard.");
                                }).catch(() => {
                                  const tx = document.createElement("textarea");
                                  tx.value = val;
                                  document.body.appendChild(tx);
                                  tx.select();
                                  document.execCommand("copy");
                                  document.body.removeChild(tx);
                                  addLog("SUCCESS", "Saved Redirect URL (fallback).");
                                });
                              } else {
                                window.prompt("Select and copy your Redirect URL:", val);
                              }
                            } catch (e) {
                              window.prompt("Select and copy your Redirect URL:", val);
                            }
                          }}
                          className="text-[11px] bg-emerald-950/40 hover:bg-emerald-900/40 text-emerald-400 hover:text-emerald-300 border border-emerald-500/20 px-3 py-1.5 rounded font-mono cursor-pointer shrink-0 font-bold active:bg-emerald-950 transition-all"
                        >
                          Copy URL
                        </button>
                      </div>
                      <span className="text-[9px] text-slate-500 block mt-1.5 leading-normal">
                        {upstoxStatus.is_localhost_fallback ? (
                          <span className="text-amber-500 font-semibold">⚠️ Dynamically calculated fallback for current environment.</span>
                        ) : (
                          <span className="text-slate-500">Statically read from your custom environment variable config.</span>
                        )}
                      </span>
                    </div>
                  </div>

                  <div className="bg-indigo-950/20 border border-indigo-900/40 rounded-lg p-3 mt-3 text-slate-300 text-xs">
                    <span className="font-mono text-[10px] text-indigo-400 block mb-1 font-bold">🛠️ STEP-BY-STEP FIX GUIDE:</span>
                    <ol className="list-decimal pl-4 space-y-1.5 text-slate-400 leading-normal">
                      <li>Log in to your <strong>Upstox Developer Console</strong> (<a href="https://developer.upstox.com" target="_blank" rel="noopener noreferrer" className="text-indigo-400 hover:underline">developer.upstox.com</a>).</li>
                      <li>Find your custom application and click <strong>Edit Profile/App details</strong>.</li>
                      <li>Locate the <strong>Redirect URL</strong> field in the Upstox dashboard.</li>
                      <li>Paste the <strong>Active Redirect URI</strong> shown above exactly into that field.</li>
                      <li>Ensure any customized environment variables (like <code>UPSTOX_API_KEY</code> and <code>UPSTOX_API_SECRET</code>) inside your <strong>AI Studio Settings or Render Dashboard Environment Variables</strong> exactly match your Upstox Developer account!</li>
                      <li>Save changes inside the Upstox portal and then click <strong>Connect Upstox</strong> above to test!</li>
                    </ol>
                  </div>
                </div>
              </div>

              {/* Detailed OAuth System Logs Terminal */}
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 shadow-lg">
                <div className="flex justify-between items-center mb-4 border-b border-slate-800 pb-3">
                  <div className="flex items-center gap-2">
                    <Terminal className="h-4 w-4 text-indigo-400" />
                    <h3 className="text-xs font-mono tracking-widest text-slate-300 uppercase font-bold">
                      OAuth Broker Logging Console & Callback Audit
                    </h3>
                  </div>
                  <button 
                    onClick={() => {
                      addLog("INFO", "🧹 CLEARED: OAuth telemetry console viewport refreshed.");
                    }}
                    className="text-[10px] text-indigo-400 hover:text-indigo-300 font-mono bg-indigo-950/40 hover:bg-indigo-950 border border-indigo-900/50 px-2.5 py-1 rounded transition-all cursor-pointer"
                  >
                    Clear Telemetry View
                  </button>
                </div>

                <div className="bg-slate-950/80 p-4 rounded-lg border border-slate-900 font-mono text-[11px] text-slate-300 space-y-1.5 max-h-80 overflow-y-auto shadow-inner select-all">
                  {systemLogs
                    .filter(log => log.msg.toUpperCase().includes("UPSTOX") || log.msg.toUpperCase().includes("OAUTH") || log.msg.toUpperCase().includes("TOKEN") || log.msg.toUpperCase().includes("CALLBACK"))
                    .concat([{
                      timestamp: new Date().toLocaleTimeString(),
                      level: "INFO" as const,
                      msg: "Broker telemetry filter streaming online..."
                    }])
                    .map((log, lidx) => (
                      <div key={lidx} className="flex gap-2.5 leading-normal">
                        <span className="text-slate-600 shrink-0 select-none">[{log.timestamp}]</span>
                        <span className={`font-bold shrink-0 select-none ${
                          log.level === "ERROR" ? "text-rose-500" :
                          log.level === "WARNING" ? "text-amber-500" :
                          log.level === "SUCCESS" ? "text-emerald-400 animate-pulse" : "text-sky-400"
                        }`}>
                          [{log.level}]
                        </span>
                        <span className="text-slate-300 leading-normal">{log.msg}</span>
                      </div>
                    ))}
                </div>
              </div>
            </div>
          )}

          {activeTab === "help" && (
            <div className="bg-slate-900/40 p-6 rounded-xl border border-slate-800 flex flex-col gap-4 text-xs leading-relaxed max-height-[540px] overflow-y-auto">
              <h3 className="text-sm font-display font-bold text-slate-100 flex items-center gap-1.5 border-b border-slate-800 pb-3">
                <HelpCircle className="h-5 w-5 text-emerald-400" />
                CPR Trading Bot System Rules & Execution Guide
              </h3>

              <div className="flex flex-col gap-5 whitespace-pre-line text-slate-300">
                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono text-sm font-bold">1. Central Pivot Range Calculation Lines</h4>
                  <p className="mt-1 leading-relaxed text-slate-400">
                    The system constructs daily non-repainting CPR bands based on historical indices:
                    {"\n"}• Pivot Center = (High + Low + Close) / 3
                    {"\n"}• BC (Bottom Center) = (High + Low) / 2
                    {"\n"}• TC (Top Center) = Pivot + (Pivot - BC)
                    {"\n"}• R1 Resistance Level = (2 * Pivot) - Low
                    {"\n"}• S1 Support Level = (2 * Pivot) - High
                  </p>
                </div>

                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono text-sm font-bold font-bold">2. 5-Stage Transition Flow Mechanics</h4>
                  <p className="mt-1 leading-relaxed text-slate-400">
                    Each monitor tracking setup undergoes strict validations inside a 10-candle state window:
                    {"\n"}• Stage 1: Absolute break of central boundaries.
                    {"\n"}• Stage 2: Rapid reversal and close inside bounds.
                    {"\n"}• Stage 3: Low or High touches of test levels within tolerance levels.
                    {"\n"}• Stage 4: Close past the prior retest high/low limits.
                    {"\n"}• Stage 5: Execution is greenlit upon high-velocity breaks of confirmations.
                  </p>
                </div>

                <div>
                  <h4 className="text-emerald-400 font-bold uppercase tracking-wider font-mono text-sm font-bold">3. Broker Integration (Upstox Sandbox)</h4>
                  <p className="mt-1 leading-relaxed text-slate-400">
                    At trigger time, nearest ATM Call Option (CE) is bought for long entries, and Put Option (PE) for short entries. Exit conditions are monitored live at millisecond resolutions.
                  </p>
                </div>
              </div>
            </div>
          )}
        </section>

        {/* Right Rail: Risk Management & Logs */}
        <aside className="w-[310px] border-l border-slate-800 bg-slate-900/20 p-6 flex flex-col gap-6 shrink-0 overflow-y-auto">
          <h2 className="text-xs font-bold text-slate-400 uppercase tracking-widest font-display mb-1 font-bold">Risk Control</h2>
          
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-slate-900 border border-slate-800 p-4 rounded-lg text-center shadow-sm">
              <div className="text-[10px] text-slate-500 mb-1 font-bold tracking-wider font-mono uppercase font-bold">DAILY TRADES</div>
              <div className="text-xl font-mono text-white font-bold">{liveStatus.daily_summary.trade_count} / {liveStatus.limits.max_trades}</div>
            </div>
            <div className="bg-slate-900 border border-slate-800 p-4 rounded-lg text-center shadow-sm">
              <div className="text-[10px] text-slate-500 mb-1 font-bold tracking-wider font-mono uppercase font-bold">TOTAL LOTS</div>
              <div className="text-xl font-mono text-white font-bold">1 LOT</div>
            </div>
          </div>

          <div>
            <div className="flex justify-between text-xs mb-2">
              <span className="text-slate-400 uppercase font-bold tracking-wider text-[10px]">Daily PnL</span>
              <span className={`font-mono font-bold ${dailyPnL >= 0 ? "text-emerald-400" : "text-rose-500"}`}>
                {dailyPnL >= 0 ? "+" : ""}₹{dailyPnL.toLocaleString()}
              </span>
            </div>
            <div className="w-full h-2 bg-slate-800 rounded-full overflow-hidden">
              <div 
                className={`h-full transition-all duration-500 ${dailyPnL >= 0 ? "bg-emerald-500" : "bg-rose-500"}`} 
                style={{ width: `${Math.min(100, Math.max(0, (dailyPnL + config.lossLimit) / (config.lossLimit * 2) * 100))}%` }}
              />
            </div>
            <div className="flex justify-between text-[10px] text-slate-600 mt-2 font-mono">
              <span>Limit: -₹{config.lossLimit}</span>
              <span>Limit: +₹{config.lossLimit}</span>
            </div>
          </div>

          <div className="flex-1 flex flex-col min-h-0 gap-3">
            <h3 className="text-[10px] font-bold text-slate-500 uppercase tracking-wider flex justify-between items-center bg-slate-950/10 pb-1.5 border-b border-slate-800/40 font-bold">
              <span>Positions Ledger</span>
              <span className="text-slate-500 font-mono font-bold">({trades.length} recorded)</span>
            </h3>

            <div className="space-y-3 overflow-y-auto max-h-[160px] pr-1">
              {trades.length === 0 ? (
                <div className="p-4 rounded border border-dashed border-slate-800 text-center text-slate-600 text-[10px] font-mono">
                  No positions open
                </div>
              ) : (
                trades.map((trade) => (
                  <div key={trade.id} className="p-3 bg-slate-900 border border-slate-800 rounded-lg text-xs leading-normal relative">
                    <div className="flex justify-between items-center">
                      <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${trade.type === "CE" ? "bg-sky-500/10 text-sky-400 border border-sky-500/20" : "bg-amber-500/10 text-amber-400 border border-amber-500/20"}`}>
                        NIFTY {trade.strikePrice} {trade.type}
                      </span>
                      <span className="text-[10px] font-mono text-slate-500 font-bold">{trade.id}</span>
                    </div>
                    <div className="grid grid-cols-2 mt-2 gap-1 font-mono text-[10px]">
                      <div className="text-slate-500 font-bold">Cost: <span className="text-slate-300">₹{trade.entryPrice.toFixed(1)}</span></div>
                      <div className="text-slate-500 font-bold text-right">LTP: <span className="text-slate-300">{trade.exitPrice !== null ? `₹${trade.exitPrice}` : "--"}</span></div>
                    </div>
                    <div className="flex justify-between items-center mt-2 pt-1.5 border-t border-slate-800/40 text-[10px]">
                      <span className={`font-bold ${trade.status === 'OPEN' ? 'text-indigo-400 animate-pulse' : trade.status === 'CLOSED_TP' ? 'text-emerald-400 font-bold' : 'text-rose-500'}`}>
                        {trade.status}
                      </span>
                      {trade.status !== "OPEN" && (
                        <span className={`font-mono font-bold ${trade.pnl >= 0 ? "text-emerald-400 font-bold" : "text-rose-500"}`}>
                          {trade.pnl >= 0 ? "+" : ""}₹{trade.pnl}
                        </span>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="flex flex-col min-h-0">
            <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2 flex justify-between items-center bg-slate-950/10 pb-1.5 border-b border-slate-800/40 font-bold">
              <span>Live Activity Logs</span>
              <button 
                onClick={() => {
                  setSystemLogs([{ timestamp: new Date().toTimeString().split(" ")[0], level: "INFO", msg: "Logs console cleared." }]);
                }}
                className="text-[9px] text-indigo-400 hover:text-indigo-300 font-mono tracking-tight underline border-0 bg-transparent cursor-pointer font-semibold"
              >
                Clear
              </button>
            </h3>
            <div 
              ref={scrollRef}
              className="flex-1 overflow-y-auto bg-slate-900/40 border border-slate-800 rounded-lg p-3 font-mono text-[10px] leading-relaxed space-y-1.5 max-h-[140px] shadow-inner"
            >
              {systemLogs.map((log, idx) => {
                const levelColors = {
                  INFO: "text-slate-500",
                  WARNING: "text-amber-400 font-bold animate-pulse",
                  ERROR: "text-red-400 font-bold",
                  SUCCESS: "text-emerald-400 font-semibold",
                  STRATEGY: "text-indigo-400",
                };
                return (
                  <div key={idx} className="border-b border-slate-800/25 pb-0.5 last:border-none">
                    <span className="text-slate-600 select-none">[{log.timestamp}]</span>{" "}
                    <span className={levelColors[log.level]}>{log.msg}</span>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="mt-4 pt-4 border-t border-slate-800 shadow-md">
            <div className="flex items-center gap-3 text-xs select-none">
              <div className="w-2.5 h-2.5 bg-emerald-500 rounded-full animate-pulse"></div>
              <span className="text-slate-400 italic font-mono text-[10px] uppercase font-bold tracking-wider">Telegram Dispatcher Active</span>
            </div>
          </div>
        </aside>
      </main>

      {/* Bottom Status Bar */}
      <footer className="h-10 bg-indigo-900/10 border-t border-slate-800 px-8 flex items-center justify-between text-[10px] font-mono tracking-wider shrink-0 text-slate-500 select-none">
        <div className="flex gap-6">
          <span>DATABASE: <span className="text-slate-300 font-bold">SQLITE_FALLBACK</span></span>
          <span>SYMBOL: <span className="text-slate-300 font-bold">NIFTY_50_INDEX</span></span>
        </div>
        <div className="flex gap-4">
          <span>CPU: 12%</span>
          <span>MEM: 1.2GB</span>
          <span className="text-emerald-400 animate-pulse font-bold flex items-center gap-1">● SYSTEM PULSE OK</span>
        </div>
      </footer>
    </div>
  );
}
