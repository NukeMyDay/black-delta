# BLACK DELTA

**Ein Entscheidungsfindungstool fuer Prediction Markets, basierend auf systematischer Ausnutzung von Wahrscheinlichkeits-Fehlbewertungen.**

---

## Philosophie

Maerkte bewerten Wahrscheinlichkeiten systematisch falsch, weil Menschen vorhersagbare kognitive Verzerrungen haben. BLACK DELTA findet diese Fehlbewertungen, quantifiziert sie, und gibt klare Handlungsempfehlungen mit positivem Expected Value.

**Kernprinzip:** Du brauchst nicht immer recht zu haben. Du brauchst Wetten, bei denen der erwartete Wert positiv ist — ueber viele Wetten gleicht sich das aus.

---

## Architektur: Zwei Module

```
BLACK DELTA
|
|-- PULSE: Kurzfrist-Statistical-Edge (BTC 5-Min Markets)   <-- MVP
|   |-- Bot-gesteuert, reine Volatilitaets-Mathematik
|   |-- Kein AI noetig, nur Statistik
|   |-- Voll automatisiert
|   |-- Ziel: Schnelle Datenpunkte, API-Infrastruktur aufbauen
|
|-- CORE: Langfrist-Edge (Geopolitik, Wirtschaft, Wissenschaft)
    |-- AI-gestuetzt (Claude API), Basisraten, Ensemble, tiefe Analyse
    |-- Semi-automatisiert (Empfehlungen + manuelle Bestaetigung)
    |-- Ziel: Groessere Edges durch tiefere Recherche
```

**PULSE ist das MVP** — es erzwingt die API-Anbindung, generiert schnell Daten, und die Infrastruktur (Auth, Orders, Tracking) wird von CORE wiederverwendet.

---

## Plattform

**Polymarket** (primaer)

- Niedrige Fees (Geopolitik: 0%, Crypto High-Frequency: max ~1.56%)
- 0% Fee auf Gewinne
- Hohe Liquiditaet auf populaeren Maerkten
- Transparentes Orderbuch (CLOB)
- Breites Event-Spektrum (Politik, Wirtschaft, Tech, Wissenschaft, Sport, Crypto)
- Crypto-basiert (USDC auf Polygon)
- Gas Fees: <$0.01 pro Trade (Polygon L2)
- Vollstaendige Trading API (Orders platzieren, canceln, Batch Orders)

**APIs:**
- **Gamma API** — Marktdaten: Events, Preise, Kategorien
- **CLOB API** — Trading: Orders platzieren, Orderbuch, Positionen
- **Data API** — Historische Daten, Zeitreihen

**SDKs:** Python SDK, TypeScript SDK, `polymarket-apis` (PyPI)

> **Hinweis:** Rechtliche Lage fuer Zugang aus Deutschland vorab pruefen.

---

## Markt-Mechanik

```
Contract-Preis = Implizite Wahrscheinlichkeit des Marktes

Preis $0.30 -> Markt glaubt: 30% Wahrscheinlichkeit
Preis $0.05 -> Markt glaubt: 5% Wahrscheinlichkeit

Bei Eintritt:  Contract = $1.00
Bei Nicht-Eintritt: Contract = $0.00

Dein Edge = Deine Schaetzung minus Marktpreis
```

---

# PULSE — BTC 5-Minuten Statistical Edge Bot

## Konzept

Polymarket bietet BTC "Up or Down" Maerkte mit 5-Minuten-Fenstern. Der Markt setzt einen "Price to Beat" und du wettest ob BTC am Ende darueber (UP) oder darunter (DOWN) liegt.

**Das Edge:** Der Markt underpriced Tail Events systematisch. Wenn BTC $83 ueber dem Target liegt, bietet der Markt z.B. 200x auf DOWN — impliziert 0.5% Wahrscheinlichkeit. Statistisch liegt die reale Wahrscheinlichkeit aber bei ~2-3%. Diese Diskrepanz ist der Gewinn.

```
Beispiel:
  BTC aktuell: $68,123
  Price to Beat: $68,040
  Distanz: $83 (0.12%)
  Restzeit: 47 Sekunden
  Quote DOWN: 200x (impliziert 0.5%)
  Reale Wahrscheinlichkeit: ~2.3% (statistisch berechnet)
  -> Edge: ~1.8 Prozentpunkte, Faktor ~4x underpriced
```

## Die Formel

### Stufe 1: Basis (Start)

```python
from scipy.stats import t as student_t
import numpy as np

def calculate_edge(current_price, target_price, time_left_seconds,
                   payout_multiplier, recent_returns):
    """
    Berechnet ob ein BTC 5-Min Contract ein positives EV hat.

    Returns: (real_prob, implied_prob, edge, ev, should_bet)
    """
    distance = abs(current_price - target_price)

    # EWMA Volatilitaet (reagiert auf aktuelle Marktlage)
    lambda_param = 0.94
    variance = np.var(recent_returns)
    for r in recent_returns[-20:]:  # letzte 20 Datenpunkte staerker gewichten
        variance = lambda_param * variance + (1 - lambda_param) * r**2
    volatility = np.sqrt(variance)

    # Sigma-Distanz berechnen
    expected_move = current_price * volatility * np.sqrt(time_left_seconds / 86400)
    sigma_move = distance / expected_move if expected_move > 0 else float('inf')

    # Student-t Verteilung (Fat Tails, df=4)
    # Besser als Normalverteilung fuer BTC
    real_prob = student_t.cdf(-sigma_move, df=4)

    # Implizierte Wahrscheinlichkeit aus Quote
    implied_prob = 1.0 / payout_multiplier

    # Edge und EV
    edge = real_prob - implied_prob
    stake = 1.0  # normalisiert
    ev = (real_prob * (payout_multiplier - stake)) - ((1 - real_prob) * stake)

    # Bet-Entscheidung
    min_edge = 0.01  # 1 Prozentpunkt Minimum
    should_bet = edge > min_edge and ev > 0

    return real_prob, implied_prob, edge, ev, should_bet
```

### Stufe 2: Dynamische Volatilitaet (Woche 1-2)

EWMA Volatilitaet statt fixem Fenster — reagiert auf Volatilitaets-Cluster:

```
Ruhige Phase:  Volatilitaet = 1.5%/Tag
  -> Drop von $83 ist ~3 Sigma -> unwahrscheinlich

Nach grossem Move (vor 30 Min war -2% Drop):
  Volatilitaet = 4.5%/Tag
  -> Drop von $83 ist ~1 Sigma -> ziemlich wahrscheinlich
  -> Quote hat sich kaum geaendert
  -> HIER ist das Edge am groessten
```

### Stufe 3: Microstructure Features (Monat 2-3)

Zusaetzliche Inputs:
- **Orderbuch-Tiefe:** Duennes Bid-Orderbuch -> Drop wahrscheinlicher
- **Funding Rate:** Hohe positive Rate -> Markt overleveraged long -> Liquidierungskaskade moeglich
- **Recent Large Trades:** Grosser Sell auf Binance -> Momentum nach unten

### Stufe 4: Selbstlernend (Monat 3+)

Nach ~1.300+ Datenpunkten (2 Wochen a 96 Wetten/Tag):
- **Parameter-Kalibrierung:** Formel sagt 2.3%, tatsaechliche Gewinnrate 3.1% -> adjustieren
- **Muster-Erkennung:** Tageszeit-Effekte, Streak-Patterns, Volatilitaets-Regime
- **Feature Importance:** ML-Modell (Logistic Regression) gewichtet Inputs automatisch

```
Feature Importance nach 5.000 Wetten (Beispiel):
  1. EWMA Volatilitaet (letzte 5 Min):    32%
  2. Distance/Target Ratio:                28%
  3. Orderbuch-Imbalance:                  18%
  4. Time of Day:                          12%
  5. Restzeit im Contract:                 10%
```

## PULSE Bot-Logik

```
Alle paar Sekunden:
|
|-- BTC-Preis und Target lesen (Polymarket API)
|-- Aktuelle EWMA-Volatilitaet berechnen
|-- Reale Wahrscheinlichkeit berechnen (Student-t, Fat Tails)
|-- Quote auslesen (z.B. 200x)
|-- Implizierte Wahrscheinlichkeit berechnen (1/200 = 0.5%)
|-- Edge = Real - Impliziert
|
|-- IF edge > MIN_EDGE AND ev > 0:
|   |-- Wette platzieren (fester Betrag)
|-- ELSE:
|   |-- Diesen Contract skippen
|
|-- Logging: Wette, Quote, Berechnung, Ergebnis
```

## PULSE Simulation

```
Phase 1: Dry Run (Woche 1-2)
  - Bot laeuft, loggt was er WUERDE setzen
  - Setzt aber nicht
  - Tracked: Haette die Wette gewonnen?
  - Ziel: ~1.300 Datenpunkte sammeln
  - Startkapital: 1.000 EUR (simuliert)

Phase 2: Parameter Tuning (Woche 2-3)
  - Optimalen MIN_EDGE Threshold finden
  - Volatilitaets-Fenster kalibrieren
  - Student-t df Parameter optimieren
  - Fee-Impact validieren

Phase 3: Live (nach positiver Simulation)
  - Gleicher Code, Orders werden platziert
  - Start mit Minimum-Einsatz ($1-2 pro Wette)
  - Langsam hochskalieren wenn Ergebnisse stimmen
```

## PULSE Volumen-Rechnung

```
1 Contract alle 5 Minuten, 8 Stunden/Tag:
  -> 96 Opportunities pro Tag
  -> Davon ~30-50% mit positivem Edge (geschaetzt)
  -> ~30-48 Wetten pro Tag
  -> ~840-1.344 Wetten pro Monat

Bei $5 Einsatz und ~2.3% realer Wahrscheinlichkeit:
  Theoretischer EV pro Wette: ~$18 (bei 200x Quote)
  Aber: Quote variiert stark (20x bis 500x)
  Realistische Schaetzung: Validierung durch Simulation noetig
```

---

# CORE — Langfrist AI-Edge Engine

## Edge Engine — Die 5 Analyse-Module

### Modul 1: Basisraten-Recherche

Das maechtigste Werkzeug. Statt aus dem Bauch zu schaetzen, historische Daten nutzen.

**Methode:**
1. Frage identifizieren ("Wird die EZB im Juli die Zinsen senken?")
2. Vergleichbare historische Situationen finden (aehnliche Inflation, GDP, geopolitische Lage)
3. Basisrate berechnen (in 14 von 18 vergleichbaren Faellen: Ja -> 78%)
4. Adjustieren fuer aktuelle Abweichungen (z.B. andere geopolitische Lage -> 55%)
5. Delta zum Marktpreis berechnen

**Warum es funktioniert:** Die meisten Marktteilnehmer machen keine Basisraten-Recherche. Sie schaetzen aus dem Bauch, beeinflusst von Recency Bias, Verfuegbarkeitsheuristik und Medien-Narrativen.

### Modul 2: Multi-Source Ensemble

Mehrere unabhaengige Schaetzungen kombinieren statt einer einzelnen Quelle zu vertrauen.

**Quellen pro Frage:**
- Historische Basisrate
- Experten-Konsens (Aggregation aus Interviews, Papers, Statements)
- Quantitative Modelle (wo verfuegbar)
- Eigene qualitative Einschaetzung
- "Bekannte Optimisten/Pessimisten" mit historischem Discount

**Methode:** Gewichteter Durchschnitt aus allen Quellen. Gewichtung basiert auf Track Record und Relevanz der Quelle fuer die spezifische Frage.

### Modul 3: Zeitliche Informationsvorteile

Prediction Markets reagieren oft langsamer als andere Maerkte auf neue Informationen.

**Opportunities:**
- Neue Wirtschaftsdaten/Studien erscheinen -> Implikation fuer Contract, aber Preis hat sich noch nicht bewegt
- Nischenthemen — Information existiert, niemand hat sie eingepreist
- Breaking News: Implikation fuer einen Contract verstehen bevor die Masse es tut

**Umsetzung im Tool:**
- News-Feed mit automatischer Zuordnung zu offenen Contracts
- Alert: "Neue Information relevant fuer Contract X, Marktpreis hat sich noch nicht bewegt"

### Modul 4: Cross-Market Korrelationen

Verschiedene Events haengen zusammen, aber der Markt behandelt sie oft isoliert.

**Beispiel:**
```
Contract A: "Fed senkt Zinsen im Juni?"         -> Marktpreis: $0.25
Contract B: "US Arbeitslosigkeit ueber 5% im Mai?" -> Marktpreis: $0.60

Wenn B eintritt, steigt die Wahrscheinlichkeit von A massiv.
Aber A hat das noch nicht eingepreist.
-> Kauf A, weil B wahrscheinlich ist und A dann nachzieht.
```

**Umsetzung im Tool:**
- Korrelations-Matrix zwischen aktiven Contracts
- Alert: "Contract X hat sich bewegt, korrelierter Contract Y noch nicht"

### Modul 5: Kalibrierungs-Tracking

Eigene systematische Verzerrungen erkennen und korrigieren.

**Methode:**
- Jede Schaetzung wird gespeichert mit Datum und Begruendung
- Nach Outcome: War die Schaetzung kalibriert?
- Ueber Zeit: Wenn du 70% sagst, tritt es tatsaechlich in ~70% der Faelle ein?

**Automatische Korrektur:**
```
Kalibrierung nach 100 Wetten:
- Du sagst 80%+ -> Tatsaechlich: 83% <- gut kalibriert
- Du sagst 50-60% -> Tatsaechlich: 71% <- zu konservativ -> System adjustiert +11%
- Du sagst <30% -> Tatsaechlich: 35% <- leicht pessimistisch -> System adjustiert +5%
```

## Bias-Detektoren

### Probability Mispricing Detector
- Tail-Risk Underpricing: Events die "unwahrscheinlich" scheinen, aber signifikant wahrscheinlicher sind als eingepreist
- Binary Event Scanner: Ja/Nein-Events wo der Markt das Outcome-Risiko falsch bewertet

### Herd Behavior Tracker
- Uebertriebene Panik nach negativen Nachrichten
- Uebertriebene Euphorie bei Hype-Themen
- Sentiment-vs-Daten Gap Score

### Recency Bias Exploiter
- Markt ueberbewertet das was gerade passiert ist
- Mean Reversion Scanner: Extreme Abweichungen vom statistischen Mittel

### Neglect Bias
- Maerkte mit niedriger Liquiditaet und wenig Teilnehmern -> wahrscheinlicher falsch bewertet
- Neue Maerkte die gerade erst eroeffnet wurden -> noch wenig Recherche eingeflossen

## CORE Trading-Regeln (Hard Rules)

### Regel 1: Minimum Delta
Nur handeln wenn Delta zum Marktpreis mindestens 10-15 Prozentpunkte betraegt.

### Regel 2: Rand-Contracts bevorzugen
Contracts nahe $0.01-0.15 und $0.85-0.99 bieten die beste Asymmetrie.

### Regel 3: Position Sizing
Maximum 2-5% des Gesamtkapitals pro einzelne Wette. Kelly Criterion als Richtlinie.

### Regel 4: Limit Orders
Immer Limit Orders nutzen, nie Market Orders.

### Regel 5: Fruehzeitig einsteigen
Neue Maerkte sind am ineffizientesten.

### Regel 6: Exit vor Outcome erlaubt
Bei $0.10 kaufen, bei $0.40 verkaufen wenn sich der Markt in deine Richtung bewegt.

## CORE Datenqualitaets-Bewertung

Jedes Signal bekommt neben der Staerke eine Bewertung der zugrunde liegenden Datenqualitaet.

```
Signal-Staerke:   STARK (Delta: 25 Prozentpunkte)
Daten-Qualitaet:  HOCH (3 unabhaengige Quellen, historische Basisrate verfuegbar)
Gesamt-Konfidenz: HOCH

vs.

Signal-Staerke:   STARK (Delta: 20 Prozentpunkte)
Daten-Qualitaet:  NIEDRIG (nur 1 Quelle, keine Basisrate, Nischenthema)
Gesamt-Konfidenz: MITTEL -- kleinere Position
```

---

## UI-Konzept

### PULSE Dashboard

```
+-- BLACK DELTA / PULSE -- BTC 5-Min Bot -------------------+
|                                                            |
|  Status: RUNNING          Modus: SIMULATION                |
|  Kapital: 1.000 EUR       Aktuell: 1.142 EUR (+14.2%)     |
|                                                            |
|  -- Aktueller Contract ---------------------------------- |
|  BTC Up or Down | 14:35-14:40 UTC                          |
|  Target: $68,040  |  BTC: $68,123  |  Distanz: +$83       |
|  DOWN Quote: 200x | Impl. Prob: 0.50%                     |
|  EWMA Vol: 2.8%   | Real Prob: 2.31%                      |
|  Edge: +1.81%     | EV: +$17.58                           |
|  -> BET PLACED: $5 on DOWN                                |
|                                                            |
|  -- Heute ------------------------------------------------|
|  Wetten: 34/96  |  Gewonnen: 1  |  Verloren: 33           |
|  P&L heute: +$142                                          |
|  Aktuelle Trefferrate: 2.9% (Erwartet: 2.3%)             |
|                                                            |
|  -- Letzte 7 Tage ---------------------------------------  |
|  Wetten: 238  |  Gewonnen: 6  |  Verloren: 232            |
|  P&L: +$892   |  ROI: +12.4%                              |
|  Avg Edge bei Bet: 1.9%                                    |
|  Formel-Kalibrierung: leicht konservativ (+0.4%)           |
+------------------------------------------------------------+
```

### CORE Dashboard

```
+-- BLACK DELTA / CORE -- Edge Scanner ---------------------+
|                                                            |
|  Filter: [Alle Kategorien v]  [Min Delta: 10% v]  [Suche] |
|                                                            |
|  -- Top Opportunities ----------------------------------- |
|                                                            |
|  "EZB senkt Zinsen Juli 2026"           Politik/Wirtschaft |
|  Marktpreis: $0.20 (20%)                                  |
|  Basisrate (hist.): 55%                                    |
|  Datenqualitaet: HOCH                                      |
|  [Analysieren]                                             |
|                                                            |
|  -- Detail-Ansicht -------------------------------------- |
|  Ensemble-Analyse:                                         |
|    Basisrate (18 Situationen):              55%            |
|    Experten-Konsens (5 Oekonomen):          48%            |
|    Quantitatives Modell:                    52%            |
|    Gewichteter Durchschnitt:                52%            |
|                                                            |
|  Delta: 52% vs 20% = +32pp              STARK              |
|  EV: +$0.32 pro Contract                                  |
|  Empfehlung: KAUFEN Yes @ $0.20                            |
|  Position Size (bei 1000 EUR): 30 EUR (3%)                |
|                                                            |
|  [Zur Simulation hinzufuegen]  [Watchlist]  [Verwerfen]   |
+------------------------------------------------------------+
```

---

## Adaptives Level-System

### Level 1 -- Anfaenger
- Einfache Ansicht: Empfehlung + Konfidenz-Ampel (Gruen/Gelb/Rot)
- Erklaerungen bei jedem Schritt
- Nur Simulation, kein Live-Trading
- Maximal 3 offene Positionen gleichzeitig

### Level 2 -- Fortgeschritten (nach ~50 simulierten Wetten)
- Ensemble-Details sichtbar
- Eigene Schaetzungen eingeben und gegen das System vergleichen
- Cross-Market Korrelationen freigeschaltet
- Position Sizing Empfehlungen

### Level 3 -- Erfahren (nach ~200 Wetten + positive Performance)
- Volle Kontrolle: Eigene Gewichtungen, eigene Quellen
- Kalibrierungs-Adjustierung aktiv
- Alerts und News-Feed
- Alle Bias-Detektoren mit Details

---

## Betriebskosten (monatlich)

### PULSE (Bot-only, kein AI)

| Komponente | Kosten |
|---|---|
| Server (VPS/Railway) | $5-10 |
| Polymarket Crypto Fees (~1.5%) | $5-15 |
| Polygon Gas | <$1 |
| **Gesamt PULSE** | **~$10-25** |

### CORE (AI-gestuetzt)

| Komponente | Kosten |
|---|---|
| Claude API Haiku (News-Scanning, EV) | $10-15 |
| Claude API Sonnet (Basisraten, Korrelationen) | $15-20 |
| Prompt Caching + Batch API (bis -95%) | Reduziert auf $5-15 |
| News API (Premium, optional) | $0-20 |
| **Gesamt CORE** | **~$5-55** |

### Gesamtkosten

```
Minimal (PULSE only):                ~$10-25/Monat
Standard (PULSE + CORE Basis):       ~$30-50/Monat
Maximal (alles Premium):             ~$60-80/Monat
```

---

## Phasenplan

### Phase 0: PULSE MVP (jetzt)
- Polymarket API Anbindung (Auth, Marktdaten, Orders)
- BTC Preis-Feed
- Formel implementieren (Stufe 1+2: Student-t + EWMA)
- Simulations-Modus (Dry Run)
- Logging und Ergebnis-Tracking
- **Startkapital: 1.000 EUR (simuliert)**
- **Dauer: 2 Wochen Simulation**

### Phase 1: PULSE Optimierung
- Formel-Parameter tuning basierend auf Simulationsdaten
- Stufe 3 Features (Microstructure) wenn Daten verfuegbar
- Live-Trading mit Minimum-Einsatz
- Hochskalierung bei positiver Performance

### Phase 2: CORE Foundation
- Shared Infrastruktur von PULSE wiederverwenden (Auth, Orders, Tracking)
- Edge Scanner UI
- EV-Rechner
- Simulations-Engine fuer Langfrist-Contracts

### Phase 3: CORE Intelligence
- Claude API Integration fuer Basisraten-Recherche
- Ensemble-System
- Datenqualitaets-Bewertung
- Kalibrierungs-Tracking

### Phase 4: CORE Automation
- News-Feed mit automatischer Contract-Zuordnung
- Cross-Market Korrelations-Erkennung
- Bias-Detektoren
- Alert-System

### Phase 5: Selbstlernend
- PULSE: ML-basierte Formel-Optimierung
- CORE: Automatische Basisraten-Recherche
- Adaptives Level-System
- Performance Analytics ueber beide Module

---

## Tech-Stack (geplant)

| Komponente | Technologie | Begruendung |
|---|---|---|
| Sprache | Python | Polymarket SDK, scipy/numpy, ML Libraries |
| BTC Preis-Feed | Polymarket API + Backup (Binance WS) | Echtzeit, redundant |
| Datenbank | SQLite (Start) -> PostgreSQL (spaeter) | Einfach, skalierbar |
| Bot Runtime | asyncio Event Loop | Mehrfach pro Sekunde berechnen |
| UI (spaeter) | Web Dashboard (FastAPI + HTMX oder SvelteKit) | Leichtgewichtig |
| Hosting | VPS oder Railway | 24/7 Betrieb fuer Bot |

---

## Offene Fragen

- [ ] Rechtliche Lage Polymarket-Nutzung aus Deutschland klaeren
- [ ] Polymarket API: Exakte Endpoints fuer BTC 5-Min Markets identifizieren
- [ ] Polymarket API: Auth-Flow testen (Wallet, API Keys, HMAC)
- [ ] BTC Volatilitaets-Daten: Quelle fuer historische Minutendaten fuer Backtesting
- [ ] Student-t Freiheitsgrad: Aus historischen BTC-Daten berechnen
- [ ] Hosting: VPS Anbieter evaluieren (Latenz zu Polymarket/Polygon)
