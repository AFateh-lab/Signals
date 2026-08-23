# OCC Toolkit Power v4

Power v4 keeps the full Power v3 scanner and adds a browser-local **Command Center** inspired by professional trading operations dashboards, while remaining research/paper only.

## Added in v4

- Command Center dashboard
- Paper balance, realized/unrealized paper P&L and equity curve
- Latest Gold signal board
- Paper positions / lifecycle view
- Decision trace showing factor-by-factor score contributions
- Event log of PAPER / WATCH / SKIP decisions
- Public Binance feed-health / latency checks
- Persistent local paper settings and migration from the v3 paper journal
- Gold scan snapshots saved to local browser storage for the dashboard

## Added after v4 — live layer (Command Center)

- **Live tape** — every trade on the top 12 coins streamed over WebSocket; big prints
  (threshold selectable $25k–$1M), running taker buy/sell totals and delta, live
  liquidation feed, and open paper positions marked to the live tape with at-TP/at-stop flags.
- **Whale radar** — the books of the top 15/30/50 coins rotated continuously (3 books
  every 3s); every wall above the chosen size tracked market-wide with age, refills and
  disappearances classified: NEW / PULLED (vanished away from price — spoof-like) /
  CONSUMED (price actually reached it) / REFILL (size restored — iceberg-like). A
  "most walled right now" board ranks coins by resting whale money.
- **On-chain** — large raw transfers watched from the browser: BTC mempool via
  blockchain.info (before confirmation) and ETH from each new block via a public RPC,
  with explorer links. No identity attribution — who owns an address is not public data.

- **Alerts** — a hub that keeps working while the page is open: chart patterns re-scanned
  on the top coins across 15m/1h/4h every 3 minutes (confirmed-only by default), and
  Binance's announcement feed polled every 60s for listings/delistings/notices. New
  findings land in a persistent feed and, if enabled, fire browser notifications.
  The wider crypto press blocks browser requests, so news = the exchange's own feed
  plus what the tape itself shows.

Network note: on some networks Binance futures WebSockets connect but never deliver
frames while spot streams flow normally. The tape therefore runs on spot streams; the
liquidation feed (futures-only) carries a watchdog that says so if it is silent.

## Important

This file does **not** connect to an exchange account or place trades. Dashboard P&L is a paper research calculation using the scanner's 2R target / 1R stop convention and the user-selected dollar risk per signal. Feed health checks public endpoints only.

Open `OCC_Toolkit_Power_v4.html` in a modern browser. Some browsers may restrict cross-origin requests when opening local files; serving it from a simple local or static web server is more reliable.
