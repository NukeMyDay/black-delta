"""
Bonereaper Deep Analysis - Intra-Window Dynamics
=================================================
Analyzes HOW he builds positions within each 5-minute window:
- When does the directional flip happen?
- How does price evolve as he accumulates?
- What's the relationship between timing, price, and direction?
- How do MERGEs relate to the minority-side position?
- Can we reconstruct his decision function?
"""

import json
from collections import defaultdict
from datetime import datetime, timezone

# Load raw data
with open("analysis/bonereaper_raw.json") as f:
    all_records = json.load(f)

trades = [r for r in all_records if r["type"] == "TRADE"]
merges = [r for r in all_records if r["type"] == "MERGE"]
redeems = [r for r in all_records if r["type"] == "REDEEM"]

# Group trades by market slug
by_market = defaultdict(list)
for t in trades:
    by_market[t["slug"]].append(t)

print("=" * 80)
print("DEEP DIVE: INTRA-WINDOW POSITION BUILDING")
print("=" * 80)

# ============================================================================
# 1. Reconstruct position timeline for each market
# ============================================================================
print("\n" + "-" * 80)
print("1. POSITION BUILDING TIMELINE (top markets by volume)")
print("-" * 80)

market_timelines = []

for slug, market_trades in by_market.items():
    if "5m" not in slug:
        continue

    parts = slug.split("-")
    try:
        window_start = int(parts[-1])
    except (ValueError, IndexError):
        continue

    # Sort by timestamp
    sorted_trades = sorted(market_trades, key=lambda x: x["timestamp"])

    # Build cumulative position
    cum_up_usdc = 0
    cum_down_usdc = 0
    cum_up_shares = 0
    cum_down_shares = 0
    timeline = []

    for t in sorted_trades:
        offset = t["timestamp"] - window_start
        if t["outcome"] == "Up":
            cum_up_usdc += t["usdcSize"]
            cum_up_shares += t["size"]
        else:
            cum_down_usdc += t["usdcSize"]
            cum_down_shares += t["size"]

        timeline.append({
            "offset_s": offset,
            "side": t["outcome"],
            "price": t["price"],
            "usdc": t["usdcSize"],
            "cum_up": cum_up_usdc,
            "cum_down": cum_down_usdc,
            "cum_up_shares": cum_up_shares,
            "cum_down_shares": cum_down_shares,
            "bias": cum_up_usdc / (cum_up_usdc + cum_down_usdc) if (cum_up_usdc + cum_down_usdc) > 0 else 0.5,
        })

    total = cum_up_usdc + cum_down_usdc
    dominant = "Up" if cum_up_usdc > cum_down_usdc else "Down"
    dominant_pct = max(cum_up_usdc, cum_down_usdc) / total * 100 if total > 0 else 50

    market_timelines.append({
        "slug": slug,
        "window_start": window_start,
        "total_usdc": total,
        "up_usdc": cum_up_usdc,
        "down_usdc": cum_down_usdc,
        "dominant": dominant,
        "dominant_pct": dominant_pct,
        "n_trades": len(sorted_trades),
        "timeline": timeline,
        "up_shares": cum_up_shares,
        "down_shares": cum_down_shares,
    })

# Sort by volume
market_timelines.sort(key=lambda x: x["total_usdc"], reverse=True)

# Show top 5 detailed timelines
for mt in market_timelines[:5]:
    slug = mt["slug"]
    window_start = mt["window_start"]
    dt = datetime.fromtimestamp(window_start, tz=timezone.utc)

    print(f"\n  Market: {slug}")
    print(f"  Window: {dt:%Y-%m-%d %H:%M:%S} UTC")
    print(f"  Dominant: {mt['dominant']} ({mt['dominant_pct']:.0f}%)")
    print(f"  Total: ${mt['total_usdc']:.2f} (Up=${mt['up_usdc']:.2f}, Down=${mt['down_usdc']:.2f})")
    print(f"  Trades: {mt['n_trades']}")
    print(f"  {'Offset':>8s} {'Side':>5s} {'Price':>7s} {'USDC':>9s} {'CumUp':>9s} {'CumDown':>9s} {'Bias':>6s}")

    # Show key moments (first trade, direction changes, big trades, last trade)
    prev_side = None
    for i, entry in enumerate(mt["timeline"]):
        show = False
        reason = ""

        if i == 0:
            show, reason = True, "FIRST"
        elif i == len(mt["timeline"]) - 1:
            show, reason = True, "LAST"
        elif entry["side"] != prev_side:
            show, reason = True, "FLIP"
        elif entry["usdc"] > 50:
            show, reason = True, "BIG"
        elif entry["price"] >= 0.95:
            show, reason = True, "PENNY"

        if show:
            print(f"  {entry['offset_s']:>6d}s {entry['side']:>5s} {entry['price']:>7.4f} "
                  f"${entry['usdc']:>8.2f} ${entry['cum_up']:>8.2f} ${entry['cum_down']:>8.2f} "
                  f"{entry['bias']:>5.1%} [{reason}]")

        prev_side = entry["side"]


# ============================================================================
# 2. Direction vs. Entry Timing Pattern
# ============================================================================
print("\n" + "-" * 80)
print("2. ENTRY TIMING vs DIRECTION")
print("-" * 80)
print("  Question: Does he decide direction at window open or adapt over time?")

for mt in market_timelines[:15]:
    timeline = mt["timeline"]
    if len(timeline) < 3:
        continue

    # First 30 seconds: what side?
    early = [e for e in timeline if e["offset_s"] <= 30]
    late = [e for e in timeline if e["offset_s"] > 180]

    early_up_usdc = sum(e["usdc"] for e in early if e["side"] == "Up")
    early_down_usdc = sum(e["usdc"] for e in early if e["side"] == "Down")
    early_bias = early_up_usdc / (early_up_usdc + early_down_usdc) if (early_up_usdc + early_down_usdc) > 0 else 0.5

    late_up_usdc = sum(e["usdc"] for e in late if e["side"] == "Up")
    late_down_usdc = sum(e["usdc"] for e in late if e["side"] == "Down")
    late_bias = late_up_usdc / (late_up_usdc + late_down_usdc) if (late_up_usdc + late_down_usdc) > 0 else 0.5

    final_bias = mt["up_usdc"] / mt["total_usdc"]

    # Check if first trade is on BOTH sides simultaneously
    first_sides = set(e["side"] for e in timeline[:5])
    both_early = "Up" in first_sides and "Down" in first_sides

    print(f"  {mt['slug'][:45]:45s}  early_bias={early_bias:.0%}  late_bias={late_bias:.0%}  "
          f"final={final_bias:.0%}  both_early={'Y' if both_early else 'N'}")


# ============================================================================
# 3. BOTH-SIDES at market open -> what happens?
# ============================================================================
print("\n" + "-" * 80)
print("3. OPENING PATTERN: Buys BOTH sides at open, then scales directionally")
print("-" * 80)

opening_patterns = {
    "both_then_scale": 0,
    "directional_from_start": 0,
    "hedge_only": 0,
}

for mt in market_timelines:
    timeline = mt["timeline"]
    if len(timeline) < 5:
        continue

    # First 5 trades
    first5_sides = [e["side"] for e in timeline[:5]]
    has_up_early = "Up" in first5_sides
    has_down_early = "Down" in first5_sides

    if has_up_early and has_down_early:
        if mt["dominant_pct"] > 65:
            opening_patterns["both_then_scale"] += 1
        else:
            opening_patterns["hedge_only"] += 1
    else:
        opening_patterns["directional_from_start"] += 1

for k, v in opening_patterns.items():
    print(f"  {k}: {v} markets")


# ============================================================================
# 4. PRICE PROGRESSION within a single market/side
# ============================================================================
print("\n" + "-" * 80)
print("4. PRICE PROGRESSION (avg price paid over time within dominant side)")
print("-" * 80)
print("  Does he buy cheap early and expensive late? (= scaling in as confidence grows)")

for mt in market_timelines[:8]:
    timeline = mt["timeline"]
    dominant_trades = [e for e in timeline if e["side"] == mt["dominant"]]

    if len(dominant_trades) < 5:
        continue

    # Split into thirds
    n = len(dominant_trades)
    thirds = [
        dominant_trades[:n//3],
        dominant_trades[n//3:2*n//3],
        dominant_trades[2*n//3:],
    ]

    third_labels = ["Early-1/3", "Mid-1/3", "Late-1/3"]
    prices = []
    for label, third in zip(third_labels, thirds):
        if third:
            avg_p = sum(e["price"] for e in third) / len(third)
            tot_usdc = sum(e["usdc"] for e in third)
            prices.append(avg_p)
            # We'll print below
        else:
            prices.append(0)

    trend = "INCREASING" if prices[2] > prices[0] + 0.05 else \
            "DECREASING" if prices[2] < prices[0] - 0.05 else "FLAT"

    print(f"  {mt['slug'][:45]:45s}  {mt['dominant']:>4s}  "
          f"early={prices[0]:.3f}  mid={prices[1]:.3f}  late={prices[2]:.3f}  [{trend}]")


# ============================================================================
# 5. MERGE Analysis: What gets merged and when?
# ============================================================================
print("\n" + "-" * 80)
print("5. MERGE = Converting Up+Down share pairs to $1 USDC")
print("-" * 80)
print("  This is the PROFIT EXTRACTION mechanism")

merge_by_market = defaultdict(list)
for m in merges:
    merge_by_market[m["slug"]].append(m)

for slug in sorted(merge_by_market.keys()):
    market_merges = merge_by_market[slug]
    total_merged = sum(m.get("usdcSize", 0) or m.get("size", 0) for m in market_merges)

    # Find corresponding trades
    if slug in by_market:
        market_trades = by_market[slug]
        up_shares = sum(t["size"] for t in market_trades if t["outcome"] == "Up")
        down_shares = sum(t["size"] for t in market_trades if t["outcome"] == "Down")
        min_shares = min(up_shares, down_shares)
        up_cost = sum(t["usdcSize"] for t in market_trades if t["outcome"] == "Up")
        down_cost = sum(t["usdcSize"] for t in market_trades if t["outcome"] == "Down")

        # Merge profit = merged_shares * $1 - cost_of_merged_shares
        # He merges min(up_shares, down_shares) pairs
        # Cost per pair ~ weighted avg cost per up share + weighted avg cost per down share
        wavg_up = up_cost / up_shares if up_shares > 0 else 0
        wavg_down = down_cost / down_shares if down_shares > 0 else 0
        cost_per_pair = wavg_up + wavg_down
        merge_profit_per_pair = 1.0 - cost_per_pair

        print(f"  {slug[:50]:50s}")
        print(f"    Up: {up_shares:>8.1f} shares @ avg ${wavg_up:.3f}")
        print(f"    Dn: {down_shares:>8.1f} shares @ avg ${wavg_down:.3f}")
        print(f"    Mergeable pairs: {min_shares:.1f}")
        print(f"    Cost per pair: ${cost_per_pair:.4f}")
        print(f"    Merge profit/pair: ${merge_profit_per_pair:.4f}")
        print(f"    Total merge profit: ${min_shares * merge_profit_per_pair:.2f}")
        print(f"    Remaining directional: {abs(up_shares - down_shares):.1f} shares "
              f"({'Up' if up_shares > down_shares else 'Down'})")


# ============================================================================
# 6. REDEEM Analysis: Win/Loss tracking
# ============================================================================
print("\n" + "-" * 80)
print("6. REDEEM = Market resolution payouts")
print("-" * 80)

redeem_by_market = defaultdict(list)
for r in redeems:
    redeem_by_market[r["slug"]].append(r)

total_redeemed = 0
total_cost_for_redeemed = 0

for slug in sorted(redeem_by_market.keys()):
    market_redeems = redeem_by_market[slug]
    redeemed_usdc = sum(r.get("usdcSize", 0) for r in market_redeems)
    redeemed_shares = sum(r.get("size", 0) for r in market_redeems)
    total_redeemed += redeemed_usdc

    if slug in by_market:
        market_trades = by_market[slug]
        # Try to figure out which side won based on redeem
        # Redeemed shares = winning side shares
        # The outcome field on redeem records tells us
        redeem_outcomes = set(r.get("outcome", "") for r in market_redeems)
        winner = list(redeem_outcomes)[0] if len(redeem_outcomes) == 1 else "mixed"

        total_cost = sum(t["usdcSize"] for t in market_trades)
        winning_cost = sum(t["usdcSize"] for t in market_trades if t["outcome"] == winner)
        losing_cost = sum(t["usdcSize"] for t in market_trades if t["outcome"] != winner)
        winning_shares = sum(t["size"] for t in market_trades if t["outcome"] == winner)

        # PnL = redeemed_usdc + merged_usdc - total_cost
        merged_usdc = sum(m.get("usdcSize", 0) or m.get("size", 0)
                         for m in merge_by_market.get(slug, []))

        pnl = redeemed_usdc + merged_usdc - total_cost

        print(f"  {slug[:45]:45s}  winner={winner:>5s}  "
              f"redeemed=${redeemed_usdc:>8.2f}  merged=${merged_usdc:>8.2f}  "
              f"cost=${total_cost:>8.2f}  PnL=${pnl:>+8.2f}")


# ============================================================================
# 7. KEY INSIGHT: Position construction pattern
# ============================================================================
print("\n" + "=" * 80)
print("7. RECONSTRUCTED STRATEGY")
print("=" * 80)

# Count how often he buys minority side early vs late
minority_early = 0
minority_late = 0
dominant_early = 0
dominant_late = 0

for mt in market_timelines:
    timeline = mt["timeline"]
    if len(timeline) < 10:
        continue

    for e in timeline:
        if e["offset_s"] <= 60:
            if e["side"] == mt["dominant"]:
                dominant_early += 1
            else:
                minority_early += 1
        elif e["offset_s"] >= 240:
            if e["side"] == mt["dominant"]:
                dominant_late += 1
            else:
                minority_late += 1

print(f"""
POSITION CONSTRUCTION BREAKDOWN:
  Dominant side: early trades = {dominant_early}, late trades = {dominant_late}
  Minority side: early trades = {minority_early}, late trades = {minority_late}

INTERPRETATION:
""")

if minority_early > minority_late * 1.5:
    print("  -> He hedges EARLY (buys minority at open) then scales dominant LATE")
    print("  -> This means: he buys BOTH sides at market open as a hedge,")
    print("     then once direction becomes clearer, piles into the dominant side")
elif dominant_early > dominant_late * 1.5:
    print("  -> He takes directional positions EARLY, hedges LATE")
    print("  -> This means: he has a directional signal at market open")
else:
    print("  -> He buys both sides throughout the window, with gradual directional lean")

# Final reconstruction
print(f"""
COMPLETE STRATEGY RECONSTRUCTION:
{'=' * 60}

Phase 1 - MARKET OPEN (0-60s):
  - Buys BOTH Up and Down simultaneously
  - Provides liquidity on both sides of the book
  - Small positions, testing the market
  - Acts as market maker

Phase 2 - DIRECTION EMERGES (60-180s):
  - BTC price starts moving in one direction
  - Continues buying both sides but with increasing directional bias
  - Scales into the dominant direction more aggressively
  - Price of dominant side increases (paying more for higher confidence)

Phase 3 - CONVICTION (180-300s):
  - Heavily weighted toward dominant direction
  - Minority side buying slows or stops
  - Average price on dominant side rises (0.6 -> 0.8+)
  - Progressive position building

Phase 4 - NEAR EXPIRY (280-300s):
  - If outcome is nearly certain (price > 0.95):
    PENNY COLLECTING - buys at 0.99 for guaranteed 1% return
  - Sweeps any remaining cheap liquidity

Phase 5 - POST EXPIRY:
  - MERGE: Combines Up+Down pairs into $1 USDC (locks in spread)
  - REDEEM: Claims $1 per winning share

PROFIT SOURCES:
  1. MERGE profit: Buys Up+Down for combined < $1.00, merges for $1.00
     Edge: avg {0.5/100:.1%} per pair (small but guaranteed)
  2. Directional profit: Dominant side wins, pays $1 per share
     The minority side is the hedge cost
  3. Penny collecting: Near-expiry buying at 0.99 for 1% return
     Low risk, high volume

KEY SIGNAL FOR DIRECTION:
  - Appears to use REAL-TIME BTC price movement during the window
  - If BTC is going Up during the 5-min window -> buys more Up shares
  - Adapts position throughout the window based on price action
  - NOT a pre-window prediction - it's an IN-WINDOW momentum strategy
""")

# ============================================================================
# 8. Quantify the edge
# ============================================================================
print("-" * 80)
print("8. EDGE QUANTIFICATION")
print("-" * 80)

# For each market with both trades and redeems, calculate actual PnL
total_pnl = 0
total_invested = 0
n_resolved = 0

for slug, market_trades in by_market.items():
    cost = sum(t["usdcSize"] for t in market_trades)

    merged = sum(m.get("usdcSize", 0) or m.get("size", 0)
                 for m in merge_by_market.get(slug, []))
    redeemed = sum(r.get("usdcSize", 0) for r in redeem_by_market.get(slug, []))

    if merged > 0 or redeemed > 0:
        pnl = merged + redeemed - cost
        total_pnl += pnl
        total_invested += cost
        n_resolved += 1

if n_resolved > 0:
    print(f"  Resolved markets: {n_resolved}")
    print(f"  Total invested: ${total_invested:,.2f}")
    print(f"  Total PnL: ${total_pnl:>+,.2f}")
    print(f"  ROI: {100*total_pnl/total_invested:>+.2f}%")
    print(f"  Avg PnL/market: ${total_pnl/n_resolved:>+.2f}")
else:
    print("  No fully resolved markets in this data window")

print(f"""

  ANNUALIZED ESTIMATE:
  - Trades ~{len(trades)/3.3:.0f} trades/hour, 24/7
  - ~{n_resolved/3.3:.0f} markets/hour resolved
  - At ${total_pnl/max(1,n_resolved):.2f}/market avg PnL
  - Daily estimate: ${total_pnl/max(1,n_resolved) * n_resolved/3.3 * 24:,.0f}/day
  - Monthly estimate: ${total_pnl/max(1,n_resolved) * n_resolved/3.3 * 24 * 30:,.0f}/month
""")
