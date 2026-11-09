<p align="center">
  <img src="assets/logo.svg" width="160" alt="Victor logo" />
</p>

<h1 align="center">Victor — Day-Trading Agent (HK / US Equities)</h1>

<p align="center">
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT" /></a>
  <img src="https://img.shields.io/badge/Type-AI%20Agent-FF1493.svg" alt="Type: AI Agent" />
  <img src="https://img.shields.io/badge/Markets-HK%20%2F%20US-16C784.svg" alt="Markets: HK / US" />
  <img src="https://img.shields.io/badge/Mode-Signal-FF8C00.svg" alt="Mode: Signal" />
  <img src="https://img.shields.io/badge/Mode-Auto-2ECC71.svg" alt="Mode: Auto" />
</p>

<p align="center">🌐 <a href="README_cn.md">简体中文</a></p>

**Victor** is a personified AI day-trading execution agent for Hong Kong and US equities. It watches the market, analyzes tickers, computes position sizing and stop levels, and emits structured trading signals — while a human executes every order in the broker app.

> This is **not** a traditional software project. There is no application to `npm install` or `cargo run`. The repository *is* the agent: its entire behavior is shaped by the `trade` skill under `.pi/skills/` and the project rules in `AGENTS.md`, which the **pi** agent harness loads as Victor's operating discipline.

---

## Who is Victor?

The agent is personified as **Victor** — a disciplined intraday trading execution assistant for the Hong Kong and US markets. Victor is less a program and more a **rule-shaped persona**: everything about how it thinks and acts — when to stay flat, how tightly to trail a stop, why it never places an order itself — is encoded in the skills and rules in this repo, not in application code.

The name **Victor** ("conqueror, winner") reflects the project's aspiration toward profitable trading. But Victor's edge does **not** come from reckless aggression. It comes from factual rigor and iron discipline:

- **Verify before stating.** No factual or numeric claim leaves Victor's mouth unchecked.
- **EV ≥ 0.** Every entry is gated on positive expected value (win-rate × payoff).
- **Signal mode.** Victor analyzes, watches, and signals — it never touches an order. Execution belongs to the human.

The operating principle, in one line: **losing money on a trade is acceptable risk; stating an unverified fact or breaking a guardrail is a bug — and bugs are unacceptable.**

---

## Core Philosophy

| Principle | What it means |
|---|---|
| **Facts first** | Verify entity identity, arithmetic, trading calendar, API fields, and fees before asserting. Never pass a guess off as a fact. |
| **Signal mode** | Since 2026-07-07, Victor only emits signals (🟢 open / 🔵 add / 🟠 reduce / 🔴 close / 🟡 trailing stop). It never calls any order-placement command. |
| **EV-driven** | Every entry requires win-rate > 50% **and** payoff ratio ≥ 1.2; otherwise no signal. |
| **Signal-level review** | Win-rate / payoff / EV tracked at the signal level (account-independent); actual account P&L is the user's to compute. |
| **Knowledge sedimentation** | Hard rules distill into `AGENTS.md` and the `trade` skill's references. AutoMemory was deprecated 2026-07-20 (its content folded into SKILL.md). |

---

## How Victor Works

Victor is activated by pi whenever the user asks to **watch the market, emit a signal, run a post-trade review, or analyze a ticker**. On activation it loads the `trade` skill and runs its guardrails.

**Standard watch sequence** (scripts under `.pi/skills/trade/scripts/`):

1. `preflight.py` — verify time, market session, Futu OpenD port
2. `hot_list.py` — pull the heat board (the mandatory first step for ticker selection)
3. `static` + `classify_hk_security.py` — confirm the ticker's true identity and security type
4. `snapshot.py` / `kline.py` — snapshot + trend / Fibonacci retracement
5. `monitor.py` — dense sampling (default 6 rounds × 10s) during the session

When a setup meets the EV bar, Victor emits a signal as a table in the chat (emoji-marked), with the stop price computed, then appends it to the day's signal log (HK / US split) for later review. The account is the user's choice — Victor does not manage accounts or verify buying power; the human executes in their own broker app.

---

## Repository Structure

```
trading-agent/
├── AGENTS.md                      # Project rules — the agent's operating discipline (entry point)
├── LICENSE.md                     # MIT
├── README.md                      # This file (English)
├── README_cn.md                   # Chinese README
│
├── .pi/                           # pi harness configuration
│   ├── settings.json              # Provider / model defaults
│   ├── extensions/                # Project-level pi extensions
│   │   ├── trade-guards.ts        # Bridges pi events to the Python guardrails in hooks/
│   │   └── ask-question.ts        # AskUserQuestion tool (structured confirmations)
│   └── skills/
│       └── trade/                 # Domain execution spec — the heart of Victor
│           ├── SKILL.md           # Master file: execution spec + hard guardrails
│           ├── references/        # Strategy, risk, monitoring, review, host integration
│           ├── config.example.json       # Risk / monitoring config template
│           ├── accounts.example.json     # Tiger credential template
│           └── scripts/           # Watch-market + order + review script library
│
├── hooks/                         # Guardrail scripts (single source, spawned by the pi bridge)
│   ├── secret_guard.py            # Credential-read blocking
│   ├── monitor_guard.py           # Sampling discipline / mutex / disconnect reminders
│   ├── changelog_guard.py         # CHANGELOG / TODO hygiene reminders
│   ├── jms_guard.py               # Blocks subscription refresh during trading hours
│   └── live_auth_witness.py       # Live-account authorization witness
│
├── signals/                       # Per-day signal logs (HK/US split, HKT/ET suffix) + ring-log.csv (+ local-only equity ledger, gitignored)
├── reviews/                       # Post-trade review reports + per-review CSV/PNG attachments
├── notes/                         # Long-form derivations (Kelly sizing plan, cumulative-return math)
├── state/                         # Persistent runtime state (gitignored): circuit breaker, pool claims, mutex WAL
├── tmp/                           # Throwaway runtime files (gitignored): sampling logs, heartbeats, bg task logs
└── archive/                       # Local-only history (gitignored): pre-refactor memory snapshots + old reviews
```

> The real `config.json` and `accounts.json` (containing Tiger credentials) are **gitignored** — only the `*.example.json` templates ship in the repo. The `state/`, `tmp/` and `archive/` directories are likewise local-only.

---

## Toolchain

Victor trades through and reads from three broker data sources:

| Source | Role | Notes |
|---|---|---|
| **Futu OpenD** (`futu-api`) | Primary market data (HK + US) | Free HK Level-2 (10-depth book + broker queue + capital flow) + US 10-depth + US capital flow; local gateway `127.0.0.1:11111` |
| **Tiger Brokers SDK** (`tigeropen`) | HK backup data + WebSocket push | HK Lv1/Lv2 verified; **no US quote permission** |

Victor only emits signals — it never places orders. Which broker / account to use is **the user's choice**; the human executes in their own broker app. Market data comes from Futu + Tiger above.

---

## Hard Guardrails (excerpts)

Victor self-checks these before emitting any signal (full list in `SKILL.md`):

- **One ticker at a time** — never open a new position while another is held.
- **Position sizing from stop (fixed-fraction)** — per-trade budget `B = risk_fraction × equity` (default 2%); size is the lot-rounded position whose actual max_loss lands closest to B. max_loss may slightly exceed B but must stay under `equity × f_max` (default 2.5%); the absolute cap *is* a fraction of equity.
- **Stop price mandatory in every open signal** — set as a technical level; the human places it in the app.
- **No derivatives** — stocks, ETFs (incl. 2×/3× leveraged), and REITs only; no options/warrants/CBBCs/futures.
- **Session-bound, flat by close** — HK: regular session only (09:30-12:00 / 13:00-16:00), positions flattened before the 12:00 lunch break and the 16:00 close. US: tradable window = premarket + regular session (04:00-16:00 ET), watched until the user stops or the close — no after-hours/overnight signals, and no positions carried past the close.
- **Short allowed by default** — assume shortable unless told otherwise.
- **Flat by end of day** — never carry a position overnight.

---

## Prerequisites

To actually run Victor, you need — outside this repo:

- The **pi** agent harness — the runtime that loads this repo's `.pi/` skills, extensions and `AGENTS.md` rules
- **Tiger** SDK (`tigeropen`) configured at `~/.tigeropen/`
- **Futu OpenD** local gateway running (HK Level-2 + US depth)
- A local `config.json` and `accounts.json` filled in from the `*.example.json` templates (accounts.json only needs the Tiger section)
- **Windows users** (scripts and guardrails are dual-platform since 2026-08-09): install Git for Windows (check "Add to PATH" during setup) and add Python 3 to PATH; where docs show `python3`, run `python` instead

Without these, the repo still reads as a complete spec of *how* a disciplined trading agent should behave.

---

## Current Stage

Victor is currently in **signal mode**: AI signals, human executes — the arrangement that (since 2026-07-07) rooted out the order-failure / reverse-position / stop-failure problems of direct AI ordering. Graduation to direct ordering requires sustained signal win-rate, payoff, and EV plus continued improvement, and explicit user authorization.

---

## Risk Disclaimer

Day trading involves substantial risk of loss. Victor emits analysis and signals for the account holder's decision — **it is not financial advice, and it does not execute trades.** All orders are placed manually by the user in their own brokerage account. Past performance does not guarantee future results, and any backtest or simulated result does not represent live trading performance. The user assumes full responsibility for any trading decision and any resulting loss. The authors and contributors assume no liability for trading losses.

---

## Attribution

This project is released under the MIT License, and you are additionally asked to **credit the author and cite the source** whenever you use, redistribute, or build upon it:

- **Author:** xhqing and contributors
- **Project:** Victor — Day-Trading Agent (HK / US Equities)
- **Source:** https://github.com/xhqing/trading-agent

If you fork, reference, or derive from this repository, please retain this attribution — the author name, the project name, and the repository URL — in your documentation, README, or acknowledgements.

---

## License

[MIT](LICENSE.md) © 2026 xhqing and contributors.
