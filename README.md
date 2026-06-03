# 🎯 Automated CPR 4-Setup Trading System

Production-ready, highly precise intraday automated trading system built in **Python 3.12** implementing the static Central Pivot Range (CPR) strategy with **4 core setups (R1↔TC Short, S1↔BC Long, TC↔R1 Long, BC↔S1 Short)** on **NIFTY 5m candles**.

Integrated seamlessly with **Upstox API v2** for weekly ATM option trading, persistent tracking using **PostgreSQL/SQLite**, state machine execution preservation, and advanced real-time alerting via **Telegram Bot**.

---

## 🏗️ Project Architecture

```
├── app/
│   ├── __init__.py
│   └── main.py             # FastAPI entry point & Scheduler coordinator
├── config/
│   ├── __init__.py
│   └── settings.py         # Pydantic env validations and log setups
├── strategies/
│   ├── __init__.py
│   └── cpr_strategy.py     # Static CPR math & State Machine transitions
├── brokers/
│   ├── __init__.py
│   └── upstox_client.py    # Upstox API Auth, instruments ATM selection & order execution
├── risk/
│   ├── __init__.py
│   └── manager.py          # Trade entry/exit registers, daily limits and blocks checks
├── database/
│   ├── __init__.py
│   ├── db.py               # Session handlers & schema bootstrap (SQLite auto-fallback)
│   └── models.py           # SQLAlchemy tables for Trades, Daily metrics, States
├── telegram/
│   ├── __init__.py
│   └── bot.py              # Telegram message formatter & API clients
├── tests/
│   ├── __init__.py
│   └── test_strategy.py    # Unit tests covering state machine sequences
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🛠️ Micro-Service Setup Instructions

### Option 1: Direct Local Deployment
1. **Clone and Navigate**:
   ```bash
   cd cpr-trading-system
   ```

2. **Create and Activate Virtual Env**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Setup**:
   Copy `.env.example` to `.env` and fill in credentials:
   ```bash
   cp .env.example .env
   ```

5. **Run FastAPI Service**:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 3000
   ```

6. **Verify Server Status**:
   Visit `http://localhost:3000/api/status`

### Option 2: Docker Container Deployment
1. Build container:
   ```bash
   docker build -t cpr-trading-bot .
   ```
2. Spawn microservice:
   ```bash
   docker run -d --name cpr-bot-instance -p 3000:3000 --env-file .env cpr-trading-bot
   ```

> Note: If you deploy on a free-tier host such as Render free plan, the container may still be put to sleep after a period of inactivity. That means the dashboard tab can close without affecting the running service in theory, but the host may still stop the app after idle time. Use an always-on instance or paid plan for guaranteed continuous operation.
>
> This repository now supports an optional keepalive ping target. Set `KEEPALIVE_URL` in `.env` or Render environment variables to a public health check URL (for example, `https://uptimerobot.com/` or your own deployed ping endpoint). The app will automatically ping that URL every `KEEPALIVE_INTERVAL_SECONDS` seconds when configured.

---

## 📜 Strategy Rules Summary

| Setup Name | Core Band Range | Trade Direction | Entry Criteria Guards | Option Strike | Stop Loss (Index Level) | Take Profit (Index Level) |
|---|---|---|---|---|---|---|
| **Setup A** | R1 → TC | **Short (BUY PE)** | Close above R1 ➜ Close below R1 ➜ Retest R1 ➜ Break Conf Low, Entry Close > TC | ATM Weekly PE | Retest High + 3 | TC + 3 |
| **Setup B** | S1 → BC | **Long (BUY CE)** | Close below S1 ➜ Close above S1 ➜ Retest S1 ➜ Break Conf High, Entry Close < BC | ATM Weekly CE | Retest Low - 3 | BC - 3 |
| **Setup C** | TC → R1 | **Long (BUY CE)** | Close above TC ➜ Close below TC ➜ Retest TC ➜ Break Conf High, Entry Close > TC | ATM Weekly CE | Retest Low - 3 | R1 - 3 |
| **Setup D** | BC → S1 | **Short (BUY PE)** | Close below BC ➜ Close above BC ➜ Retest BC ➜ Break Conf Low, Entry Close < BC | ATM Weekly PE | Retest High + 3 | S1 + 3 |

---

## 🛡️ Risk Safeguards & Limits
*   **One Active Position Limit**: No simultaneous trades can be held.
*   **Maximum Trades/Day**: Absolute limit of **2 closed entries** daily.
*   **Max Daily Loss**: Auto-failsafe blocks trading for the remainder of the session once cumulative losses exceed **₹2000**.
*   **Size**: Static **1 Lot Only** constraint.
*   **State Retention**: Persistent states are recorded to DB to avoid signal loss on connection drops.

---

## 🔗 Upstox API Verification Quick-Start
To establish link with live broker accounts:
1. Trigger Upstox OAuth login: `GET http://localhost:3000/api/v1/login-url` which provides a login redirection.
2. Complete authorization, redirecting code to callback: `/api/v1/callback?code=AUTH_CODE_HERE`.
3. System fetches access token automatically.

---

## 🧑‍💻 Manual Unit Test suite execution
Run tests to verify State Machine setups transitions validity:
```bash
pytest -v tests/
```
