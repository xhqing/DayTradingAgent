<p align="center">
  <img src="assets/logo.svg" width="160" alt="Victor logo" />
</p>

<h1 align="center">Victor — Day-Trading Agent (HK / US Equities)</h1>

<p align="center">
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
  <a href="https://claude.com/claude-code"><img src="https://img.shields.io/badge/built%20with-Claude%20Code-7C3AED.svg" alt="Built with Claude Code" /></a>
  <img src="https://img.shields.io/badge/markets-HK%20%2F%20US-16C784.svg" alt="Markets: HK / US" />
  <img src="https://img.shields.io/badge/mode-signal-FF8C00.svg" alt="Mode: Signal" />
</p>

<p align="center">🌐 <a href="README_cn.md">中文</a></p>

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

**Under the hood.** The LLM driving Victor is **GLM-5.2**, presented to the outside world disguised as **Opus-4.8**. The model label is camouflage; what truly makes Victor *Victor* is the discipline encoded in this repo — its skills, rules, and guardrails — not the name on the underlying model.

---

## Core Philosophy

| Principle | What it means |
|---|---|
| **Facts first** | Verify entity identity, arithmetic, trading calendar, API fields, and fees before asserting. Never pass a guess off as a fact. |
| **Signal mode** | Since 2026-07-07, Victor only emits signals (🟢 open / 🔴 close / 🟡 trailing stop / 🟠 trailing take-profit). It never calls any order-placement command. |
| **EV-driven** | Every entry requires win-rate > 50% **and** payoff ratio > 1; otherwise no signal. |
| **Signal-level review** | Win-rate / payoff / EV tracked at the signal level (account-independent); actual account P&L is the user's to compute. |
| **Knowledge sedimentation** | AutoMemory persists in project-level `.claude/memory/`; hard rules distill into `rules/` and `skills/`. The sediment self-check hook was removed 2026-07-15. |

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
│   ├── settings.local.json        # Local-only: permissions + autoMemoryDirectory (gitignored)
│   ├── settings.local.example.json # Template for settings.local.json (tracked)
│   ├── memory/                    # AutoMemory store (project-level, tracked — not gitignored)
│   │
│   ├── rules/                     # General working discipline (cross-domain)
│   │   ├── verify-facts-before-stating.md
│   │   ├── output-and-writing-style.md
│   │   └── knowledge-sedimentation.md
│   │
│   └── skills/
│       └── trade/                 # Domain execution spec — the heart of Victor
│           ├── SKILL.md           # Master file: execution spec + hard guardrails
│           ├── classify_hk_security.py   # HK security-type classifier (stock/ETF/REIT/derivative)
│           ├── config.example.json       # Risk / monitoring config template
│           ├── accounts.example.json     # Tiger credential template
│           ├── accounts.md               # Tiger SDK config + HK code format
│           ├── tiger-websocket.md        # Tiger SDK WebSocket skeleton
│           ├── hk-level2-sources.md      # HK Level-2 data-source survey
│           ├── futu-opend-level2.md      # Futu OpenD Level-2 skeleton
│           ├── quant/                    # Quant data layer (schema, sources, README)
│           └── scripts/                  # Watch-market script library
│               ├── preflight.py
│               ├── hot_list.py
│               ├── snapshot.py
│               ├── kline.py
│               ├── monitor.py
│               └── alert.sh              # Sound alert on signal output
│
├── signals/                      # Per-day signal logs at project root (HK / US split, HKT/ET suffix) + ring-log.csv
└── archive/                       # Local-only history (gitignored): past memory snapshots (pre-refactor)
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
- **Position sizing from stop** — size = `max_loss_per_trade` ÷ per-share max loss, rounded to lot size; loss is capped by config, not by % of equity.
- **Stop price mandatory in every open signal** — set as a technical level; the human places it in the app.
- **No derivatives** — stocks, ETFs (incl. 2×/3× leveraged), and REITs only; no options/warrants/CBBCs/futures.
- **HK intraday / US 24h** — HK: regular session only (09:30-12:00 / 13:00-16:00), positions flattened before the 12:00 lunch break and the 15:45 close. US (since 2026-07-15): signals allowed around the clock — pre / regular / after / overnight.
- **Short allowed by default** — assume shortable unless told otherwise.
- **Flat by end of day** — never carry a position overnight.

---

## Prerequisites

To actually run Victor, you need — outside this repo:

- [Claude Code](https://claude.com/claude-code)
- **Tiger** SDK (`tigeropen`) configured at `~/.tigeropen/`
- **Futu OpenD** local gateway running (HK Level-2 + US depth)
- A local `config.json` and `accounts.json` filled in from the `*.example.json` templates (accounts.json only needs the Tiger section); optionally copy `.claude/settings.local.example.json` → `.claude/settings.local.json` and set `autoMemoryDirectory` to your machine's absolute path to store AutoMemory inside the project (it defaults to Claude Code's global path otherwise)

Without these, the repo still reads as a complete spec of *how* a disciplined trading agent should behave.

---

## Current Stage

Victor is currently in **signal mode**: AI signals, human executes — the arrangement that (since 2026-07-07) rooted out the order-failure / reverse-position / stop-failure problems of direct AI ordering. Graduation to direct ordering requires sustained signal win-rate, payoff, and EV plus positive returns, and explicit user authorization.

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
