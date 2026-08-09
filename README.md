<p align="center">
  <img src="assets/logo.svg" width="160" alt="Victor logo" />
</p>

<h1 align="center">Victor — Day-Trading Agent (HK / US Equities)</h1>

<p align="center">
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
  <img src="https://img.shields.io/badge/markets-HK%20%2F%20US-16C784.svg" alt="Markets: HK / US" />
  <img src="https://img.shields.io/badge/mode-signal-FF8C00.svg" alt="Mode: Signal" />
</p>

<p align="center">🌐 <a href="README_cn.md">简体中文</a></p>

**Victor** is a personified AI day-trading execution agent for Hong Kong and US equities, built on [Claude Code](https://claude.com/claude-code). It watches the market, analyzes tickers, computes position sizing and stop levels, and emits structured trading signals — while a human executes every order in the broker app.

> This is **not** a traditional software project. There is no application to `npm install` or `cargo run`. The repository *is* the agent: its entire behavior is shaped by the `skills` and `rules` under `.claude/`, which Claude Code loads as Victor's operating discipline.

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
| **Knowledge sedimentation** | Hard rules distill into `rules/` and `skills/`. AutoMemory was deprecated 2026-07-20 (its content folded into SKILL.md; the `.claude/memory/` directory and `autoMemoryDirectory` config were removed). |

---

## How Victor Works

Victor is activated by Claude Code whenever the user asks to **watch the market, emit a signal, run a post-trade review, or analyze a ticker**. On activation it loads the `trade` skill and runs its guardrails.

**Standard watch sequence** (scripts under `.claude/skills/trade/scripts/`):

1. `preflight.py` — verify time, market session, Futu OpenD port
2. `hot_list.py` — pull the heat board (the mandatory first step for ticker selection)
3. `static` + `classify_hk_security.py` — confirm the ticker's true identity and security type
4. `snapshot.py` / `kline.py` — snapshot + trend / Fibonacci retracement
5. `monitor.py` — dense sampling (default 6 rounds × 10s) during the session

When a setup meets the EV bar, Victor emits a signal as a table in the chat (emoji-marked), with the stop price computed, then appends it to the day's signal log (HK / US split) for later review. The account is the user's choice — Victor does not manage accounts or verify buying power; the human executes in their own broker app.

---

## Repository Structure

```
DayTradingAgent/
├── CLAUDE.md                      # Entry point — points Claude at the rules & trade skill
├── LICENSE.md                     # MIT
├── README.md                      # This file (English)
├── README_cn.md                   # Chinese README
│
├── .claude/
│   ├── settings.local.json        # Local-only: permissions (gitignored)
│   ├── settings.local.example.json # Template for settings.local.json (tracked)
│   │
│   ├── rules/                     # General working discipline (cross-domain)
│   │   ├── verify-facts-before-stating.md
│   │   └── output-and-writing-style.md
│   │
│   └── skills/
│       └── trade/                 # Domain execution spec — the heart of Victor
│           ├── SKILL.md           # Master file: execution spec + hard guardrails
│           ├── classify_hk_security.py   # HK security-type classifier (stock/ETF/REIT/derivative)
│           ├── config.example.json       # Risk / monitoring config template
│           ├── accounts.example.json     # Tiger credential template
│           ├── accounts.md               # Data-source config + HK code format
│           ├── tiger-websocket.md        # Tiger SDK WebSocket skeleton
│           ├── hk-level2-sources.md      # HK Level-2 data-source survey
│           ├── futu-opend-level2.md      # Futu OpenD Level-2 skeleton
│           └── scripts/                  # Watch-market script library
│               ├── preflight.py          # Pre-flight: time/session/OpenD + risk config + anti-sleep
│               ├── hot_list.py           # Heat board (mandatory first step for ticker selection)
│               ├── snapshot.py           # Market snapshot
│               ├── kline.py              # K-line + Fibonacci retracement
│               ├── monitor.py            # Dense sampling (single ticker)
│               ├── monitor_segment.py    # Background segmented sampling (multi-ticker — the main watch loop)
│               ├── monitor_summary.py    # Full-day summary + market-regime classification
│               ├── capital.py            # Capital flow (Futu)
│               ├── review.py             # Post-trade review stats (R-multiple, Bayesian NIG)
│               ├── bayes_evolution.py    # Sequential Bayesian evolution charts
│               └── alert.sh              # Write signal file + sound alert
│
├── signals/                      # Per-day signal logs (HK/US split, HKT/ET suffix) + ring-log.csv + equity-log.csv
├── reviews/                      # Post-trade review reports + per-review CSV/PNG attachments
├── notes/                        # Long-form derivations (Kelly sizing plan, cumulative-return math)
└── archive/                       # Local-only history (gitignored): pre-refactor memory snapshots + old reviews
```

> The real `config.json` and `accounts.json` (containing Tiger credentials) are **gitignored** — only the `*.example.json` templates ship in the repo. The `archive/` directory is likewise local-only.

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
- **Session-bound, flat by close** — HK: regular session only (09:30-12:00 / 13:00-16:00), positions flattened before the 12:00 lunch break and the 16:00 close. US: regular session only (09:30-16:00 ET), watched until the user stops or the close — no pre/after/overnight signals, and no positions carried past the close.
- **Short allowed by default** — assume shortable unless told otherwise.
- **Flat by end of day** — never carry a position overnight.

---

## Prerequisites

To actually run Victor, you need — outside this repo:

- [Claude Code](https://claude.com/claude-code)
- **Tiger** SDK (`tigeropen`) configured at `~/.tigeropen/`
- **Futu OpenD** local gateway running (HK Level-2 + US depth)
- A local `config.json` and `accounts.json` filled in from the `*.example.json` templates (accounts.json only needs the Tiger section); optionally copy `.claude/settings.local.example.json` → `.claude/settings.local.json` to add extra command pre-approvals
- **Windows users** (scripts and guardrails are dual-platform since 2026-08-09): install Git for Windows (check "Add to PATH" during setup) and add Python 3 to PATH; where docs show `python3`, run `python` instead (both are pre-approved in `settings.json`)

Without these, the repo still reads as a complete spec of *how* a disciplined trading agent should behave.

---

## Current Stage

Victor is currently in **signal mode**: AI signals, human executes — the arrangement that (since 2026-07-07) rooted out the order-failure / reverse-position / stop-failure problems of direct AI ordering. Graduation to direct ordering requires sustained signal win-rate, payoff, and EV plus continued improvement, and explicit user authorization.

---

## Risk Disclaimer

Day trading involves substantial risk of loss. Victor emits analysis and signals for the account holder's decision — **it is not financial advice, and it does not execute trades.** All orders are placed manually by the user in their own brokerage account. Past performance does not guarantee future results. The authors and contributors assume no liability for trading losses.

---

## Attribution

This project is released under the MIT License, and you are additionally asked to **credit the author and cite the source** whenever you use, redistribute, or build upon it:

- **Author:** All Contributors
- **Project:** Victor — Day-Trading Agent (HK / US Equities)
- **Source:** https://github.com/xhqing/DayTradingAgent

If you fork, reference, or derive from this repository, please retain this attribution — the author name, the project name, and the repository URL — in your documentation, README, or acknowledgements.

---

## License

[MIT](LICENSE.md) © 2026 All Contributors.
