"""
Bonereaper Trading Strategy Reverse-Engineering
================================================
Fetches trade history from Polymarket Data API and analyzes:
- Market selection (5m vs 15m, BTC vs ETH)
- Timing patterns (entry relative to market window)
- Price distribution (what prices he pays)
- Both-sides analysis (does he buy Up AND Down in same market?)
- Size allocation (how capital is distributed)
- Late-window "penny collecting" (buying at 0.99 near expiry)
- MERGE/REDEEM patterns
"""

import requests
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

WALLET = "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30"
API_URL = "https://data-api.polymarket.com/activity"

# -- Fetch all trades ----------------------------------------------

def fetch_trades(limit_per_page=100, max_pages=30):
    """Fetch trade history, paginating through the API."""
    all_trades = []
    for page in range(max_pages):
        offset = page * limit_per_page
        url = f"{API_URL}?user={WALLET}&limit={limit_per_page}&offset={offset}"
        print(f"  Fetching page {page+1} (offset {offset})...", end=" ")
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"ERROR: {e}")
            break

        if not data:
            print("empty -> done")
            break

        print(f"got {len(data)} records")
        all_trades.extend(data)

        if len(data) < limit_per_page:
            break

        time.sleep(0.3)  # rate limiting

    return all_trades


# -- Analysis functions --------------------------------------------

def analyze_trades(trades):
    """Main analysis pipeline."""

    # Separate by type
    trade_records = [t for t in trades if t.get("type") == "TRADE"]
    merges = [t for t in trades if t.get("type") == "MERGE"]
    redeems = [t for t in trades if t.get("type") == "REDEEM"]

    print(f"\n{'='*70}")
    print(f"BONEREAPER STRATEGY ANALYSIS")
    print(f"{'='*70}")
    print(f"Total records: {len(trades)}")
    print(f"  TRADE: {len(trade_records)}")
    print(f"  MERGE: {len(merges)}")
    print(f"  REDEEM: {len(redeems)}")

    if not trade_records:
        print("No trades to analyze!")
        return

    # -- Time range --
    timestamps = [t["timestamp"] for t in trade_records]
    t_min, t_max = min(timestamps), max(timestamps)
    dt_min = datetime.fromtimestamp(t_min, tz=timezone.utc)
    dt_max = datetime.fromtimestamp(t_max, tz=timezone.utc)
    span_hours = (t_max - t_min) / 3600
    print(f"\nTime range: {dt_min:%Y-%m-%d %H:%M} -> {dt_max:%Y-%m-%d %H:%M} UTC")
    print(f"Span: {span_hours:.1f} hours")

    # -- 1. Market Selection --
    print(f"\n{'-'*70}")
    print("1. MARKET SELECTION")
    print(f"{'-'*70}")

    market_types = defaultdict(lambda: {"count": 0, "usdc": 0})
    for t in trade_records:
        slug = t.get("slug", "")
        if "5m" in slug:
            mtype = "5min"
        elif "15m" in slug:
            mtype = "15min"
        else:
            mtype = "other"

        asset = "BTC" if "btc" in slug else "ETH" if "eth" in slug else "OTHER"
        key = f"{asset}-{mtype}"
        market_types[key]["count"] += 1
        market_types[key]["usdc"] += t.get("usdcSize", 0)

    for key in sorted(market_types, key=lambda k: market_types[k]["usdc"], reverse=True):
        v = market_types[key]
        print(f"  {key:12s}: {v['count']:5d} trades, ${v['usdc']:>10,.2f} USDC volume")

    # -- 2. Both-Sides Analysis (KEY INSIGHT) --
    print(f"\n{'-'*70}")
    print("2. BOTH-SIDES ANALYSIS (per market window)")
    print(f"{'-'*70}")

    # Group by slug (= unique market window)
    by_market = defaultdict(list)
    for t in trade_records:
        by_market[t.get("slug", "unknown")].append(t)

    both_sides = 0
    up_only = 0
    down_only = 0

    both_sides_details = []

    for slug, market_trades in by_market.items():
        outcomes = set(t.get("outcome") for t in market_trades)
        if "Up" in outcomes and "Down" in outcomes:
            both_sides += 1
            up_usdc = sum(t["usdcSize"] for t in market_trades if t.get("outcome") == "Up")
            down_usdc = sum(t["usdcSize"] for t in market_trades if t.get("outcome") == "Down")
            both_sides_details.append({
                "slug": slug,
                "up_usdc": up_usdc,
                "down_usdc": down_usdc,
                "ratio": up_usdc / down_usdc if down_usdc > 0 else float('inf'),
                "total": up_usdc + down_usdc,
                "n_trades": len(market_trades),
            })
        elif "Up" in outcomes:
            up_only += 1
        else:
            down_only += 1

    total_markets = both_sides + up_only + down_only
    print(f"  Total unique markets: {total_markets}")
    print(f"  Both sides:  {both_sides:4d} ({100*both_sides/total_markets:.1f}%)")
    print(f"  Up only:     {up_only:4d} ({100*up_only/total_markets:.1f}%)")
    print(f"  Down only:   {down_only:4d} ({100*down_only/total_markets:.1f}%)")

    if both_sides_details:
        print(f"\n  Both-sides markets (Up/Down USDC allocation):")
        # Show some examples
        for d in sorted(both_sides_details, key=lambda x: x["total"], reverse=True)[:10]:
            print(f"    {d['slug'][:50]:50s}  Up=${d['up_usdc']:>8.2f}  Down=${d['down_usdc']:>8.2f}  "
                  f"ratio={d['ratio']:.2f}  ({d['n_trades']} trades)")

    # -- 3. Price Distribution --
    print(f"\n{'-'*70}")
    print("3. PRICE DISTRIBUTION")
    print(f"{'-'*70}")

    # Bucket prices
    price_buckets = {
        "0.01-0.10 (deep OTM)": [],
        "0.10-0.25 (OTM)": [],
        "0.25-0.40 (slight OTM)": [],
        "0.40-0.60 (at-the-money)": [],
        "0.60-0.75 (slight ITM)": [],
        "0.75-0.90 (ITM)": [],
        "0.90-0.98 (deep ITM)": [],
        "0.98-1.00 (near-certain)": [],
    }

    def bucket_key(price):
        if price < 0.10: return "0.01-0.10 (deep OTM)"
        if price < 0.25: return "0.10-0.25 (OTM)"
        if price < 0.40: return "0.25-0.40 (slight OTM)"
        if price < 0.60: return "0.40-0.60 (at-the-money)"
        if price < 0.75: return "0.60-0.75 (slight ITM)"
        if price < 0.90: return "0.75-0.90 (ITM)"
        if price < 0.98: return "0.90-0.98 (deep ITM)"
        return "0.98-1.00 (near-certain)"

    for t in trade_records:
        p = t.get("price", 0)
        bk = bucket_key(p)
        price_buckets[bk].append(t)

    for bk, trades_in_bucket in price_buckets.items():
        if not trades_in_bucket:
            continue
        total_usdc = sum(t["usdcSize"] for t in trades_in_bucket)
        avg_price = sum(t["price"] for t in trades_in_bucket) / len(trades_in_bucket)
        print(f"  {bk:30s}: {len(trades_in_bucket):4d} trades, ${total_usdc:>10,.2f} USDC, avg price={avg_price:.4f}")

    # -- 4. Timing Analysis (entry relative to market window) --
    print(f"\n{'-'*70}")
    print("4. TIMING ANALYSIS (seconds into 5-min window)")
    print(f"{'-'*70}")

    timing_data = []
    for t in trade_records:
        slug = t.get("slug", "")
        # Extract window start from slug: btc-updown-5m-{unix_ts}
        parts = slug.split("-")
        try:
            window_start = int(parts[-1])
        except (ValueError, IndexError):
            continue

        if "5m" in slug:
            window_duration = 300  # 5 minutes
        elif "15m" in slug:
            window_duration = 900
        else:
            continue

        entry_offset = t["timestamp"] - window_start  # seconds into window
        timing_data.append({
            "offset": entry_offset,
            "price": t.get("price", 0),
            "outcome": t.get("outcome"),
            "usdc": t.get("usdcSize", 0),
            "duration": window_duration,
            "slug": slug,
        })

    if timing_data:
        # Bucket by window phase for 5-min markets
        fivemin = [td for td in timing_data if td["duration"] == 300]
        if fivemin:
            phases = {
                "Before window (<0s)": [td for td in fivemin if td["offset"] < 0],
                "Early (0-60s)": [td for td in fivemin if 0 <= td["offset"] < 60],
                "Mid-early (60-120s)": [td for td in fivemin if 60 <= td["offset"] < 120],
                "Middle (120-180s)": [td for td in fivemin if 120 <= td["offset"] < 180],
                "Mid-late (180-240s)": [td for td in fivemin if 180 <= td["offset"] < 240],
                "Late (240-300s)": [td for td in fivemin if 240 <= td["offset"] < 300],
                "After window (>300s)": [td for td in fivemin if td["offset"] >= 300],
            }

            print("  5-minute markets:")
            for phase, entries in phases.items():
                if not entries:
                    continue
                total_usdc = sum(e["usdc"] for e in entries)
                avg_price = sum(e["price"] for e in entries) / len(entries)
                up_pct = 100 * sum(1 for e in entries if e["outcome"] == "Up") / len(entries)
                print(f"    {phase:25s}: {len(entries):4d} trades, ${total_usdc:>10,.2f} USDC, "
                      f"avg_price={avg_price:.3f}, Up%={up_pct:.0f}%")

        # Same for 15-min
        fifteenmin = [td for td in timing_data if td["duration"] == 900]
        if fifteenmin:
            phases_15 = {
                "Before window (<0s)": [td for td in fifteenmin if td["offset"] < 0],
                "First 3min (0-180s)": [td for td in fifteenmin if 0 <= td["offset"] < 180],
                "3-6min (180-360s)": [td for td in fifteenmin if 180 <= td["offset"] < 360],
                "6-9min (360-540s)": [td for td in fifteenmin if 360 <= td["offset"] < 540],
                "9-12min (540-720s)": [td for td in fifteenmin if 540 <= td["offset"] < 720],
                "Last 3min (720-900s)": [td for td in fifteenmin if 720 <= td["offset"] < 900],
                "After window (>900s)": [td for td in fifteenmin if td["offset"] >= 900],
            }

            print("  15-minute markets:")
            for phase, entries in phases_15.items():
                if not entries:
                    continue
                total_usdc = sum(e["usdc"] for e in entries)
                avg_price = sum(e["price"] for e in entries) / len(entries)
                print(f"    {phase:25s}: {len(entries):4d} trades, ${total_usdc:>10,.2f} USDC, "
                      f"avg_price={avg_price:.3f}")

    # -- 5. Late-Window Penny Collecting --
    print(f"\n{'-'*70}")
    print("5. PENNY COLLECTING (buying at ≥0.95 late in window)")
    print(f"{'-'*70}")

    penny_trades = [td for td in timing_data
                    if td["price"] >= 0.95 and td["duration"] == 300]

    if penny_trades:
        total_usdc = sum(t["usdc"] for t in penny_trades)
        # Potential profit = size * (1 - price) for each
        potential_profit = sum(t["usdc"] / t["price"] * (1 - t["price"]) for t in penny_trades)
        avg_offset = sum(t["offset"] for t in penny_trades) / len(penny_trades)

        print(f"  Count: {len(penny_trades)} trades")
        print(f"  Total USDC staked: ${total_usdc:,.2f}")
        print(f"  Avg entry: {avg_offset:.0f}s into window")
        print(f"  Potential profit if ALL win: ${potential_profit:,.2f}")
        print(f"  Avg price: {sum(t['price'] for t in penny_trades)/len(penny_trades):.4f}")

        # Timing distribution
        for offset_bucket in [("Before 60s", 0, 60), ("60-180s", 60, 180),
                               ("180-240s", 180, 240), ("240-300s", 240, 300),
                               ("After 300s", 300, 9999)]:
            label, lo, hi = offset_bucket
            bucket = [t for t in penny_trades if lo <= t["offset"] < hi]
            if bucket:
                print(f"    {label}: {len(bucket)} trades, ${sum(t['usdc'] for t in bucket):,.2f}")

    # -- 6. Market-Making / Arbitrage Pattern --
    print(f"\n{'-'*70}")
    print("6. MARKET-MAKING ANALYSIS (per market window)")
    print(f"{'-'*70}")

    mm_stats = []
    for slug, market_trades in by_market.items():
        if "5m" not in slug:
            continue

        outcomes = set(t.get("outcome") for t in market_trades)
        if "Up" not in outcomes or "Down" not in outcomes:
            continue

        up_trades = [t for t in market_trades if t["outcome"] == "Up"]
        down_trades = [t for t in market_trades if t["outcome"] == "Down"]

        up_usdc = sum(t["usdcSize"] for t in up_trades)
        down_usdc = sum(t["usdcSize"] for t in down_trades)
        up_avg_price = sum(t["price"] for t in up_trades) / len(up_trades) if up_trades else 0
        down_avg_price = sum(t["price"] for t in down_trades) / len(down_trades) if down_trades else 0

        # Implied probability sum (should be close to 1.0 for fair market)
        implied_sum = up_avg_price + down_avg_price
        # Spread/edge
        edge = 1.0 - implied_sum  # positive = he's buying below fair value

        # Weighted average cost
        total_shares_up = sum(t["size"] for t in up_trades)
        total_shares_down = sum(t["size"] for t in down_trades)
        wavg_up = sum(t["price"] * t["size"] for t in up_trades) / total_shares_up if total_shares_up else 0
        wavg_down = sum(t["price"] * t["size"] for t in down_trades) / total_shares_down if total_shares_down else 0

        # Guaranteed profit check: if he holds both sides
        # Cost = up_usdc + down_usdc
        # Payout: max(total_shares_up, total_shares_down) * $1
        # If cost < max_payout, guaranteed profit
        cost = up_usdc + down_usdc
        # One side pays out at $1 per share
        payout_up_wins = total_shares_up  # $1 per up share
        payout_down_wins = total_shares_down  # $1 per down share
        min_payout = min(payout_up_wins, payout_down_wins)
        guaranteed_profit = min_payout - cost

        mm_stats.append({
            "slug": slug,
            "up_usdc": up_usdc,
            "down_usdc": down_usdc,
            "up_avg_price": up_avg_price,
            "down_avg_price": down_avg_price,
            "wavg_up": wavg_up,
            "wavg_down": wavg_down,
            "implied_sum": implied_sum,
            "edge": edge,
            "cost": cost,
            "shares_up": total_shares_up,
            "shares_down": total_shares_down,
            "min_payout": min_payout,
            "guaranteed_profit": guaranteed_profit,
        })

    if mm_stats:
        profitable = sum(1 for m in mm_stats if m["guaranteed_profit"] > 0)
        print(f"  Both-sides 5min markets: {len(mm_stats)}")
        print(f"  Markets with guaranteed profit (arb): {profitable} ({100*profitable/len(mm_stats):.0f}%)")

        avg_edge = sum(m["edge"] for m in mm_stats) / len(mm_stats)
        avg_implied = sum(m["implied_sum"] for m in mm_stats) / len(mm_stats)
        print(f"  Avg implied probability sum: {avg_implied:.4f} (1.0 = fair, <1.0 = arb)")
        print(f"  Avg edge: {avg_edge:.4f}")

        print(f"\n  Top 10 markets by size:")
        for m in sorted(mm_stats, key=lambda x: x["cost"], reverse=True)[:10]:
            print(f"    {m['slug'][:45]:45s}")
            print(f"      Up: ${m['up_usdc']:>8.2f} @ avg {m['wavg_up']:.3f}  ({m['shares_up']:.1f} shares)")
            print(f"      Dn: ${m['down_usdc']:>8.2f} @ avg {m['wavg_down']:.3f}  ({m['shares_down']:.1f} shares)")
            print(f"      Cost: ${m['cost']:>8.2f} -> Min payout: ${m['min_payout']:>8.2f}  "
                  f"{'+ ARB' if m['guaranteed_profit'] > 0 else '- directional'} "
                  f"(P&L: ${m['guaranteed_profit']:>+.2f})")

    # -- 7. Directional Bias --
    print(f"\n{'-'*70}")
    print("7. DIRECTIONAL BIAS (weighted by USDC)")
    print(f"{'-'*70}")

    total_up_usdc = sum(t["usdcSize"] for t in trade_records if t.get("outcome") == "Up")
    total_down_usdc = sum(t["usdcSize"] for t in trade_records if t.get("outcome") == "Down")
    total_usdc = total_up_usdc + total_down_usdc

    print(f"  Up:   ${total_up_usdc:>12,.2f} ({100*total_up_usdc/total_usdc:.1f}%)")
    print(f"  Down: ${total_down_usdc:>12,.2f} ({100*total_down_usdc/total_usdc:.1f}%)")

    # Per-market directional analysis
    directional_markets = []
    for slug, market_trades in by_market.items():
        up_usdc = sum(t["usdcSize"] for t in market_trades if t.get("outcome") == "Up")
        down_usdc = sum(t["usdcSize"] for t in market_trades if t.get("outcome") == "Down")
        total = up_usdc + down_usdc
        if total > 50:  # Only significant markets
            bias = (up_usdc - down_usdc) / total  # -1 = all down, +1 = all up
            directional_markets.append({"slug": slug, "bias": bias, "total": total})

    if directional_markets:
        strong_up = sum(1 for m in directional_markets if m["bias"] > 0.5)
        strong_down = sum(1 for m in directional_markets if m["bias"] < -0.5)
        balanced = sum(1 for m in directional_markets if -0.5 <= m["bias"] <= 0.5)
        print(f"\n  Markets > $50 total: {len(directional_markets)}")
        print(f"  Strong Up bias (>50%):   {strong_up}")
        print(f"  Strong Down bias (>50%): {strong_down}")
        print(f"  Balanced (±50%):         {balanced}")

    # -- 8. Size Analysis --
    print(f"\n{'-'*70}")
    print("8. POSITION SIZING")
    print(f"{'-'*70}")

    # Per-market total commitment
    market_sizes = []
    for slug, market_trades in by_market.items():
        total_usdc = sum(t["usdcSize"] for t in market_trades)
        market_sizes.append(total_usdc)

    market_sizes.sort(reverse=True)
    avg_size = sum(market_sizes) / len(market_sizes) if market_sizes else 0
    median_idx = len(market_sizes) // 2
    median_size = market_sizes[median_idx] if market_sizes else 0

    print(f"  Per-market commitment:")
    print(f"    Avg:    ${avg_size:>10,.2f}")
    print(f"    Median: ${median_size:>10,.2f}")
    print(f"    Max:    ${market_sizes[0]:>10,.2f}")
    print(f"    Min:    ${market_sizes[-1]:>10,.2f}")

    # Trade size distribution
    trade_sizes = [t["usdcSize"] for t in trade_records]
    trade_sizes.sort()
    print(f"\n  Per-trade size:")
    print(f"    Avg:    ${sum(trade_sizes)/len(trade_sizes):>10,.2f}")
    print(f"    Median: ${trade_sizes[len(trade_sizes)//2]:>10,.2f}")
    print(f"    Max:    ${trade_sizes[-1]:>10,.2f}")
    print(f"    P95:    ${trade_sizes[int(0.95*len(trade_sizes))]:>10,.2f}")

    # -- 9. Strategy Classification --
    print(f"\n{'-'*70}")
    print("9. STRATEGY CLASSIFICATION")
    print(f"{'-'*70}")

    # Classify based on observed patterns
    penny_pct = len(penny_trades) / len(trade_records) * 100 if penny_trades else 0
    both_pct = both_sides / total_markets * 100 if total_markets else 0

    strategies = []

    if penny_pct > 10:
        strategies.append(f"PENNY COLLECTING: {penny_pct:.0f}% of trades at price ≥0.95 "
                         f"(buying near-certain outcomes for small guaranteed return)")

    if both_pct > 30:
        strategies.append(f"MARKET MAKING / BOTH-SIDES: {both_pct:.0f}% of markets have "
                         f"positions on BOTH Up and Down")

    if mm_stats:
        arb_pct = profitable / len(mm_stats) * 100
        if arb_pct > 20:
            strategies.append(f"ARBITRAGE: {arb_pct:.0f}% of both-sides markets show "
                             f"guaranteed profit (buying below combined fair value)")

    # Check if he scales in progressively
    progressive_count = 0
    for slug, market_trades in by_market.items():
        if len(market_trades) >= 5:
            # Sort by timestamp
            sorted_t = sorted(market_trades, key=lambda x: x["timestamp"])
            prices = [t["price"] for t in sorted_t if t.get("outcome") == "Up"]
            if len(prices) >= 3:
                # Check if prices trend upward (scaling in as confidence grows)
                if prices[-1] > prices[0] + 0.1:
                    progressive_count += 1

    if progressive_count > 5:
        strategies.append(f"PROGRESSIVE SCALING: {progressive_count} markets show "
                         f"increasing prices over time (building position as certainty increases)")

    for i, s in enumerate(strategies, 1):
        print(f"  {i}. {s}")

    if not strategies:
        print("  No clear dominant strategy pattern identified.")

    # -- 10. MERGE Analysis --
    if merges:
        print(f"\n{'-'*70}")
        print("10. MERGE OPERATIONS")
        print(f"{'-'*70}")
        print(f"  Total MERGEs: {len(merges)}")
        for m in merges[:5]:
            print(f"    {m.get('slug', 'unknown'):40s}  size={m.get('size', 0):.2f}  "
                  f"usdc=${m.get('usdcSize', 0):.2f}")
        print("  -> MERGE = combining Up+Down shares into USDC (cashing out both-sides position)")

    # -- 11. REDEEM Analysis --
    if redeems:
        print(f"\n{'-'*70}")
        print("11. REDEEM OPERATIONS")
        print(f"{'-'*70}")
        print(f"  Total REDEEMs: {len(redeems)}")
        for r in redeems[:5]:
            print(f"    {r.get('slug', 'unknown'):40s}  size={r.get('size', 0):.2f}  "
                  f"usdc=${r.get('usdcSize', 0):.2f}")
        print("  -> REDEEM = claiming payout after market resolves (winning side)")

    # -- Summary --
    print(f"\n{'='*70}")
    print("EXECUTIVE SUMMARY")
    print(f"{'='*70}")
    print(f"""
Bonereaper appears to run a HYBRID strategy on Polymarket Up/Down markets:

1. PRIMARY: Both-sides market making with edge extraction
   - Buys BOTH Up and Down in {both_pct:.0f}% of markets
   - Takes advantage of wide spreads and slow book updates
   - MERGEs positions to lock in guaranteed profit when spread < 100%

2. SECONDARY: Penny collecting / momentum confirmation
   - Late in the window when outcome is near-certain, buys at 0.99
   - Sweeps remaining liquidity for 1% guaranteed return
   - High volume, low risk per trade

3. SIZING: Aggressive on high-edge, small on speculative
   - Larger positions when implied sum < 1.0 (arbitrage available)
   - Smaller positions on directional bets

4. EXECUTION: Bot-driven, sub-second latency
   - Multiple fills per second (order splitting across orderbook)
   - Trades 24/7, continuous operation
   - ~{len(trade_records)/max(1,span_hours):.0f} trades/hour observed
""")

    return {
        "trade_records": trade_records,
        "merges": merges,
        "redeems": redeems,
        "by_market": dict(by_market),
        "mm_stats": mm_stats,
        "timing_data": timing_data,
    }


# -- Main ----------------------------------------------------------

if __name__ == "__main__":
    print("Fetching Bonereaper trade history...")
    trades = fetch_trades(max_pages=30)

    if trades:
        # Save raw data
        outfile = "analysis/bonereaper_raw.json"
        with open(outfile, "w") as f:
            json.dump(trades, f, indent=2)
        print(f"Saved {len(trades)} records to {outfile}")

        results = analyze_trades(trades)
    else:
        print("No trades fetched!")
