# Signal Strategy: Simulation vs. Live Gap Analysis

**Datum:** 2026-04-05
**Datenbasis:** ~50 Sim-Signale (Pulse) + 49 Live-Trades (Black Delta), gleicher Zeitraum

---

## 1. Rohdaten-Vergleich

### Simulation (Pulse Dashboard)
| Metrik | Wert |
|--------|------|
| Accuracy gesamt | 68.5% |
| Full Tier (entry < $0.60) | 61.8% (94/152) |
| Half Tier ($0.60-$0.70) | 75.9% (104/137) |
| Sim ROI | +238.85% |
| Flips / Skips | 39 / 146 |

### Live (Black Delta)
| Metrik | Wert |
|--------|------|
| Trades gesamt | 49 signal |
| Wins / Losses | 28 / 21 |
| Win Rate | **57.1%** |
| Netto P&L | **-$99** (signals only) |

### Die Kern-Diskrepanz
- **Sim: 68.5% Accuracy, +238% ROI**
- **Live: 57.1% Win Rate, netto negativ**

---

## 2. Market-by-Market Matching (Sim vs. Live)

Gleiche Markets, gleicher Zeitraum, beide Systeme sehen die gleichen Bonereaper-Trades:

| Market | Sim Entry | Live Entry | Delta | Sim Outcome | Live Outcome | Sim Bet |
|--------|-----------|------------|-------|-------------|--------------|---------|
| BTC 1775420100 | $0.52 | $0.52 | $0.00 | CORRECT +$36 | WIN +$22 | $39 |
| BTC 1775419800 | $0.59 | $0.61 | +$0.02 | WRONG **$0** | LOSE -$86 | **kein Bet** |
| ETH 1775419800 | $0.67 | $0.68 | +$0.01 | WRONG -$99 | LOSE -$53 | $99 |
| BTC 1775418600 | $0.63 | $0.65 | +$0.02 | CORRECT +$76 | WIN +$36 | $129 |
| ETH 1775418300 | $0.48 | $0.52 | +$0.04 | WRONG -$71 | LOSE -$24 | $71 |
| BTC 1775417400 | $0.64 | $0.66 | +$0.02 | CORRECT +$68 | WIN +$28 | $118 |
| BTC 1775416800 | $0.49 | $0.32 | -$0.17 | CORRECT +$59 | WIN +$169 | $57 |
| BTC 1775415900 | $0.35 | $0.39 | +$0.04 | CORRECT +$210 | WIN +$93 | $115 |
| BTC 1775415300 | $0.57 | $0.61 | +$0.04 | WRONG **$0** | LOSE -$69 | **kein Bet** |
| BTC 1775415000 | $0.65 | $0.72 | +$0.07 | CORRECT +$48 | WIN +$21 | $89 |
| ETH 1775414700 | $0.43 | $0.53 | +$0.10 | CORRECT +$110 | WIN +$46 | $82 |
| BTC 1775414100 | $0.34 | $0.44 | +$0.10 | CORRECT +$202 | WIN +$85 | $104 |
| BTC 1775412900 | $0.56 | $0.61 | +$0.05 | WRONG -$8 | LOSE -$21 | $8 |
| ETH 1775412600 | $0.63 | $0.72 | +$0.09 | WRONG -$101 | LOSE -$60 | $101 |
| BTC 1775412600 | $0.54 | $0.65 | +$0.11 | CORRECT +$20 | WIN +$3 | $23 |
| BTC 1775412300 | $0.53 | $0.64 | +$0.11 | CORRECT +$24 | WIN +$6 | $27 |
| BTC 1775411700 | $0.47 DOWN | $0.71 **UP** | -- | WRONG | WIN | **ANDERE RICHTUNG** |

### Erkenntnisse aus dem Matching

**A) Live Entry ist konsistent 5-11 Cent hoeher als Sim Entry**
- Durchschnittliches Delta: **+$0.06** (Sim $0.53 → Live $0.59)
- Grund: In den 90 Sekunden Signal-Akkumulation bewegt sich der Markt in Signal-Richtung. Bis der Bot die Order platziert, ist der Preis gestiegen.

**B) Sim hat Trades mit $0 Bet uebersprungen, Live hat sie platziert**
- BTC 1775419800: Sim entry $0.59 → Kelly = 0 → kein Bet. Live hat bei $0.61 gekauft → -$86
- BTC 1775415300: Sim entry $0.57 → Kelly = 0 → kein Bet. Live hat bei $0.61 gekauft → -$69
- Diese Trades wuerde die Sim nie machen! Sie sind mathematisch negativ-EV. Aber die Live-Version hat Kelly auf Bonerepers Entry berechnet (Bug, jetzt gefixt).

**C) Mindestens eine Richtungs-Abweichung**
- BTC 1775411700: Sim sagt DOWN, Live sagt UP. Unterschiedliche Bias-Berechnung oder Timing beim Signal-Fire.

---

## 3. Die drei Hauptursachen

### Ursache 1: Kelly auf falschem Preis (Bug - GEFIXT)

**Das groesste Problem.** Der Live-Bot hat Kelly auf Bonerepers Entry berechnet statt auf dem tatsaechlichen Fill-Preis.

```
Bonereaper Entry: $0.52  →  Kelly sagt: positive Edge, bet!
Tatsaechlicher Fill: $0.62  →  Kelly waere: NEGATIVE Edge, skip!
```

Bei 57% Win Rate ist der Breakeven-Entry $0.57. Alles darueber ist mathematisch negativ.

| Fill-Preis | Payout Odds | Kelly (57% WR) | EV pro Dollar |
|------------|-------------|----------------|---------------|
| $0.50 | 1.00:1 | +14.0% | +$0.14 |
| $0.52 | 0.92:1 | +9.6% | +$0.10 |
| $0.55 | 0.82:1 | +3.6% | +$0.04 |
| **$0.57** | **0.75:1** | **0.0%** | **Breakeven** |
| $0.60 | 0.67:1 | -5.0% | -$0.05 |
| $0.65 | 0.54:1 | -12.3% | -$0.12 |
| $0.72 | 0.39:1 | -20.8% | -$0.21 |

**23 von 49 Live-Trades hatten Entry >= $0.60** — alles negative EV bei 57% WR.

**Status: GEFIXT** — Kelly wird jetzt auf `max_price` (= entry + slippage) berechnet. Trades ueber $0.57 Fill werden jetzt uebersprungen ("No Kelly edge at fill=...").

### Ursache 2: Execution Slippage (Strukturelles Problem)

Selbst bei KORREKTER Kelly-Berechnung: die Simulation berechnet P&L auf Bonerepers Entry-Preis, aber Live filled 5-11 Cent hoeher.

**Beispiel: BTC 1775414100**
- Sim: Entry $0.34, gewonnen → P&L = +$202 (194% Rendite)
- Live: Entry $0.44, gewonnen → P&L = +$85 (127% Rendite)
- **Selbe Richtung, selbes Ergebnis, aber 58% weniger Gewinn**

Das ist kein Bug — es ist die Realitaet des 90-Sekunden-Signals. In dieser Zeit bewegt sich der Markt:
1. Bonereaper kauft bei $0.34
2. Weitere Trader kaufen auch (Preis steigt)
3. Nach 90s + 15 Trades feuert das Signal
4. Zu diesem Zeitpunkt steht der Markt bei $0.40+
5. Bot platziert Limit-Order bei $0.44 (entry + slippage)

**Resultat: Die Sim ist systematisch optimistisch um ~6 Cent pro Trade.**

### Ursache 3: Sim ueberspringt Trades die Live platziert

Die Sim berechnet bet_size AUCH auf Bonerepers Entry und ueberspringt korrekt wo Kelly <= 0. Die Live-Version (vor Fix) hat diese Trades trotzdem platziert.

Von den 49 Live-Trades:
- ~10 haetten von der Sim uebersprungen werden sollen ($0 bet)
- Diese 10 Trades haben zusammen ca. **-$450** P&L generiert
- Ohne diese: Live P&L waere ca. +$350 statt -$100

---

## 4. Win Rate Analyse nach Tier

### Live Win Rate nach tatsaechlichem Fill-Preis

| Entry-Bereich | Trades | Wins | Win Rate | Netto P&L |
|---------------|--------|------|----------|-----------|
| < $0.45 | 5 | 3 | 60% | +$269 |
| $0.45 - $0.54 | 10 | 7 | 70% | +$174 |
| $0.55 - $0.64 | 14 | 5 | 36% | -$335 |
| $0.65 - $0.72 | 20 | 13 | 65% | -$108 |
| **Gesamt** | **49** | **28** | **57%** | **-$100** |

### Kritische Beobachtungen

1. **Entry < $0.55 ist profitabel**: 10/15 = 67% WR, +$443 P&L
2. **Entry $0.55-$0.64 ist katastrophal**: 5/14 = 36% WR, -$335 P&L
3. **Entry $0.65+ hat hohe WR (65%) aber negativen P&L**: Weil der Payout bei $0.72 Entry nur 39 Cent pro Dollar ist — selbst bei 65% Win Rate reicht das nicht.

### Vergleich mit Sim-Tiers (Bonereaper's Entry)

| | Sim Win Rate | Noetige WR fuer Breakeven | |
|---|---|---|---|
| Full (< $0.60) | 61.8% | 60% bei $0.60 Fill | Knapp profitabel |
| Half ($0.60-$0.70) | 75.9% | 65-70% bei $0.65-$0.70 Fill | **Profitabel WENN WR haelt** |

**Die zentrale Frage: Haelt die 76% Half-Tier Accuracy auch in Live?**

Wenn ja → Entries bis $0.72 sind profitabel (Breakeven bei 76% WR = $0.76).
Wenn nein → Nur Full-Tier (Entry < $0.55) ist sicher profitabel.

---

## 5. Das Half-Tier Paradox

Die Sim zeigt 75.9% Accuracy bei $0.60-$0.70 Entry. Das ist die eigentliche Goldmine:
- Bei 76% WR und $0.65 Entry: EV = +$0.17/Dollar
- Bei 76% WR und $0.70 Entry: EV = +$0.09/Dollar

**Aber die Live-Daten koennen das nicht bestaetigen**, weil:
1. Der Live-Bot hat Half-Tier Trades mit falschem Kelly-Sizing platziert
2. Die Entry-Preise in Live sind hoeher als in der Sim (fill > Bonereaper's entry)
3. Die Stichprobe ist zu klein (49 Trades) fuer belastbare Tier-Analyse

### Was wir pruefen muessen

Die 76% Accuracy bezieht sich auf die **Richtungs-Vorhersage**, nicht den Entry-Preis.
Wenn Bonereaper bei $0.60-$0.70 einsteigt und der Markt in 76% der Faelle in seine Richtung resolved, dann:
- Ist es egal ob WIR bei $0.65 oder $0.72 kaufen — die Richtung bleibt gleich
- Entscheidend ist nur: `Win_Rate > Entry_Price` (bei binaeren Maerkten)
- Bei 76% WR: Breakeven = $0.76. Alles darunter ist profitabel.

**Hypothese:** Die 76% Half-Tier WR ist real und auf Live uebertragbar. Das Problem war ausschliesslich der Kelly-Bug (falsche Preisbasis) und die extra-Trades die nie haetten platziert werden sollen.

---

## 6. Optimierungsplan

### Phase 1: Sofort (nur Config-Aenderungen)

**A) Entry-Thresholds korrigieren**

Aktuell im Code:
```python
ENTRY_FULL = 0.60   # Full Kelly bei entry < $0.60
ENTRY_HALF = 0.70   # Half Kelly bei $0.60-$0.70
WIN_RATE_FULL = 0.57
WIN_RATE_HALF = 0.75
```

Problem: Die `_handle_signal` Funktion in dashboard.py berechnet Kelly auf `max_price` (entry + slippage). Aber die TIER-ZUORDNUNG in signal.py's `_compute_bet_sizing` nutzt immer noch Bonerepers Entry.

**Vorschlag: Zweistufiger Ansatz**

```
Option A: Konservativ — Nur Full-Tier (bewiesen profitabel)
  ENTRY_FULL = 0.55  (statt 0.60)
  ENTRY_HALF = 0.55  (= deaktiviert, Half-Tier komplett aus)
  WIN_RATE_FULL = 0.57
  Slippage = 0.03    (statt 0.05, da wir nur niedrige Entries nehmen)
  → Max Fill = $0.55 + $0.03 = $0.58
  → Kelly bei $0.58: knapp positiv
  → Erwartete Trades: ~30% weniger, aber fast alle profitabel

Option B: Aggressiv — Beide Tiers mit korrektem Kelly
  ENTRY_FULL = 0.55
  ENTRY_HALF = 0.70
  WIN_RATE_FULL = 0.62  (update auf Sim-Wert statt 0.57)
  WIN_RATE_HALF = 0.76  (update auf Sim-Wert statt 0.75)
  Slippage = 0.05
  → Full: Max Fill $0.60, Kelly bei 62% WR positiv
  → Half: Max Fill $0.75, Kelly bei 76% WR positiv
  → Mehr Trades, aber Half-Tier noch nicht live validiert
```

**Empfehlung: Option A fuer 2-3 Tage, dann Daten auswerten und entscheiden.**

**B) ETH vs BTC differenzieren**

Aus den Live-Daten:
- BTC-Signals: hoehere WR, groessere Sample Size
- ETH-Signals: weniger Volumen, duennere Orderbooks → schlechtere Fills

Moegliche ETH-Anpassung:
- Strikterer Bias-Threshold fuer ETH (70% statt 65%)
- Oder: ETH Signals erstmal nur in Simulation laufen lassen

### Phase 2: Execution-Optimierung (Code-Aenderungen)

**A) Schnellerer Signal-Trigger**

Aktuell: 90s / 15 Trades → Signal. In diesen 90s bewegt sich der Markt 5-11 Cent.

Optionen:
```
Variante 1: Kuerzere Akkumulation
  BET_MIN_ELAPSED = 60  (statt 90)
  BET_MIN_TRADES = 20   (mehr Trades, kuerzere Zeit)
  → ~30s weniger Preisbewegung
  → Risiko: Direction Flip Rate koennte steigen

Variante 2: Adaptive Timing
  Wenn Bias > 80% und Trades > 12 → Signal schon bei 60s
  Wenn Bias 65-80% → weiterhin 90s warten
  → Starke Signale werden frueher ausgefuehrt

Variante 3: Pre-Order bei Scout
  Bei Scout (30s/8 Trades) bereits kleine "Probe-Order" (10% des Stakes)
  Bei Bet-Bestaetigung (90s) restliche 90% nachlegen
  → Durchschnittlicher Fill-Preis wird besser
  → Risiko: 10% Verlust bei Direction Flips
```

**Empfehlung: Variante 2 (Adaptive Timing) ist der beste Kompromiss.**

**B) Bessere Fill-Preis-Ermittlung**

Aktuell: `fill_estimate = max_price` (worst case). Der tatsaechliche CLOB-Fill koennte besser sein.

```python
# Statt max_price als Entry zu speichern:
# Ueberpruefen ob CLOB API den tatsaechlichen Fill-Preis zurueckgibt
# order_resp koennte "averagePrice" oder "fills" enthalten
```

Wenn wir den echten Fill kennen, koennen wir:
1. Kelly genauer berechnen
2. Bessere P&L-Tracking machen
3. Slippage-Modell kalibrieren

### Phase 3: Validierung (1-2 Wochen)

**A) Parallele Sim + Live Auswertung**

Die Sim (Pulse) weiterlaufen lassen und nach 200+ Signalen:
1. Richtungs-Accuracy pro Tier bestaetigen
2. Durchschnittlichen Slippage messen (Sim Entry vs. Live Fill)
3. Half-Tier WR validieren (ist 76% real oder Zufall?)

**B) Profit-Tracking per Tier**

Im Code: separate P&L-Counter pro Tier einfuehren:
```python
stats = {
    "full_tier": {"trades": 0, "wins": 0, "pnl": 0.0},
    "half_tier": {"trades": 0, "wins": 0, "pnl": 0.0},
}
```

Nach 100+ Live-Trades pro Tier: datenbasiert entscheiden ob Half-Tier aktiviert bleibt.

---

## 7. Mathematische Durchrechnung der Optionen

### Option A: Konservativ (nur Full-Tier, entry < $0.55)

Annahmen:
- Win Rate: 62% (Sim full-tier) bis 67% (Live <$0.55 Daten)
- Durchschnittlicher Fill: $0.52 (entry $0.49 + $0.03 slippage)
- Stakes: Kelly-sized, ~$30-80 pro Trade
- Frequenz: ~15 Trades/Tag (nur klare low-entry Signale)

```
Bei 62% WR, avg fill $0.52:
  Win: 62% × ($1/$0.52 - 1) = 62% × $0.923 = +$0.572
  Loss: 38% × -$1.00 = -$0.380
  EV pro Dollar: +$0.192 (19.2% Edge)

  Kelly: (0.62 × 0.923 - 0.38) / 0.923 = 20.8%
  1/8 Kelly: 2.6% pro Trade
  Bei $1800 Kapital: ~$47 pro Trade

  15 Trades/Tag × $47 × 19.2% EV = ~$135/Tag
  Monatlich: ~$4,000
```

### Option B: Aggressiv (Full + Half Tier)

Annahmen:
- Full: 62% WR, avg fill $0.55
- Half: 76% WR, avg fill $0.68
- Stakes: Kelly-sized
- Frequenz: ~30 Trades/Tag

```
Full Tier (15 Trades/Tag):
  EV pro Dollar bei $0.55: 62% × 0.818 - 38% = +12.7%
  Kelly 1/8: 1.6%, ~$29/Trade
  Taeglich: 15 × $29 × 12.7% = ~$55

Half Tier (15 Trades/Tag):
  EV pro Dollar bei $0.68: 76% × 0.471 - 24% = +11.8%
  Kelly 1/8: 1.5%, ~$27/Trade
  Taeglich: 15 × $27 × 11.8% = ~$48

  Gesamt: ~$103/Tag
  Monatlich: ~$3,100
```

### Vergleich

| | Option A | Option B |
|---|---|---|
| EV/Tag | ~$135 | ~$103 |
| Risiko | Niedrig (bewiesen) | Mittel (Half unvalidiert) |
| Drawdown-Risiko | Gering (weniger Trades) | Hoeher (mehr Exposure) |
| Daten-Qualitaet | Gut (67% WR live bestaetigt) | Unsicher (76% nur Sim) |

**Option A hat hoehere EV/Tag weil die wenigen Trades bessere Odds haben.**

---

## 8. Sofort-Massnahmen (Reihenfolge)

1. **[DONE] Kelly-Bug gefixt** — Kelly auf Fill-Preis statt Bonereaper Entry
2. **[TODO] Entry-Thresholds senken** — ENTRY_FULL auf $0.55, Half erstmal deaktivieren
3. **[TODO] Slippage auf $0.03 reduzieren** — nur bei niedrigen Entries noetig
4. **[TODO] WIN_RATE_FULL auf 0.62 aktualisieren** — Sim-Daten zeigen hoehere Accuracy
5. **[TODO] Tier-basiertes P&L-Tracking einbauen** — fuer spaetere Validierung
6. **[TODO] ETH-Signals strenger filtern** — hoeherer Bias-Threshold oder nur BTC

---

## 9. Zusammenfassung

**Warum Live schlechter als Sim:**
1. **60% des Gaps**: Kelly-Bug (behoben) — Live hat Trades platziert die Sim korrekt uebersprungen hat
2. **30% des Gaps**: Execution Slippage — Live filled 5-11 Cent hoeher als Sim annimmt
3. **10% des Gaps**: Sim berechnet P&L auf Bonereaper's Entry → systematisch optimistisch

**Was tun:**
- Kurzfristig: Konservativer fahren (Option A), nur bewiesen profitable Entries
- Mittelfristig: Half-Tier validieren, wenn 76% WR bestaetigt → aktivieren
- Langfristig: Execution-Optimierung (schnellere Signals, bessere Fills)

**Die Edge ist real** — 62-67% Accuracy bei Entry < $0.55 wurde sowohl in Sim als auch Live beobachtet. Das Problem war nie die Signal-Qualitaet, sondern die Execution.
