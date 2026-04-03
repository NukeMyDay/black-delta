"""Analyse der PULSE Trading-Historie — Edge-Kalibrierung."""

# Parsed from user's history dump (columns: time, slug, direction, order_type, btc_price, target_price, edge%, ev_per_dollar, sigma_move, outcome, pnl)
# Only resolved trades (WIN/LOSE), excluding PENDING

trades = [
    # (edge%, sigma, direction, outcome, pnl)
    (6.48, 0.00, "UP", "LOSE", -5),
    (20.18, -0.38, "UP", "LOSE", -5),
    (4.50, 0.00, "DOWN", "LOSE", -5),
    (2.19, 0.17, "UP", "LOSE", -5),
    (4.00, 0.00, "DOWN", "LOSE", -5),
    (2.50, 0.00, "UP", "WIN", 5.55),
    (8.76, -0.14, "DOWN", "WIN", 5.75),
    (3.89, -0.46, "DOWN", "LOSE", -5),
    (2.43, 0.08, "DOWN", "LOSE", -5),
    (2.52, 0.00, "UP", "LOSE", -5),
    (2.50, 0.00, "UP", "WIN", 5.55),
    (2.04, -0.15, "DOWN", "WIN", 4.35),
    (2.59, 0.05, "DOWN", "LOSE", -5),
    (2.70, -0.11, "DOWN", "WIN", 4.70),
    (4.49, 0.00, "DOWN", "WIN", 6.00),
    (5.85, -0.20, "DOWN", "LOSE", -5),
    (3.52, 0.00, "UP", "LOSE", -5),
    (3.00, 0.00, "UP", "LOSE", -5),
    (3.50, 0.00, "DOWN", "LOSE", -5),
    (2.71, 0.18, "DOWN", "LOSE", -5),
    (2.49, 0.00, "UP", "WIN", 5.55),
    (2.50, 0.00, "DOWN", "WIN", 5.55),
    (6.83, -0.34, "DOWN", "LOSE", -5),
    (3.62, -0.42, "UP", "WIN", 3.15),
    (3.87, 0.18, "DOWN", "LOSE", -5),
    (2.18, -0.32, "DOWN", "LOSE", -5),
    (2.02, 0.00, "UP", "WIN", 5.40),
    (2.49, 0.00, "DOWN", "LOSE", -5),
    (2.33, -0.08, "UP", "WIN", 4.90),
    (9.23, -0.47, "UP", "LOSE", -5),
    (2.51, 0.00, "DOWN", "LOSE", -5),
    (4.49, 0.00, "UP", "LOSE", -5),
    (2.75, 0.13, "DOWN", "LOSE", -5),
    (3.50, 0.00, "DOWN", "LOSE", -5),
    (3.22, -0.10, "DOWN", "WIN", 4.90),
    (3.00, 0.00, "UP", "WIN", 5.65),
    (4.07, 0.28, "UP", "WIN", 9.10),
    (2.50, 0.00, "UP", "LOSE", -5),
    (8.23, -0.21, "UP", "WIN", 5.10),
    (2.50, 0.00, "UP", "LOSE", -5),
    (2.77, 0.01, "UP", "LOSE", -5),
    (3.50, 0.00, "UP", "WIN", 5.75),
    (5.13, -0.14, "DOWN", "LOSE", -5),
    (2.58, 0.05, "UP", "WIN", 6.00),
    (4.50, 0.00, "DOWN", "WIN", 6.00),
    (2.51, 0.00, "DOWN", "WIN", 5.55),
    (2.55, -0.03, "DOWN", "WIN", 5.30),
    (3.77, -0.42, "DOWN", "WIN", 3.15),
    (12.12, -0.32, "DOWN", "WIN", 5.10),
    (3.49, 0.00, "DOWN", "LOSE", -5),
    (4.50, 0.00, "DOWN", "LOSE", -5),
    (2.65, -0.33, "DOWN", "WIN", 3.40),
    (2.50, 0.00, "DOWN", "WIN", 5.55),
    (8.50, 0.00, "DOWN", "LOSE", -5),
    (3.48, -0.07, "DOWN", "WIN", 5.20),
    (4.64, 0.13, "DOWN", "WIN", 7.35),
    (2.17, 0.25, "DOWN", "WIN", 8.00),
    (2.67, 0.18, "UP", "LOSE", -5),
    (2.51, 0.00, "UP", "LOSE", -5),
    (4.49, 0.00, "UP", "WIN", 6.00),
    (6.51, 0.00, "UP", "WIN", 6.50),
    (9.51, 0.00, "UP", "LOSE", -5),
    (5.50, 0.00, "UP", "WIN", 6.25),
    (3.50, 0.00, "UP", "WIN", 5.75),
    (2.99, 0.00, "DOWN", "LOSE", -5),
    (5.48, 0.00, "UP", "WIN", 6.25),
    (4.50, 0.00, "DOWN", "LOSE", -5),
    (9.50, 0.00, "UP", "LOSE", -5),
    (7.55, -0.14, "UP", "WIN", 5.55),
    (10.76, -0.28, "UP", "WIN", 5.10),
    (2.79, -0.22, "DOWN", "LOSE", -5),
    (14.23, -0.63, "DOWN", "WIN", 3.70),
    (3.62, -0.22, "UP", "LOSE", -5),
    (2.49, 0.00, "UP", "LOSE", -5),
    (6.96, -0.07, "DOWN", "LOSE", -5),
    (2.51, 0.00, "DOWN", "LOSE", -5),
    (2.50, 0.00, "DOWN", "LOSE", -5),
    (3.52, 0.00, "DOWN", "LOSE", -5),
    (3.50, 0.00, "UP", "WIN", 5.75),
    (6.71, -0.19, "DOWN", "WIN", 4.90),
    (6.50, 0.00, "DOWN", "LOSE", -5),
    (16.42, -0.83, "DOWN", "WIN", 3.20),
    (4.49, 0.00, "DOWN", "WIN", 6.00),
    (3.07, 0.12, "UP", "LOSE", -5),
    (2.27, 0.06, "UP", "WIN", 6.00),
    (4.54, -0.19, "DOWN", "WIN", 4.50),
    (2.50, 0.00, "UP", "LOSE", -5),
    (5.01, -0.09, "UP", "WIN", 5.30),
    (3.16, -0.21, "UP", "LOSE", -5),
    (3.51, 0.00, "UP", "WIN", 5.75),
    (6.51, 0.00, "UP", "LOSE", -5),
    (3.07, 0.04, "UP", "WIN", 6.00),
    (3.18, 0.28, "DOWN", "WIN", 8.70),
    (2.27, -0.05, "DOWN", "LOSE", -5),
    (2.43, -0.27, "DOWN", "LOSE", -5),
    (2.64, -0.11, "DOWN", "LOSE", -5),
    (3.46, -0.03, "UP", "WIN", 5.55),
    (3.35, -0.05, "UP", "LOSE", -5),
    (12.13, -0.32, "UP", "WIN", 5.10),
    (3.53, -0.14, "UP", "WIN", 4.70),
    (6.81, -0.22, "UP", "LOSE", -5),
    (5.53, -0.14, "DOWN", "WIN", 5.10),
    (3.50, 0.00, "DOWN", "LOSE", -5),
    (2.50, 0.00, "UP", "WIN", 5.55),
    (3.50, 0.00, "UP", "LOSE", -5),
    (2.75, -0.33, "UP", "LOSE", -5),
    (2.36, 0.36, "DOWN", "LOSE", -5),
    (3.49, 0.00, "DOWN", "LOSE", -5),
    (2.50, 0.00, "DOWN", "WIN", 5.55),
    (6.82, -0.45, "DOWN", "WIN", 3.40),
    (4.19, 0.12, "UP", "LOSE", -5),
    (3.05, -0.22, "UP", "LOSE", -5),
    (3.44, 0.24, "UP", "LOSE", -5),
    (2.50, 0.00, "UP", "WIN", 5.55),
    (3.50, 0.00, "DOWN", "LOSE", -5),
    (2.50, 0.00, "DOWN", "WIN", 5.55),
    (2.50, 0.00, "DOWN", "WIN", 5.55),
    (3.83, -0.01, "UP", "LOSE", -5),
    (2.25, -0.10, "DOWN", "LOSE", -5),
    (2.51, 0.00, "DOWN", "WIN", 5.55),
    (4.25, -0.18, "UP", "WIN", 4.50),
    (10.84, -0.28, "DOWN", "WIN", 5.10),
    (4.03, -0.09, "UP", "LOSE", -5),
    (2.01, 0.00, "UP", "WIN", 5.40),
    (2.50, 0.00, "UP", "LOSE", -5),
    (6.49, 0.00, "UP", "WIN", 6.50),
    (2.57, -0.16, "UP", "WIN", 4.35),
    (4.50, 0.00, "UP", "WIN", 6.00),
    (6.04, -0.15, "DOWN", "LOSE", -5),
    (2.50, 0.00, "DOWN", "LOSE", -5),
    (7.49, 0.00, "DOWN", "LOSE", -5),
    (2.50, 0.00, "DOWN", "WIN", 5.55),
    (2.68, -0.06, "DOWN", "LOSE", -5),
    (4.50, 0.00, "DOWN", "LOSE", -5),
    (8.50, 0.00, "DOWN", "WIN", 7.05),
    (7.78, -0.65, "DOWN", "WIN", 2.75),
    (3.49, 0.00, "DOWN", "LOSE", -5),
    (2.17, -0.26, "UP", "LOSE", -5),
    (2.51, 0.00, "DOWN", "WIN", 5.55),
    (5.53, 0.21, "DOWN", "LOSE", -5),
    (19.73, -0.19, "DOWN", "WIN", 8.35),
    (3.17, -0.13, "UP", "LOSE", -5),
    (4.86, -0.12, "DOWN", "LOSE", -5),
    (2.45, 0.03, "UP", "WIN", 5.75),
    (2.73, -0.23, "UP", "WIN", 3.95),
    (4.50, 0.00, "DOWN", "WIN", 6.00),
    (3.33, 0.03, "UP", "LOSE", -5),
    (3.49, 0.00, "DOWN", "LOSE", -5),
    (7.01, 0.09, "UP", "WIN", 7.65),
    (5.50, 0.00, "UP", "WIN", 6.25),
]


def analyze():
    print("=" * 70)
    print("PULSE TRADING ANALYSIS")
    print("=" * 70)

    total = len(trades)
    wins = [t for t in trades if t[3] == "WIN"]
    losses = [t for t in trades if t[3] == "LOSE"]
    print(f"\nTotal resolved trades: {total}")
    print(f"Wins: {len(wins)} ({len(wins)/total*100:.1f}%)")
    print(f"Losses: {len(losses)} ({len(losses)/total*100:.1f}%)")
    print(f"Total PnL: ${sum(t[4] for t in trades):.2f}")
    print(f"Avg Win: ${sum(t[4] for t in wins)/len(wins):.2f}")
    print(f"Avg Loss: ${sum(t[4] for t in losses)/len(losses):.2f}")

    # By edge bucket
    print("\n" + "=" * 70)
    print("WIN RATE BY EDGE BUCKET")
    print("=" * 70)
    buckets = [
        ("2.0-3.0%", 2.0, 3.0),
        ("3.0-5.0%", 3.0, 5.0),
        ("5.0-8.0%", 5.0, 8.0),
        ("8.0-12.0%", 8.0, 12.0),
        ("12.0%+", 12.0, 100.0),
    ]
    for name, lo, hi in buckets:
        bucket = [t for t in trades if lo <= t[0] < hi]
        if not bucket:
            continue
        w = sum(1 for t in bucket if t[3] == "WIN")
        pnl = sum(t[4] for t in bucket)
        print(f"  {name:12s}  n={len(bucket):3d}  wins={w:3d}  "
              f"winrate={w/len(bucket)*100:5.1f}%  PnL=${pnl:+.2f}")

    # By sigma bucket
    print("\n" + "=" * 70)
    print("WIN RATE BY SIGMA MOVE")
    print("=" * 70)
    sigma_buckets = [
        ("sigma=0 (at target)", -0.005, 0.005),
        ("sigma<0 (favorable)", -10, -0.005),
        ("sigma>0 (unfavorable)", 0.005, 10),
    ]
    for name, lo, hi in sigma_buckets:
        bucket = [t for t in trades if lo <= t[1] < hi]
        if not bucket:
            continue
        w = sum(1 for t in bucket if t[3] == "WIN")
        pnl = sum(t[4] for t in bucket)
        avg_edge = sum(t[0] for t in bucket) / len(bucket)
        print(f"  {name:25s}  n={len(bucket):3d}  wins={w:3d}  "
              f"winrate={w/len(bucket)*100:5.1f}%  PnL=${pnl:+.2f}  avg_edge={avg_edge:.1f}%")

    # By direction
    print("\n" + "=" * 70)
    print("WIN RATE BY DIRECTION")
    print("=" * 70)
    for d in ["UP", "DOWN"]:
        bucket = [t for t in trades if t[2] == d]
        w = sum(1 for t in bucket if t[3] == "WIN")
        pnl = sum(t[4] for t in bucket)
        print(f"  {d:6s}  n={len(bucket):3d}  wins={w:3d}  "
              f"winrate={w/len(bucket)*100:5.1f}%  PnL=${pnl:+.2f}")

    # Combined: sigma=0 by direction
    print("\n" + "=" * 70)
    print("SIGMA=0 TRADES BY DIRECTION (coin flips)")
    print("=" * 70)
    for d in ["UP", "DOWN"]:
        bucket = [t for t in trades if t[2] == d and abs(t[1]) < 0.005]
        if not bucket:
            continue
        w = sum(1 for t in bucket if t[3] == "WIN")
        pnl = sum(t[4] for t in bucket)
        avg_edge = sum(t[0] for t in bucket) / len(bucket)
        print(f"  {d:6s}  n={len(bucket):3d}  wins={w:3d}  "
              f"winrate={w/len(bucket)*100:5.1f}%  PnL=${pnl:+.2f}  avg_edge={avg_edge:.1f}%")

    # Breakeven analysis
    print("\n" + "=" * 70)
    print("BREAKEVEN ANALYSIS")
    print("=" * 70)
    avg_win = sum(t[4] for t in wins) / len(wins)
    avg_loss = abs(sum(t[4] for t in losses) / len(losses))
    breakeven_wr = avg_loss / (avg_win + avg_loss) * 100
    print(f"  Avg win:  ${avg_win:.2f}")
    print(f"  Avg loss: ${avg_loss:.2f}")
    print(f"  Breakeven win rate needed: {breakeven_wr:.1f}%")
    print(f"  Current win rate: {len(wins)/total*100:.1f}%")
    print(f"  Gap: {len(wins)/total*100 - breakeven_wr:+.1f}pp")

    # High-edge trades only (simulating higher threshold)
    print("\n" + "=" * 70)
    print("SIMULATED THRESHOLD IMPACT")
    print("=" * 70)
    for threshold in [2, 3, 4, 5, 6, 8, 10]:
        subset = [t for t in trades if t[0] >= threshold]
        if not subset:
            continue
        w = sum(1 for t in subset if t[3] == "WIN")
        pnl = sum(t[4] for t in subset)
        wr = w / len(subset) * 100
        print(f"  >= {threshold:2d}%  n={len(subset):3d}  wins={w:3d}  "
              f"winrate={wr:5.1f}%  PnL=${pnl:+7.2f}  "
              f"PnL/trade=${pnl/len(subset):+.2f}")


    # Simulate new filters
    print("\n" + "=" * 70)
    print("SIMULATED NEW FILTERS (edge>=8%, sigma<=0, no DOWN@sigma~0)")
    print("=" * 70)
    filtered = [t for t in trades
                if t[0] >= 8.0          # edge >= 8%
                and t[1] <= 0           # sigma <= 0
                and not (t[2] == "DOWN" and abs(t[1]) < 0.05)]  # no DOWN at sigma~0
    if filtered:
        w = sum(1 for t in filtered if t[3] == "WIN")
        pnl = sum(t[4] for t in filtered)
        print(f"  Trades: {len(filtered)}")
        print(f"  Wins: {w} ({w/len(filtered)*100:.1f}%)")
        print(f"  PnL: ${pnl:+.2f}")
        print(f"  PnL/trade: ${pnl/len(filtered):+.2f}")
    else:
        print("  No trades match filters")

    # Also simulate edge>=8% alone
    print("\n  --- edge>=8% only (for comparison) ---")
    e8 = [t for t in trades if t[0] >= 8.0]
    w8 = sum(1 for t in e8 if t[3] == "WIN")
    pnl8 = sum(t[4] for t in e8)
    print(f"  Trades: {len(e8)}  Wins: {w8} ({w8/len(e8)*100:.1f}%)  PnL: ${pnl8:+.2f}")

    # Simulate combined: edge>=5% + sigma<=0 + no DOWN@0
    print("\n  --- edge>=5% + sigma<=0 + no DOWN@sigma~0 ---")
    f5 = [t for t in trades
          if t[0] >= 5.0 and t[1] <= 0
          and not (t[2] == "DOWN" and abs(t[1]) < 0.05)]
    if f5:
        w5 = sum(1 for t in f5 if t[3] == "WIN")
        pnl5 = sum(t[4] for t in f5)
        print(f"  Trades: {len(f5)}  Wins: {w5} ({w5/len(f5)*100:.1f}%)  PnL: ${pnl5:+.2f}  PnL/trade: ${pnl5/len(f5):+.2f}")


if __name__ == "__main__":
    analyze()
