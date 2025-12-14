# Phase 2: Technische Recherche - HVAC Energieeffizienz-Analyse
## Wiener Bürogebäude 2.800 m² (A-1150)

**Datum**: 31. Oktober 2025
**Projekt**: HVAC Energieeffizienz-Analyse Wien
**Phase**: 2 - Literaturkontext und technische Benchmarks
**Gebäude**: Bürogebäude 2.800 m², Wien 1150, Österreich

---

## Zusammenfassung

Dieser Bericht liefert den technischen Literaturkontext für die Analyse eines hybriden Kühl-/Heizsystems mit TURBOCOR-Kaltwassersätzen, TABS-Betonkernaktivierung und Fan-Coil-Einheiten. Die Recherche fokussiert auf technische Benchmarks, Normen, Produktspezifikationen und Best Practices aus der Literatur - **ohne projektspezifische Berechnungen** (diese erfolgen in Phase 7).

---

## 1. TECHNISCHE NORMEN UND STANDARDS

### 1.1 Österreichische Normen (ÖNORM)

#### ÖNORM H 5155 (2024) - Wärmedämmung von betriebstechnischen Anlagen
- **Geltungsbereich**: Alle gebäudetechnischen Anlagen nach ÖNORM B 2110
- **Anwendung**: Heizung, Warmwasser, Solarsysteme, Kaltwasser, Kühlung, Lüftungskanäle
- **Gebäudetypen**: Wohn- und Nichtwohngebäude
- **Aktuelle Version**: ÖNORM H 5155:2024-11-01 (ersetzt Version 2024-06-15)
- **Bedeutung**: Definition erforderlicher Charakteristika der Wärmedämmung für Kälteanlagen

#### ÖNORM B 8135 (1983) - Wärmeverlustberechnung
- **Status**: ZURÜCKGEZOGEN, ersetzt durch ÖNORM H 7500-3
- **Ursprünglicher Zweck**: Vereinfachte Berechnung des zeitbezogenen Wärmeverlustes (Heizlast) von Gebäuden

#### ÖNORM H 5160-1 (2023) - Flächenheizung/-kühlung
- **Geltungsbereich**: Planungs- und Installationsbedingungen für Flächenheizungs- und -kühlsysteme
- **Abgrenzung**: Heiz-/Kühlelemente NICHT in zentraler Betonkerndecke eingebettet (im Gegensatz zu TABS)

#### ÖNORM H 5195-1 (2024) - Raumheizung
- **Aktuelle Version**: 2024-01-01
- **Anwendung**: Allgemeine Anforderungen an Raumheizungssysteme

#### Hinweis zu ÖNORM H 5156
- In der Recherche **keine spezifische Norm ÖNORM H 5156** gefunden
- Verwandte Normen: H 5155, H 5160, H 5195-Serie

### 1.2 Deutsche VDI-Richtlinien

#### VDI 2067 - Wirtschaftlichkeit gebäudetechnischer Anlagen
- **Teil 1**: Grundlagen und Wirtschaftlichkeitsberechnung
- **Teil 10**: Energieaufwand für Heizen, Kühlen, Be- und Entfeuchtung
  - Beschreibt Berechnung des Energiebedarfs von Gebäuden
  - Anwendbar für alle Gebäudetypen
- **Teil 21**: Energieaufwand der Nutzenübergabe - HLK-Systeme

#### VDI 3803 - Lufttechnische Anlagen
- **Teil 1**: Anforderungen an HLK-Systeme für energieeffizienten und hygienisch einwandfreien Betrieb
  - Anwendung: Planung und Ausführung von RLT-Anlagen
  - Umfasst: Hygiene, Brandschutz, Gebäudeautomation
- **Teil 2**: Dezentrale Lüftungsgeräte - Energieeffiziente und kostenoptimierte Planung
- **Teil 5**: Wärmerückgewinnung aus Abluft
  - Wichtiger Schritt zur Reduzierung des Primärenergiebedarfs
  - Aussagen zu Eignung, Wirtschaftlichkeit, CO₂-Emissionen

**Hinweis**: VDI 6003 existiert in der Recherche nicht als Standard-VDI-Richtlinie.

### 1.3 EU.BAC System-Zertifizierung und ISO 52120-1

#### EN ISO 52120-1:2022 (ersetzt EN 15232-1:2017)
- **Titel**: "Energy performance of buildings – Contribution of building automation, controls and building management – Part 1: General framework and procedures"
- **Implementierung**: Spätestens 30. September 2022 auf nationaler Ebene
- **Zweck**: Erste international harmonisierte Methodik zur Bewertung des Beitrags von Gebäudeautomation (BAC) zur Energieeffizienz

#### BAC-Faktor-Methode (Method 2, Clause 7)
- **Ziel**: Schnelle grobe Abschätzung des Einflusses von Gebäudeautomation auf Energieperformance
- **Basis**: Korrelation von gemessenem/berechnetem Energieverbrauch mit BAC-Effizienzklassifikation
- **Anwendung**: Typische Gebäudetypen und Nutzungsprofile

#### Energieeffizienz-Potenzial
- **Einsparungen**: Bis zu 40% Energieeinsparung möglich
- **ROI**: Unter 3 Jahre
- **Methodik**: Drei Ansätze verfügbar
  1. Mindestanforderungen definieren
  2. Faktorbasierte Methode (erste Abschätzung)
  3. Detaillierte Methoden (spezifisches Gebäude)

#### Neue Funktionen in ISO 52120 vs. EN 15232
- **Hydronic Balancing**: Neue Funktionen 1.4a und 3.4a für Heizungs-/Kühlverteilung
- **Luftqualitätsbasierte Regelung**: Funktion 4.1.3 für raumweise Zuluftmengensteuerung (CO₂, VOC)
- **Tabelle 5**: Erweiterte Beschreibung der Steuerungsfunktionen (vormals Tabelle 4 in EN 15232)

#### Limitierungen
- Neuere Forschung zeigt: Faktoren in EN 52120-1 sind **nicht ausreichend genau** für präzise Vorhersage von BAC-Energieeinsparungen
- Empfehlung: Faktoren als Orientierung nutzen, detaillierte Analysen für konkrete Projekte durchführen

---

## 2. BENCHMARKS FÜR BÜROGEBÄUDE - KÄLTEVERBRAUCH

### 2.1 Österreich-spezifische Benchmarks

#### Mueller et al. (2014) - Österreich gesamt
- **Kühlbedarf**: 36,9 kWh/m²a (Elektrizität für Kühlung)
- **Gekühlte Bruttogeschossfläche**: 25,6 Mio. m² österreichweit
- **Prognose 2050**: 33-55% höherer Bedarf durch Klimawandel

#### Peharz et al. (2018) - Wien spezifisch
- **Bottom-up Studie**: Durchschnittlicher Netto-Kühlbedarf 2025 = 36,8 kWh/m²
- **Bürogebäude mit Kompressionskälte** (60 W/m² Kühllast): 33,3 kWh/m²a

### 2.2 Europäischer Kontext

#### Vergleichswerte Europa
- **Bürokühlung typisch**: 40,5 kWh/m²
- **Industry Benchmark "Typical Consumption"**: 30-40 kWh/m²
- **Industry Benchmark "Good Practice"**: 15-20 kWh/m²

#### Gesamtenergieverbrauch Bürogebäude
- **Durchschnitt**: 166 kWh/m²a (gesamter Energieverbrauch)
- **Anteil Kühlung**: Je nach System und Klimazone 20-25% des Gesamtverbrauchs

### 2.3 Zusammenfassung Benchmarks

| Kategorie | Wert | Quelle |
|-----------|------|--------|
| **Österreich - Typisch** | 33-37 kWh/m²a | Mueller et al., Peharz et al. |
| **Europa - Typisch** | 30-40 kWh/m²a | Industry Benchmarks |
| **Europa - Good Practice** | 15-20 kWh/m²a | Industry Benchmarks |
| **Prognose Wien 2025** | 36,8 kWh/m²a | Peharz et al. (Bottom-up) |

**Interpretation**: Österreichische Werte liegen im oberen Bereich europäischer "Typical Consumption", Verbesserungspotenzial in Richtung "Good Practice" (15-20 kWh/m²a) = **40-50% Einsparung** möglich.

---

## 3. TURBOCOR MAGNETLAGER-KÄLTETECHNIK

### 3.1 Technologieübersicht

#### Magnetlager-Technologie
- **Hersteller**: Danfoss Turbocor®
- **Funktionsprinzip**: Hauptwellen rotieren mit hoher Drehzahl (48.000 RPM) **ohne mechanischen Kontakt**
- **Ölfrei**: Keine Schmierölverluste, keine Beeinträchtigung des Wärmeübergangs
- **Drehzahlvariabel**: VFD-gesteuert, Leistungsregelung 15-100% der Kapazität
- **Gewicht**: 125 kg (TURBOCOR) vs. 600 kg (typischer Schrauben-/Hubkolbenverdichter)

### 3.2 Leistungsbereiche und Kältemittel R134a

#### Kapazitätsspektrum
- **Danfoss Turbocor TT-Serie**: 60-200 Tonnen / 200-700 kW (R134a)
- **Danfoss Turbocor VTX**: Bis 450 Tonnen / 1.600 kW (R134a)
- **Luftgekühlte Einheiten**:
  - Single Circuit: 200-945 kW (TCC), 200-1.000 kW (TCF)
  - Dual Circuit: 200-1.775 kW (TCC), 200-1.830 kW (TCF)

#### R134a Eigenschaften
- **GWP**: 1.300 (Global Warming Potential)
- **ODP**: 0 (Ozone Depletion Potential - umweltfreundlich bezüglich Ozonschicht)
- **Klassifizierung**: Sauberes, umweltfreundliches Kältemittel für HLK-Anwendungen

### 3.3 Effizienz und COP-Werte

#### Energieeffizienz-Potenzial
- **Energieeinsparung**: Bis zu 50% gegenüber traditionellen Schrauben-/Hubkolbenverdichtern
- **Teillast-Effizienz**: Besonders hoch bei Teillast (wo Kältemaschinen meiste Zeit operieren)
- **ESEER**: Bis zu 6,23 (European Seasonal Energy Efficiency Ratio) bei mechanischer Kühlung
- **SEER**: Hohe Werte im Bereich 6-9

#### COP-Werte aus Literatur
- **Durchschnittlicher COP**: 8-10 unter verschiedenen Lastbedingungen
- **Full Load COP**: Bis zu 7,0 unter GB-Standardbedingungen
- **IPLV**: Bis zu 9,5 (Integrated Part Load Value)
- **Optimaler Betriebspunkt**: PLR (Part Load Ratio) 0,71-0,84 je nach Außentemperatur

#### Vergleich mit konventionellen Systemen
- **Energieeinsparung**: 10-40% gegenüber traditioneller Zentrifugalkälte
- **Navy Testing**: Durchschnittlich 49% Leistungseinsparung über 3 Fallstudien
- **Turbomiser Chillers**: Bis zu 50% Energiekosteneinsparung

### 3.4 Umgebungstemperatur und Kondensatorleistung

#### Temperaturabhängigkeit
- **Höchster COP**: Tritt bei PLR 0,71-0,84 auf (nicht Volllast!)
- **Kondensatorwasser-Eintritt**: Magnetlager-Kühler übertrifft ölgeschmierte erst unter ca. 18°C (65°F)
- **Größte Einsparungen**: Bei kältestem möglichem Kondensatorwasser
- **Optimierung**: Kondensatorkreislauf für niedrigste Wassertemperatur betreiben

#### Ambient Temperature Range
- **Typischer Bereich**: -5°C bis +43°C (aus Projektkontext)
- **COP-Kurven**: Variieren über Außentemperaturbereich
- **Optimaler Bereich**: 30-50°C Ambient, PLR 0,68-0,76 für optimierten COP

### 3.5 Teillastverhalten und N+1 Redundanz

#### N+1 Redundanzkonfiguration
- **Definition**: N = erforderliche Kühlkreise + 1 Standby-Einheit
- **Industriestandard**: N+1 ist Minimum für moderne Rechenzentren und kritische Anwendungen
- **Rotationsstrategie**: Redundante Einheit regelmäßig einbinden → gleichmäßige Laufzeit, Funktionsprüfung

#### Teillast-Strategien
- **Typische Performance-Peaks**: Bei 40%, 60% oder 70-75% Kapazität (nicht Volllast!)
- **Alle Einheiten Teillast**: Häufig laufen alle Einheiten gemeinsam bei Teillast (inkl. redundante)
  - Reduziert Komponentenbelastung
  - Sanftere Ausfallübergänge
- **Optimierte Lastverteilung**: 22-33% bessere Performance als konventionelle Strategien
- **Load-Shedding**: Bei Teillast Einheiten abschalten, verbleibende bei optimaler PLR betreiben

#### Effizienz-Überlegungen 3× TURBOCOR (N+1)
- **1 von 3 aktiv**: PLR ca. 0,33 pro Einheit (suboptimal)
- **2 von 3 aktiv**: PLR ca. 0,50 pro Einheit (näher am Optimum)
- **3 von 3 aktiv**: PLR ca. 0,67 pro Einheit (im optimalen Bereich 0,71-0,84)
- **Empfehlung aus Literatur**: Bei Teillast alle 3 Einheiten laufen lassen für besten Aggregate COP

### 3.6 Wartung und Lebensdauer

#### Vorteile Magnetlager-Technologie
- **Verschleißfrei**: Keine mechanische Reibung → keine Leistungsdegradation über Lebensdauer
- **Geräuscharm**: Außergewöhnlich leise, bis zu 67 dB auf 5 Meter Entfernung
- **Ölfreiheit**: Kein Ölwechsel erforderlich, keine Ölverunreinigung der Wärmetauscher
- **Kompakt**: Erheblich kleiner und leichter als konventionelle Verdichter

#### Typische Lebensdauer (aus Literatur)
- **Magnetlager-Verdichter**: 15-20 Jahre (Annahme aus Projektkontext, Literatur bestätigt lange Lebensdauer)
- **Wartungsintervalle**: Deutlich verlängert gegenüber ölgeschmierten Systemen
- **Ausfallsicherheit**: Hohe Zuverlässigkeit durch Wegfall mechanischer Lagerung

### 3.7 Alternative Kältemittel - Retrofit-Optionen

#### R513A (GWP 572-573)
- **GWP-Reduktion**: 56% gegenüber R134a
- **Retrofit-Eignung**: ✓ Minimale Systemänderungen, meist nur Thermostatisches Expansionsventil anpassen
- **Sicherheitsklasse**: A1 (nicht brennbar, geringe Toxizität)
- **Effizienz**:
  - Kontomaris et al.: Ähnliche Effizienz, 0,6% höherer Energieverbrauch
  - Velasco et al.: Bis zu 24% niedrigerer EER gegenüber R134a
  - Massenstrom ca. 15% höher → erhöhter Druckverlust
- **Performance**: Leicht reduzierte isentrope und mechanische Effizienz (~8%)

#### R1234ze (GWP 7)
- **GWP-Reduktion**: 99,5% gegenüber R134a (GWP 7 vs. 1.300)
- **Effizienz**: Sehr effizient, COP bis 5,8, SEER bis 9,1
- **Sicherheitsklasse**: A2L (leicht brennbar nach ASHRAE 34 / ISO 817)
- **Retrofit-Überlegungen**:
  - ✓ Möglich, aber PED-Klassifizierung prüfen
  - ✓ EN 378 Füllmengen-Beschränkungen beachten
  - ✓ Risikoanalyse für brennbare Zonen erforderlich
  - ⚠ Größere Baugröße als R134a bei gleicher Kapazität

#### Retrofit-Empfehlung aus Literatur
- **Kurzfristig**: R513A (einfacher Retrofit, nicht brennbar, moderate GWP-Reduktion)
- **Langfristig**: R1234ze (beste Umweltbilanz, höhere Effizienz, A2L-Sicherheitsaspekte beachten)
- **Beide**: Verbesserung gegenüber R134a bezüglich Umweltauswirkungen

---

## 4. PRIMÄRKREIS-HYDRAULIK UND PUMPEN

### 4.1 Primär-Sekundär-Hydraulik

#### Systemkonfiguration
- **Primärkreis**: Konstantvolumenstrom zwischen Kältemaschine und Pufferspeicher
- **Sekundärkreis**: Variabel, bedarfsgeführt zu Verbrauchern (TABS, Fan-Coils)
- **Hydraulische Weiche**: Pufferspeicher oder 4-Port-Design als "Bridge Common Pipe"
- **Vorteile**: Entkopplung von Erzeuger und Verbraucher, Schutz der Kältemaschine vor Durchfluss-Schwankungen

#### Druckverhältnisse
- **Typisch**: 2-5 bar (aus Projektkontext)
- **Primärkreis**: Höherer Druck (Kältemaschinen-Anforderung)
- **Sekundärkreis**: Angepasst an Verbraucher (TABS: niedrig, Fan-Coils: moderat)

### 4.2 Pufferspeicher-Dimensionierung

#### Dimensierungsrichtwerte
- **Typisch**: 5-11 Gallonen pro Tonne (19-42 Liter/kW)
- **Hersteller-Empfehlung**: 2-6 Gallonen/Tonne (7,6-23 Liter/kW) für nominale Kühlung
- **Hohe Temperaturgenauigkeit**: 6-10 Gallonen/Tonne (23-38 Liter/kW)
- **Typische HVAC-Systeme**: 3-6 Gallonen/Tonne (11-23 Liter/kW)

#### Europäische Dimensierung
- **Faustformel**: Kältemaschinen-kW × 4 Liter = erforderliche Systemkapazität
- **Formel**: Erforderliche Systemkapazität (L) - Tatsächliches Systemvolumen (L) = Pufferspeicher (L)
- **Systemvolumen**: Wasser in Kältemaschine + Rohrleitungen + Wärmetauscher

#### Zweck und Funktion
- **Anti-Taktung**: Ausreichendes Fluidvolumen verhindern Kurzzeitzyklen der Kältemaschine
- **Niedriglast-Betrieb**: Besonders wichtig bei Teillast (z.B. nach Büroschluss)
- **Vertikale Ausrichtung**: Wichtig für korrekte Schichtung (Stratification)

#### Dimensionierung für 2.800 m² Büro
- **Annahme Kühllast**: 60 W/m² → 168 kW Kältebedarf
- **Typischer Pufferspeicher**: 672-1.008 Liter (4-6 Liter/kW)
- **Hinweis**: Hersteller-Spezifikationen haben Priorität!

### 4.3 Pumpen-Spezifikationen

#### Wilo Stratos-Serie (VFD-Pumpen)

**Technologie**:
- **EC-Motor**: Höchst-effizienter Motor-Antriebskombination am Markt (bis 10 HP, Motorwirkungsgrad bis 96%)
- **"Green Button" Technologie**: Einfache Bedienung, LED-Display
- **Automatische Leistungsanpassung**: Pumpe passt sich selbständig an wechselnde Anforderungen an

**Energieeinsparpotenzial**:
- **Über 75% Einsparung**: Zwei führende Hersteller (Wilo/Grundfos) dokumentieren >75% bei Austausch von Standardumwälzpumpen
- **80% Einsparung**: Grundfos-Studien für hydronikische Heizungssysteme
- **Fallstudie**: 76% Energiereduktion in US-Schulbezirk → 2.609 USD/Jahr Einsparung pro Einrichtung

**Modelle**:
- **Wilo Stratos**: Gewerbliche Anwendungen
- **Wilo Stratos ECO**: Wohnanwendungen
- **Wilo Stratos GIGA**: Großanlagen

**Kosten**:
- **Investition**: 2-2,5× Kosten einer Standardumwälzpumpe
- **ROI**: Normalerweise 3-4 Jahre durch Energieeinsparung

#### KSB Rio-Eco-Serie (VFD-Pumpen)

**Technologie**:
- **EC-Motor mit integrierter Drehzahlregelung**: Glandless Circulator mit Flanschanschluss
- **Betriebsdruck**: 6/10 bar (Standard) oder 6 bar / 10 bar / 16 bar (Spezialversionen)
- **Temperaturbereich**: -10°C bis +110°C (Heizwasser und Kühlmittel)

**Größen und Anwendungen**:
- **Verfügbare Größen**: DN 32-65 (32-120, 40-120, 50-90, 50-120, 65-90 Modelle)
- **Anwendungen**: Warmwasser-Heizung, Klimaanlagen, geschlossene Kühlkreise, industrielle Zirkulationssysteme

**Regelungsmodi**:
- **Δp-c**: Konstanter Differenzdruck
- **Δp-v**: Variabler Differenzdruck (automatische Anpassung an Rohreibung)

**Energieeffizienz**:
- **VFD-Einsparungen**: Bis zu 90% in Klima-/Heizanwendungen vs. Pumpen mit fester Drehzahl
- **KSB PumpDrive**: Zusätzliche Einsparungen durch dynamische Druckkompensation
- **ROI**: Austausch zahlt sich üblicherweise in 3-4 Jahren aus

**Gebäudeautomation-Integration**:
- **Modbus RTU**: RS485-Busanbindung
- **BACnet MS/TP**: RS485-Busanbindung
- **Bluetooth**: Drahtlose Datenübertragung, Smartphone/Tablet-Bedienung

### 4.4 VFD-Retrofit Pumpen - Allgemeine Betrachtungen

#### Energieeinspar-Mechanismus
- **Kubisches Gesetz**: Energieverbrauch sinkt mit Drehzahl³
  - Beispiel: 20% Drehzahlreduktion → 49% Energieeinsparung
- **Variable Drehmomentkennlinie**: Zentrifugalpumpen/Ventilatoren ideale Kandidaten

#### Systemvorteile
- **Verlängerte Lebensdauer**: Pumpenabdichtungen, weniger Laufrad-Degradation
- **Reduzierte Vibration**: Leiserer Betrieb
- **Reduzierter Systemdruck**: Weniger Belastung auf Komponenten

#### Retrofit-Bewertung
- **Ideal**: Bestehende mechanische Durchfluss-Begrenzung (erhöhter Druckabfall)
- **Analyse erforderlich**: System-/Pumpenkennlinien prüfen, derzeitigen Modulationsmechanismus analysieren
- **Wirtschaftlichkeit**: 20-40% Einsparungen typisch (abhängig von Betriebsprofil)

### 4.5 BELIMO Pump Optimizer / Energy Valve

#### Funktionsprinzip
- **Pressure-Independent Control Valve (PICV)**: Druckunabhängige Durchflussregelung
- **Dynamisches Balancing**: Automatisch, keine manuelle Einregulierung erforderlich
- **Echter Durchfluss-Messung**: Integriertes elektronisches Durchflussmessgerät (nicht approximiert)

#### Energieeffizienz-Funktionen

**Overflow-Eliminierung**:
- Größte Energieeinsparung durch Eliminierung von Overflow durch Spulen/Zweige
- Verhindert "Low Delta-T Syndrome" in Kaltwassersystemen

**Pumpenkopf-Reduktion**:
- Deutlich niedrigerer erforderlicher Pumpenkopf, da kein Überdruck-Kompensation nötig
- Eliminierung "Waste Zone"

**Power Control**:
- Maximale Wärmeübertragungsleistung einstellbar
- Lineare Wärmeübertragung über gesamten Lastbereich

**Zero Leakage**:
- Null-Leckage eliminiert "Ghost Energy Flow"-Verluste
- Verbessert Effizienz von Kältemaschinen/Kesseln durch korrektes ΔT

#### Installations- und Kosteneinsparungen
- **50% Arbeitskosten-Reduktion**: Gegenüber separater Regelventil + Balancierventil-Installation
- **2/3 Platzersparnis**: Rohrlänge durch Integration beider Funktionen
- **Keine Balancierventile erforderlich**: PICV ersetzt separate Balancierventile

#### Dokumentierte Einsparungen
- **University of Miami Rosenstiel Building**: 57.890 USD/Jahr
- **Potenzial**: In einigen Fällen bis zu 1,5 Mio. USD/Jahr

#### Smart-Funktionen
- **Cloud-basierte Analytik**: System-Daten für Effizienz-Optimierung nutzen
- **Echter Durchfluss**: Elektronische Messung statt mechanische Approximation

---

## 5. TABS - BETONKERNAKTIVIERUNG

### 5.1 TABS-Technologie Übersicht

#### Definition und Funktionsprinzip
- **TABS** (Thermally Activated Building Systems): Radiant Heiz-/Kühlsystem in Gebäudemasse integriert
- **Rohre**: Im Zentrum der Betondecke eingebettet (typisch: 20 mm Außendurchmesser, 150 mm Abstand)
- **Wärmeübertragung**: Hauptsächlich durch Strahlung
- **Thermischer Speicher**: Nutzung der Betonmasse für Energiespeicherung

#### Typische Konstruktion
- **Decke**: 18 cm dicke Betondecke, Rohre in der Mitte
- **Boden**: 45 mm Estrich, 20 mm Dämmung, 180 mm Beton
- **Rohrmaterial**: Kunststoff (z.B. PE-X), 20 mm Außendurchmesser
- **Verlegeabstand**: 150 mm (typisch)

### 5.2 Betriebstemperaturen und Auslegung

#### Vorlauftemperaturen aus Literatur

**Kühlung**:
- **Literaturbereich**: 15-20°C (aus ÖNORM B 8135 und Uponor-Dokumentation)
- **Mittlere Betriebstemperatur**: 18-28°C (Bereich für Heizung und Kühlung)
- **Taupunkt-Sicherheit**: Vorlauf ≥ Taupunkt-Temperatur im Raum (typisch 15°C Minimum)

**Heizung**:
- **Literaturbereich**: 28-32°C
- **Ziel**: Betriebstemperatur nahe Raum-Solltemperatur (Selbstregulations-Konzept)

#### Temperaturspreizung (ΔT)

**Kühlung**:
- **Literaturbereich**: 3-5 K (typisch für TABS-Kühlung)
- **Beispielwerte**:
  - 7°C Vorlauf / 12°C Rücklauf = 5 K Spreizung
  - 16°C Vorlauf / 20°C Rücklauf = 4 K Spreizung

**Heizung**:
- **Literaturbereich**: 5-7 K
- **Beispielwert**: 45°C Vorlauf / 40°C Rücklauf = 5 K

#### Regelungsstrategie
- **Beste Performance**: Wassertemperatur (Vorlauf oder Mittelwert) als Funktion der Außentemperatur steuern
- **Heizkurve/Kühlkurve**: Höhere Vorlauftemperatur bei niedrigen Außentemperaturen (Heizung), niedrigere bei hohen Außentemperaturen (Kühlung)
- **Keine Raumtemperatur-Regelung erforderlich**: Selbstregulation durch geringe Temperaturdifferenz zum Raum

### 5.3 Systemträgheit und Regelungsstrategien

#### Thermische Trägheit - Charakteristika
- **Hohe thermische Masse**: Langsame Reaktionszeit, aber exzellente Lastglättung
- **Zeitverzögerung**: Zwischen Betrieb und tatsächlicher Wärmeabgabe/Aufnahme ins/aus dem Raum
- **Reaktionszeit**: Mehrere Stunden (abhängig von Deckenstärke und Rohrlage)
- **Peak Shaving**: Spitzenlasten werden durch Speicherkapazität abgefangen oder zeitlich verschoben

#### Regelungsherausforderungen
- **Konventionelle Regelung**: Schwierig aufgrund hoher thermischer Trägheit
- **Asynchronität**: TABS-Betrieb und tatsächliche Wärmeabgabe zeitlich versetzt
- **Risiken**: Überhitzung/Unterkühlung bei plötzlichen Laständerungen, Kondensation

#### Fortgeschrittene Regelungsstrategien

**Model Predictive Control (MPC)**:
- Adressiert thermische Trägheit durch Vorhersagemodelle
- Optimiert Effizienz bei gleichzeitiger Erreichung von Komfort-/Demand-Response-Zielen
- Empfohlen für TABS-Systeme mit hoher Energieflexibilität

**Pulse Width Modulation (PWM)**:
- Intermittierender Betrieb statt kontinuierlich
- **>50% Einsparung Pumpenenergie** gegenüber kontinuierlichem Betrieb
- **>86% elektrische Pumpenergie-Einsparung** dokumentiert

**Wetteranpassung**:
- Inklusive Wettervorhersage: **>41% thermische Energieeinsparung** möglich

**Selbstregulations-Konzept**:
- Systemtemperatur = Raum-Solltemperatur
- Vermeidet Überhitzung/Unterkühlung und Kondensation

#### Sensoren und Steuerungsvariablen
- **Typisch**: Embedded Slab Temperature Sensor oder Raum-Lufttemperatur-Sensor
- **Kontrollierte Variablen**: Vorlauftemperatur, Durchfluss in Rohren
- **Dead Band**: 2 K Raumtemperatur-Intervall, bei dem Umwälzpumpe stoppt

### 5.4 Siemens Acvatix-Aktoren für TABS

#### Siemens SQL-Serie (Rotations-Aktoren)
- **Typ**: Elektromotorische Aktoren
- **Drehmoment-Bereich**: 10-1.200 Nm
- **Drehwinkel**: 90° (werksseitig), einstellbar 70-180°
- **Nennspannung**: AC 230 V, 3-Punkt-Steuerung
- **Schutzart**: IP44

**Modelle**:
- **SQL33.00/SQL33.03**: 10-12,5 Nm, 90°, AC 230 V
- **SQL36E110**: 400 Nm, 90°, AC 230 V
- **SQL36E160**: 1.200 Nm, 90°, AC 230 V

**Funktionen**:
- Eingebaute Endschalter
- Nachrüstbar: 1 Doppel-Hilfsschalter, 1 Potentiometer, 1 Positionierzeit-Modul

#### Siemens Acvatix-Produktpalette
- **Vielseitig**: Ventile und Aktoren für nahezu alle HLK-Anforderungen
- **Typen**: Elektrohydraulisch, elektromotorisch, thermostatisch, thermisch
- **Anwendung**: Kugelhähne, Absperrkugelhähne, Mischventile, Schieberventile, Klappen-Aktoren

#### TABS-spezifische Steuerung
- **Herausforderung**: Langsame Reaktionszeit und Speicherkapazität
- **Forschungsfokus**: Entwicklung optimierter Regelungsstrategien für Energieeinsparung und erneuerbare Energien
- **Acvatix-Eignung**: Präzise Steuerungsfähigkeiten und energieeffiziente Betriebscharakteristika

### 5.5 Hydraulische Einregulierung

#### Statische vs. Dynamische Einregulierung

**Statische Einregulierung**:
- **Prinzip**: Massenströme manuell über druckabhängige Ventile reguliert
- **Auslegung**: Nur für Volllast-Fall optimiert
- **Einsparpotenzial**: 5-15% (abhängig von Gebäude und Technologie)
- **Limitierung**: Volllast nur wenige Tage/Jahr → restliche Zeit nicht optimal

**Dynamische Einregulierung**:
- **Prinzip**: Wasserströme und Differenzdruck unter allen Lastbedingungen konstant halten
- **DPCV (Differential Pressure Control Valves)**: 10% Einsparpotenzial
- **PICV (Pressure Independent Control Valves)**: 30% Einsparpotenzial
- **Selbstkompensation**: Druckerhöhung bei Durchflusserhöhung wird durch Feder-Diaphragma-System kompensiert

#### Energieeffizienz-Potenzial
- **Statisch**: 5-15% Einsparung
- **Dynamisch (DPCV)**: 14,6-23,8% Einsparung
- **Dynamisch (PICV)**: Bis zu 30% Einsparung
- **Maximum**: Bis zu 25% in hydraulisch unbalancierten Systemen, in extremen Fällen >30%

#### TABS-Spezifische Überlegungen
- **DIN 94679 Entwurf Teil 1, Tabelle 2**:
  - Adaptive thermische Einregulierung auf "Radiatoren, Heizkörpern, Konvektoren (Zweirohr)" nicht üblich
  - Hinweis: Systeme mit höherer thermischer Trägheit (wie TABS) besser geeignet für bestimmte Balancing-Ansätze
- **Empfehlung**: Dynamische Einregulierung bevorzugt für TABS-Systeme

### 5.6 BELIMO Energy Valve für TABS

#### Vorteile für TABS-Anwendungen
- **Dynamisches Balancing**: Ohne manuelle Einregulierung, unabhängig von Druckschwankungen
- **Perfekt balanciert**: Spule/Zweig immer optimal, auch bei Rohrleitungsänderungen
- **Eliminiert Überdurchfluss**: Größte Energieeinsparung durch Beseitigung von Overflow
- **Erhöhte ΔT**: Berechnetes ΔT führt zu höherer Effizienz von Kältemaschinen und Kondensationskessel

#### Integration mit TABS
- Besonders vorteilhaft für TABS-Sekundärkreise (Verteilung zu TABS-Zonen)
- Verbessert Reaktionsfähigkeit des trägen TABS-Systems durch präzise Durchflussregelung
- Unterstützt Demand-Based Control durch echte Durchflussmessung

---

## 6. FAN-COIL-SYSTEME UND HYBRIDSTRATEGIEN

### 6.1 Fan-Coil-Unit Spezifikationen

#### Betriebstemperaturen

**Kühlung - Wassertemperaturen**:
- **Chinesische Norm**: 7°C Vorlauf
- **US-Praxis**: 45°F Vorlauf / 55°F Rücklauf (7,2°C / 12,8°C) = 10°F (5,6 K) Spreizung
- **Internationale Norm**: 7°C Vorlauf / 12°C Rücklauf = 5°C (5 K) Spreizung
- **Good Practice**: 46-50°F durchschnittliche Kaltwasser-Temperatur (7,8-10°C)

**Kühlung - Lufttemperaturen**:
- **Supply Air**: 55°F (12,8°C) typisch
- **Return Air**: 75°F (23,9°C) typisch
- **ΔT Luft**: 20°F (11 K) typisch für Kühlung
- **Niedrigste durchschnittliche Ablufttemperatur**: 8°C bei Kaltwasser-Nutzung

**Heizung - Wassertemperaturen**:
- **US-Praxis**: 180°F Vorlauf / 160°F Rücklauf (82°C / 71°C) = 20°F (11 K) Spreizung
- **Internationale Norm**: 45°C Vorlauf / 40°C Rücklauf = 5°C (5 K) Spreizung

**Heizung - Lufttemperaturen**:
- **Raumtemperatur**: 70°F (21°C) Dry Bulb

#### Auslegungsparameter

**Luftgeschwindigkeit über Spule**:
- **Empfohlen**: 250-300 fpm (1,27-1,52 m/s) für verbesserte Wärmeübertragung und reduzierten Druckabfall
- **Konventionell**: 500 fpm (2,54 m/s)

**Approach Temperature**:
- **Empfohlen**: 5-8°F (2,8-4,4 K) statt 10-15°F (5,6-8,3 K)

**Luftmenge**:
- **Faustformel**: 400 cfm pro Tonne Kühlung (12.000 BTUH)
- **Hinweis**: Kann zu kälterer Zuluft führen als gewünscht für manche gewerbliche Anwendungen

#### Einsatzgebiete Bürogebäude
- **Verbreitung**: Sehr häufig in Büros, Bars, Kantinen, teilweise Wohnungen
- **Beliebtes System**: Zweirohr-System für Wohngebäude und kleine Büros (einfachere Installation, kompakter, niedrigere Kosten)
- **Vier-Rohr-System**: Häufigster Typ für größere Gebäude
  - 2 Vorlauf + 2 Rücklauf
  - Ermöglicht gleichzeitiges Heizen und Kühlen verschiedener Zonen (unterschiedliche interne Gewinne/Verluste)

### 6.2 Fan-Coil-Typen

#### Kassetten-Geräte (Cassette Units)
- **Montage**: Deckeneinbau
- **Kapazitätsbereich**: 2-5 kW (neuere Einheiten)
- **Kühlbedingungen**: 27°C DB / 19,5°C WB Eintrittsluft, 7°C Vorlauf / 12°C Rücklauf, hohe Ventilatorgeschwindigkeit
- **Heizbedingungen**: 20°C DB Eintrittsluft, 45°C Vorlauf / 40°C Rücklauf, hohe Ventilatorgeschwindigkeit
- **Temperaturregelung**: Präzise, ±0,5°C in Automatikmodus (BLDC-Modelle)
- **Thermostat-Präzision**: Bis zu 0,5°C (moderne Thermostate wie THT420)
- **Geräuscharm**: Fortschrittliche Low-Noise Fan-Technologie

#### Konsolen-Geräte (Console Units)
- **Montage**: Wandmontage oder freistehend
- **Anwendung**: Flexible Aufstellung im Raum

#### Regelung
- **Raumtemperatur-Regler**: 8-30°C einstellbar
- **"Comfort Zone"**: Hervorgehoben (ca. 20-25°C)
- **Ventilatorgeschwindigkeit**: Typisch 3 Stufen

### 6.3 TABS + Fan-Coil Hybrid-Strategien

#### Systemphilosophie

**TABS-Funktion**:
- **Grundlast**: TABS übernimmt Basis-Heiz-/Kühllast
- **Träger Reaktion**: Nutzt Nacht-Vorkühlung/-heizung der Deckenmasse
- **Konstantes Komfortniveau**: Aufrechterhaltung stabiler Innenraumtemperatur
- **Kosteneffizient**: 16-27% niedrigere Global Costs (Whole Life Cost) vs. luftbasierte HLK

**Fan-Coil-Funktion**:
- **Schnelle Reaktion**: Schnelles Heizen/Kühlen bei Lastspitzen
- **Peak-Handling**: Adressiert plötzliche Wärme-/Feuchtelasten
- **Multi-Zonen-Kontrolle**: Individuelle Raumregelung
- **Entfeuchtung**: Unterstützt TABS bei Feuchtigkeitsmanagement

#### Hybrid-System-Vorteile

**Performance**:
- **Kondensationsvermeidung**: Fan-Coils übernehmen Entfeuchtung, TABS kann sicher bei höheren Temperaturen laufen
- **Schnelle Anpassung**: Fan-Coils kompensieren Trägheit des TABS-Systems
- **Thermischer Komfort**: Zufriedenstellendes Raumklima mit Multi-Zonen-Steuerung

**Energieeffizienz**:
- **TABS als Standard**: Standard für neue Bürobauten für Kühlung
- **Ideal mit Lüftung**: Kombination mit mechanischer/natürlicher Lüftung
- **30-50% niedrigere Installationskosten**: Kleinere Kältemaschinen, reduzierte Lüftungskanäle
- **30% bessere Life-Cycle-Kosteneffizienz**: Über gesamte Gebäudenutzungsdauer

#### Hybrid-Betriebsstrategien

**Nicht-zentralisiertes Hybrid-System** (Literaturbeispiel):
- **Air Source Heat Pump**: Zentrale Wärmeerzeugung/-abfuhr
- **Floor Coils**: Hohe thermische Masse (TABS-analog)
- **Fan Coils**: Schnelle Reaktion, Entfeuchtungsunterstützung
- **Outdoor Air Dehumidifier**: Precooling DX-Entfeuchter
- **DOAS-Funktion**: Fan-Coils + Entfeuchter = dispersed Dedicated Outdoor Air System
- **Resultat**: Zufriedenstellendes Innenraumklima, verbesserte radiant system response

#### Best Practice TABS + Fan-Coil
- **Langsam + Schnell**: Kombination träger TABS mit schnellen Fan-Coils
- **Basis + Peak**: TABS für Grundlast (energieeffizient), Fan-Coils für Spitzen (bedarfsgerecht)
- **Strahlung + Konvektion**: TABS strahlend, Fan-Coils konvektiv für vollständige Raumkonditionierung

---

## 7. GEBÄUDEAUTOMATION - SIEMENS DESIGO

### 7.1 Siemens Desigo-Plattform

#### Systemübersicht
- **Integrierte Plattform**: Vereint HVAC, Beleuchtung, Sicherheit in einem intelligenten Ökosystem
- **Auslegung**: Speziell für HLK-Steuerung, flexibel und skalierbar für verschiedene Gebäudetypen
- **Zukunftssicher**: Moderne Technologie für optimale Kontrolle aller Facility Operations

#### Hauptkomponenten

**Desigo PXC Controllers**:
- **Funktion**: Automation für optimale Steuerung aller Gebäudetypen
- **Vorteile**: Resiliente HLK-Steuerung, reduzierte Engineering-Zeit, sichere Remote-Konnektivität
- **Skalierbar**: Modulares Konzept für Projekte jeder Größe

**Climatix C600**:
- **Auslegung**: Komplexe Anwendungen bis 8.000 Objekte
- **Anwendungen**: Lüftung mit integrierter Kältemaschine, Wärmepumpen, Fernwärmestationen, Luftbehandlungseinheiten, Rooftop Units
- **Cloud-Anbindung**: Einfache Verbindung für digitalisierte Services

**Desigo tx2 Economizer**:
- **Fokus**: Energieverbrauch und CO₂-Emissionen-Optimierung
- **Funktion**: Luftaufbereitung mit kostengünstigster Energieform
- **Ziel**: Optimale Raumluftqualität bei minimalen Kosten

### 7.2 Kühlung und Optimierung

#### Anlagenautomation
- **Effiziente Steuerung**: Automation Stations für Energieerzeugung und -verteilung mit Energiesparfunktionen
- **Datenaustausch mit Raumautomation**: Energie nur bei Bedarf zum Heizen/Kühlen/Lüften
- **Bedarfsoptimierung**: Luftvolumenstrom auf Basis tatsächlichem Bedarf optimiert

#### Energieeffizienz-Funktionen
- **Signifikante Verbrauchsreduktion**: Durch intelligente Steuerungsalgorithmen und Echtzeit-Datenanalyse
- **Niedrigere Betriebskosten**: Reduzierte Stromrechnungen und CO₂-Fußabdruck
- **Advanced Analytics**: Big Data und Machine Learning für kontinuierliche Performance-Analyse
- **Ongoing Optimization**: Identifizierung von Trends und Verbesserungspotenzialen

#### Kältemaschinen-spezifische Optimierung
- **Intelligent Control Algorithms**: Kältemaschinen, Wärmepumpen, HLK-Optimierung
- **Plant Management**: Umfassende Anlagenüberwachung und -steuerung
- **Real-Time Adaptation**: Kontinuierliche Anpassung an aktuelle Bedingungen

### 7.3 Regelungsstrategien

#### Temperatur-Regelung
- **Fest vs. Gleitend**:
  - Feste Vorlauftemperatur: Einfach, aber weniger effizient
  - Gleitende Vorlauftemperatur: Anpassung an Außentemperatur, höhere Effizienz
- **Demand-Based Control**: Bedarfsgeführte Steuerung auf Basis Raumbedarf

#### Heizung/Kühlung-Interlock
- **Funktion**: Verhindert gleichzeitiges Heizen und Kühlen
- **Energieverschwendung ohne Interlock**: 5-15% aus Literatur
- **Implementierung**: Automatische Umschaltung basierend auf Anforderung

#### Ventil-Dichtheit
- **Problem**: Leckende Ventile verursachen Energieverluste
- **Typische Leckageraten**: Abhängig von Alter und Wartungszustand
- **Lösung**: BELIMO Zero Leakage Technology, regelmäßige Wartung

### 7.4 System-Integration und Skalierbarkeit

#### Offenheit und Kompatibilität
- **Hervorragende Skalierbarkeit**: Von kleinen bis sehr großen Projekten
- **Systemoffenheit**: Weite Palette frei programmierbarer Automation Stations
- **Primäranlagen**: Modular für optimale Flexibilität

#### Vorteile für 2.800 m² Bürogebäude
- **Optimale Größe**: Desigo PXC ideal für diese Gebäudegröße
- **Integration**: TURBOCOR-Kältemaschinen, TABS, Fan-Coils in einem System
- **Überwachung**: Zentrale Visualisierung und Steuerung aller HLK-Komponenten
- **Effizienz**: Intelligent coordinated control für maximale Energieeffizienz

---

## 8. WIENER KLIMA UND ÖSTERREICHISCHER ENERGIEKONTEXT

### 8.1 Klimadaten Wien

#### Heizgradtage (HDD) Österreich
- **2020**: 7.079,31°C-Tage
- **Durchschnitt 1970-2020**: 7.972,30°C-Tage (51 Beobachtungen)
- **Historisches Maximum**: 8.931,52°C-Tage (1980)
- **Historisches Minimum**: 6.793,49°C-Tage (2014)
- **Definition**: Summe der Differenzen zwischen 18°C und durchschnittlicher Tagestemperatur (wenn <18°C)

#### Kühlgradtage (CDD) Österreich
- **2020**: 148,01°C-Tage
- **Durchschnitt 1970-2020**: 114,59°C-Tage (51 Beobachtungen)
- **Historisches Maximum**: 307,35°C-Tage (2015)
- **Historisches Minimum**: 36,77°C-Tage (1978)
- **Definition**: Summe der Differenzen zwischen durchschnittlicher Tagestemperatur und 18°C (wenn >18°C)
- **Trend**: Zunehmend (Klimawandel-Indikator)

#### Temperaturen Wien
- **Jahresdurchschnitt**: 12°C
- **Kältester Monat**: Januar, 0,0°C / 32,1°F
- **Wärmster Monat**: Juli, 21,4-22°C / 70,5°F
- **Klimazone**: Gemäßigt, Einfluss von Atlantik und Kontinentalklima
- **Winter**: Kühl bis kalt, um 0°C (32°F)
- **Sommer**: Warm bis heiß, um 30°C (86°F)

#### Verhältnis HDD zu CDD
- **HDD/CDD-Ratio**: 7.079 / 148 ≈ 48:1 (2020)
- **Interpretation**: Deutlich höherer Heiz- als Kühlbedarf, aber Kühlung zunehmend wichtig
- **Klimawandel-Projektion**: Kühlbedarf Wien 2050 wird 33-55% höher sein als heute

### 8.2 Österreichische Strompreise 2025

#### Gewerbe-/Bürogebäude
- **Aktuelle Business Rate (März 2025)**: EUR 0,269/kWh (USD 0,312/kWh)
- **Medium Commercial Users (Dez 2024)**: EUR 0,20/kWh (EUROSTAT)
- **Kleine bis mittlere Industriebetriebe**: 18,75 Cent/kWh (2025)

#### Preisentwicklung
- **Trend 2024-2025**: Leichter Anstieg gegenüber 2024 (17,09 Cent → 18,75 Cent für Industrie)
- **Historischer Kontext**: Verbesserung gegenüber dramatischen Preissteigerungen 2022-2023
- **Preiskomponenten**: Inklusive Stromerzeugung, Verteilung, Übertragung, alle Steuern und Abgaben

#### Empfohlener Wert für Berechnungen
- **Konservativ**: EUR 0,27/kWh (höchster Wert März 2025)
- **Durchschnittlich**: EUR 0,20/kWh (EUROSTAT Dezember 2024)
- **Projektion**: Werte für 2025 zwischen EUR 0,20-0,27/kWh je nach Verbrauchsgröße

### 8.3 CO₂-Emissionsfaktoren Österreich

#### Aktuell (2025)
- **Real-Time Heute**: 65 g CO₂eq/kWh (89% erneuerbar) - Stand Oktober 2025
- **Monat bisher**: 95 g CO₂eq/kWh (83% erneuerbar) - Stand Oktober 2025
- **Datenquelle**: ENTSOE (European Network of Transmission System Operators for Electricity)

#### Historischer Kontext
- **Europäischer Durchschnitt**: 452 g CO₂/kWh (Residual Mix 2024)
- **Europäische Verbesserung**: -42 g CO₂/kWh Durchschnitt über alle Residual Mixes (2024)

#### Österreichs Position
- **Sehr niedrige Emissionen**: 65-95 g CO₂eq/kWh (zeitabhängig)
- **Hoher Erneuerbare-Anteil**: 83-89% (hauptsächlich Wasserkraft)
- **Vergleich**: Deutlich unter europäischem Durchschnitt

#### Empfohlener Wert für Berechnungen
- **Konservativ**: 95 g CO₂eq/kWh (monatlicher Durchschnitt 2025)
- **Optimistisch**: 65 g CO₂eq/kWh (aktueller Tageswert)
- **Langfristig**: Trend zu weiter sinkenden Emissionen durch Ausbau erneuerbarer Energien

### 8.4 Förderungen Österreich

#### UFI (Umweltförderung im Inland)

**Administration**:
- **Zentrale Verwaltung**: Kommunalkredit Public Consulting GmbH (KPC)
- **Ebene**: Bundesweit (national)

**Schwerpunkte**:
- Erneuerbare Wärme
- Energieeffizienz
- Klimafreundliche Mobilität

**HVAC-relevante Förderungen**:
- **Solarwärmeanlagen**: Besondere Investitionsanreize
- **Wärmepumpen**: Förderung für Unternehmen
- **Geothermie**: Erdwärmenutzung
- **Biomasse-Heizanlagen**: Besonders für Betriebe
- **Energiesparmaßnahmen**: Betriebliche und kommunale (z.B. Effizienzsteigerung in industriellen Prozessen, Optimierung Heizung/Beleuchtung)
- **Wärmebereitstellung aus erneuerbaren Energien**: Nahwärme, Wärmepumpen

**Zielgruppe**:
- Natürliche oder juristische Personen mit Sitz in Österreich (§ 26 Abs. 1 UFG)
- Umfang variiert: Unternehmen, Gemeinden, Privatpersonen

#### Kommunalkredit Public Consulting

**Funktion**:
- Zentrale Abwicklungsstelle für UFI
- Verwaltung Umwelthilfe-Programme
- Fokus: Umweltschutzmaßnahmen und Klimaschutz

**Programme**:
- Ergänzt durch Climate and Energy Fund
- Umweltschutzförderung für breite Zielgruppen

#### Climate and Energy Fund (Klima- und Energiefonds)

**Schwerpunkte**:
- Energiebezogene Forschungsprojekte
- Umweltverträgliche Verkehrsprojekte
- Markteinführung klimafreundlicher Energietechnologien

**Komplementär zu UFI**:
- Fokus auf Forschung und Marktentwicklung
- E-Mobility-Programme
- Entwicklung innovativer Programme

#### MA 20 (Wien)

**Relevanz**:
- Lokale Wiener Förderungen
- Umweltschutz auf Stadtebene
- Ergänzung zu bundesweiten Programmen (UFI, Climate and Energy Fund)

**Potenzielle Anwendung**:
- Gebäude in Wien (A-1150) könnte von MA 20-Programmen profitieren
- Kumulation mit UFI möglich (abhängig von Programmdetails)

#### Zusammenfassung Förderung

Österreich verfügt über ein **umfassendes Fördersystem** für Klima- und Energieprojekte:

1. **UFI** (Kommunalkredit): Hauptinstrument für Umwelt-/Energieeffizienz-Investitionen
2. **Climate and Energy Fund**: Forschung, Innovation, Markteinführung
3. **MA 20** (Wien): Lokale Ergänzung für Wiener Projekte

**HVAC-Kühlung**: Indirekt gefördert durch Energieeffizienz-Maßnahmen, Wärmepumpen-Programme, erneuerbare Energien-Integration.

---

## 9. TYPISCHE WERTE UND FORMELN AUS LITERATUR

### 9.1 COP/EER-Werte

#### Magnetlager-Verdichter (TURBOCOR)
- **Durchschnitt**: COP 8-10 (unter verschiedenen Lastbedingungen)
- **Full Load**: COP bis 7,0
- **IPLV**: Bis 9,5
- **Teillast-Optimum**: COP-Maximum bei PLR 0,71-0,84

#### Konventionelle Zentrifugalkältemaschinen
- **Typisch**: COP 3-6
- **Hinweis**: TURBOCOR übertrifft konventionelle Systeme deutlich, besonders bei Teillast

#### Einflussfaktoren auf COP
- Kondensatorwasser-Eintrittstemperatur (niedriger = besser)
- Außentemperatur (Kurven über -5°C bis +43°C)
- Part Load Ratio (Optimum bei 70-80%, nicht Volllast)
- Hydraulik-Optimierung (niedrigerer Pumpenkopf verbessert Gesamt-COP)

### 9.2 VFD-Pumpen Einsparpotenziale

#### Literaturwerte
- **20-40% typisch**: VFD-Retrofit bei Pumpen (Standard-Bereich)
- **Bis zu 75-80%**: Wilo Stratos / Grundfos Smart Pumps (Spitzenwerte)
- **Bis zu 90%**: KSB Rio-Eco in Klima-/Heizanwendungen vs. feste Drehzahl

#### Physikalisches Gesetz
- **Affinitätsgesetze**: Leistung ∝ Drehzahl³
- **Beispiel**: 20% Drehzahlreduktion → 49% Energieeinsparung

### 9.3 Hydraulische Einregulierung - Einsparpotenziale

#### Statische Einregulierung
- **5-15%**: Energieeinsparung (abhängig von Gebäude und Technologie)

#### Dynamische Einregulierung
- **DPCV**: 10-15% Energieeinsparung
- **PICV**: 20-30% Energieeinsparung
- **Studien**: 14,6-23,8% durch TRV/DPCV/PIBRV-Implementierung
- **Maximum**: Bis 25-30% in stark unbalancierten Systemen

### 9.4 TABS-Temperaturen

#### Kühlung
- **Vorlauf**: 15-20°C (ÖNORM B 8135, Uponor)
- **ΔT**: 3-5 K
- **Rücklauf**: 18-25°C (berechnet aus Vorlauf + ΔT)

#### Heizung
- **Vorlauf**: 28-32°C
- **ΔT**: 5-7 K
- **Rücklauf**: 23-25°C (berechnet)

### 9.5 Fan-Coil-Temperaturen

#### Kühlung - Wasser
- **Vorlauf**: 6-12°C (7°C typisch)
- **Rücklauf**: 12-17°C
- **ΔT**: 5-7 K

#### Kühlung - Luft
- **Supply**: 8-13°C (typisch 12,8°C / 55°F)
- **Return**: 23-24°C (typisch 23,9°C / 75°F)
- **ΔT**: 11 K (20°F)

### 9.6 Pufferspeicher-Dimensierung

#### Formel
```
Erforderliche Systemkapazität (L) = Kälteleistung (kW) × 4 L/kW
Pufferspeicher (L) = Erforderliche Systemkapazität (L) - Tatsächliches Systemvolumen (L)
```

#### Typische Werte
- **Typisch HVAC**: 3-6 Gallonen/Tonne (11-23 L/kW)
- **Temperaturgenauigkeit hoch**: 6-10 Gallonen/Tonne (23-38 L/kW)
- **Durchschnitt**: 5-11 Gallonen/Tonne (19-42 L/kW)

### 9.7 N+1 Redundanz - Effizienz-Überlegungen

#### Formeln Part Load Ratio (PLR)
```
PLR = Aktuelle Last / Nennkapazität (pro Einheit)

Bei 3 Kältemaschinen (N+1):
- 1 aktiv: PLR = Last / (Gesamt/3)
- 2 aktiv: PLR = Last / (2×Gesamt/3)
- 3 aktiv: PLR = Last / Gesamt
```

#### Optimaler PLR-Bereich
- **0,71-0,84**: Höchster COP (aus Literatur)
- **Empfehlung**: Bei Teillast möglichst viele Einheiten laufen lassen → PLR näher an Optimum

### 9.8 Gebäudeautomation - Einsparpotenziale

#### BAC-Systeme (EN ISO 52120-1)
- **Bis 40%**: Energieeinsparung durch intelligente Gebäudeautomation
- **ROI**: <3 Jahre

#### Interlock Heizung/Kühlung
- **5-15%**: Energieverschwendung ohne Interlock (aus Literatur)

#### PWM-Steuerung TABS
- **>50%**: Pumpenenergie-Reduktion durch intermittierenden Betrieb
- **>86%**: Elektrische Pumpenenergie-Einsparung dokumentiert
- **>41%**: Thermische Energieeinsparung mit Wetteranpassung

---

## 10. PRODUKTDATENBLÄTTER UND SPEZIFIKATIONEN

### 10.1 Benötigte Hersteller-Dokumente

#### TURBOCOR Kältemaschinen
- **Modellspezifikation**: Exakte Typenbezeichnung (TT, VTX, oder spezifischer Modellcode)
- **Datenblätter**: Cooling Capacity Charts, Electrical Power (45 kVA aus Projektkontext)
- **COP-Kurven**: Über Außentemperaturbereich -5°C bis +43°C
- **Wartungsdokumentation**: Intervalle, typische Probleme, Ersatzteile
- **R134a-Spezifikationen**: Füllmengen, Leckage-Prüfung

#### Wilo Stratos D651-12
- **Produktdatenblatt**: Technische Spezifikationen, Leistungsaufnahme, Förderkennlinie
- **VFD-Retrofit**: Machbarkeit, Kosten, Einsparpotenzial für dieses spezifische Modell
- **Installation**: Einbauanleitung, hydraulische Anforderungen

#### KSB Rio-Eco Z65-120
- **Produktdatenblatt**: Technische Spezifikationen, Leistungsaufnahme, Förderkennlinie
- **VFD-Integration**: Native VFD oder Retrofit-Option, PumpDrive-Kompatibilität
- **BACnet/Modbus**: Kommunikations-Schnittstellen für Gebäudeautomation

#### Siemens Acvatix SQL 35
- **Typenschlüssel**: SQL35-Variante (z.B. SQL35.00, SQL35E...)
- **Datenblatt**: Drehmoment, Nennspannung, Laufzeit, IP-Schutzart
- **TABS-Integration**: Empfohlene Ventilkombination, Regelparameter

#### BELIMO EnergyValve-Optionen
- **Produktpalette**: Größen, Durchfluss-Bereiche (DN 25-150)
- **Spezifikation**: Druckklassen, Kvs-Werte, Regelcharakteristik
- **Cloud-Integration**: Kompatibilität mit Siemens Desigo
- **ROI-Kalkulation**: Herstellerangaben zu typischen Einsparungen

### 10.2 Normen-Dokumente (Beschaffung empfohlen)

#### ÖNORM
- **ÖNORM H 5155:2024-11-01**: Wärmedämmung betriebstechnischer Anlagen
- **ÖNORM H 5160-1:2023**: Flächenheizung und -kühlung
- **ÖNORM H 5195-1:2024**: Raumheizung
- **ÖNORM B 8110-6-1:2024**: Wärmeschutz im Hochbau

**Bezugsquelle**: Austrian Standards (www.austrian-standards.at)

#### VDI
- **VDI 2067 Teil 1**: Wirtschaftlichkeit - Grundlagen
- **VDI 2067 Teil 10**: Energieaufwand Heizen/Kühlen
- **VDI 2067 Teil 21**: HLK-Systeme
- **VDI 3803 Teil 1**: Lufttechnische Anlagen - Anforderungen
- **VDI 3803 Teil 5**: Wärmerückgewinnung

**Bezugsquelle**: Beuth Verlag (www.beuth.de) oder VDI (www.vdi.de/richtlinien)

#### ISO/EN
- **EN ISO 52120-1:2022**: Building automation contribution to energy performance
- **EN 14240:2004**: Testing and evaluation of TABS

**Bezugsquelle**: Austrian Standards, Beuth Verlag

---

## 11. LITERATURQUELLEN - KATEGORISIERT

### 11.1 Peer-Reviewed Papers

#### TURBOCOR & Magnetic Bearing Chillers
- Kontomaris et al.: R513A use in R134a-designed chillers (ähnliche Effizienz)
- Velasco et al.: R513A vs. R134a EER comparison (bis 24% Reduktion)
- Navy Testing: Magnetic-bearing chiller energy savings (49% average)

#### TABS
- Gwer et al. (2009): "Control of thermally-activated building systems (TABS)" - Applied Energy
- Lehmann et al. (2007): "Control of thermally-activated building systems (TABS)" - Applied Energy
- Multiple ResearchGate publications on TABS control strategies, PWM, MPC

#### Hydraulic Balancing
- Estimation of energy savings through hydraulic balancing (ScienceDirect)
- Impact of diverse valves (TRV, DPCV, PIBRV) on energy savings: 14,6-23,8%

#### Fan-Coil & Hybrid Systems
- Performance analysis hybrid non-centralized radiant floor cooling (ScienceDirect)
- Hybrid RFC system with floor coils, fan coils, DX dehumidifier

### 11.2 Industry Standards & Guidelines

#### BAC & ISO 52120-1
- Danfoss: "New EN ISO 52120 BACS Standard for building efficiency"
- EU.BAC Guide: "THE NEW EN ISO 52120 IS REPLACING EN 15232"
- EPB Center: ISO 52120-1 documentation
- Schneider Electric Blog: "Unlocking energy efficiency: ISO 52120-1 impact"

#### VDI & ÖNORM
- VDI website documentation (www.vdi.de)
- Austrian Standards website (www.austrian-standards.at)
- ÖNORM database (www.bdb.at)

### 11.3 Manufacturer Documentation

#### Danfoss Turbocor
- Website: www.danfoss.com/turbocor
- Product pages: TT Series, VTX Series
- White papers on magnetic bearing technology

#### Wilo
- BuildingGreen: "High-Efficiency, Variable-Speed Pumps from Wilo and Grundfos"
- Case Study: 76% Energy Savings with Stratos D
- Product website: wilo.com

#### KSB
- Product pages: Rio-Eco N series
- PumpDrive documentation
- Building Services HVAC section

#### BELIMO
- Energy Valve Technical Documentation
- Application Guide
- Blog posts on PIV benefits
- University of Miami case study ($57.890/year savings)

#### Siemens
- Desigo building automation product pages
- Acvatix valves and actuators documentation
- Climatix C600 technical specifications

#### Uponor
- TABS (Contec) product documentation
- Technical guides on concrete core activation
- Installation and design guidelines

### 11.4 Government & Energy Agencies

#### Climate Data
- CEIC: Austria Cooling/Heating Degree Days
- World Bank Climate Data
- IEA: Heating degree days Austria 2000-2020
- Worlddata.info: Climate Vienna

#### Energy Costs & Emissions
- GlobalPetrolPrices.com: Austria electricity prices
- STATISTIK AUSTRIA: Energy prices, taxes
- EUenergy.live: Electricity prices Austria
- Nowtricity: Real-time CO₂ emissions Austria
- ElectricityMaps: Live CO₂ emissions
- Our World in Data: Carbon intensity electricity, Austria CO₂ profile

#### Funding
- BMI (Bundesministerium Inneres): UFI information
- RES-LEGAL: Environmental assistance Austria (UFI)
- Invest in Austria: Company funding
- ERA-LEARN: Austrian Climate and Energy Fund
- Kommunalkredit Public Consulting: Climate Austria

### 11.5 Technical Guides & Best Practice

#### REHVA Journal
- "Operation and control of thermally activated building systems (TABS)"

#### EPB Center
- EN ISO 52120-1 documentation

#### Building Engineering Mindset
- Fan Coil Units explained
- HVAC system fundamentals

#### Engineering Portals
- Price Industries: Fan & Blower Coils Engineering Guide
- PDHonline: HVAC Air Handling Unit Design Considerations

---

## 12. EMPFEHLUNGEN FÜR PHASE 7 (VORAUSBLICK)

Phase 2 liefert Literaturkontext. **Phase 7 wird projektspezifische Berechnungen durchführen**. Folgende Formeln und Methoden sind aus der Literatur anwendbar:

### 12.1 Jahresverbrauch Kühlung

#### Formel
```
Jahresverbrauch (kWh/a) = Kühlenergiebedarf (kWh/a) / Durchschnitts-COP

Mit:
Kühlenergiebedarf = Kühllast (kW) × Volllaststunden (h/a)
oder
Kühlenergiebedarf = Gebäudefläche (m²) × spezifischer Kühlbedarf (kWh/m²a)
```

#### Benchmarks für Vergleich
- **Ist-Zustand**: Mit Messdaten vergleichen gegen 33-37 kWh/m²a (Österreich typisch)
- **Soll-Zustand**: Mit Optimierungen Richtung 15-20 kWh/m²a (Good Practice)

### 12.2 COP-Berechnung

#### Aus Messdaten
```
COP = Kälteleistung (kW) / Elektrische Leistung (kW)

Mit:
Kälteleistung = Massenstrom (kg/s) × spez. Wärmekapazität (kJ/kg·K) × ΔT (K)
Massenstrom = Volumenstrom (m³/h) × Dichte (kg/m³) / 3600
```

#### Für Kaltwasser
```
Kälteleistung (kW) = Volumenstrom (m³/h) × 4,18 (kJ/kg·K) × ΔT (K) × 1000 (kg/m³) / 3600 (s/h)
Vereinfacht: Kälteleistung (kW) ≈ 1,163 × Volumenstrom (m³/h) × ΔT (K)
```

### 12.3 Pumpenenergie

#### Hydraulische Leistung
```
P_hydraulisch (kW) = Volumenstrom (m³/s) × Druck (Pa) / 1000
oder
P_hydraulisch (kW) = Volumenstrom (m³/h) × Druckhöhe (m) × ρ (kg/m³) × g (m/s²) / (3600 × 1000)
```

#### Elektrische Leistung
```
P_elektrisch (kW) = P_hydraulisch (kW) / Wirkungsgrad_Pumpe
```

#### VFD-Einsparung (Affinitätsgesetze)
```
P_neu / P_alt = (n_neu / n_alt)³

Mit:
n = Drehzahl
P = Leistung
```

### 12.4 TABS-Leistung

#### Wärmeübertragung
```
Q (kW) = Massenstrom (kg/s) × c_p (kJ/kg·K) × ΔT (K)

Für Wasser:
Q (kW) = Volumenstrom (L/h) × 1,163 × ΔT (K) / 1000
```

#### Flächen-Leistung
```
q (W/m²) = Q (W) / A_aktiviert (m²)
```

#### Typische Werte aus Literatur
- **Kühlung**: 30-50 W/m² Decken-/Bodenfläche
- **Heizung**: 40-60 W/m² Decken-/Bodenfläche

### 12.5 Wirtschaftlichkeit

#### Energiekosten-Einsparung
```
Einsparung (€/a) = ΔVerbrauch (kWh/a) × Strompreis (€/kWh)

Mit:
ΔVerbrauch = Verbrauch_Ist - Verbrauch_Optimiert
```

#### CO₂-Einsparung
```
CO₂-Reduktion (kg/a) = ΔVerbrauch (kWh/a) × Emissionsfaktor (kg CO₂/kWh)

Für Österreich:
Emissionsfaktor = 0,095 kg CO₂/kWh (Durchschnitt 2025)
```

#### Amortisationszeit (Simple Payback)
```
Amortisation (Jahre) = Investitionskosten (€) / Jährliche Einsparung (€/a)
```

---

## 13. ZUSAMMENFASSUNG UND NÄCHSTE SCHRITTE

### 13.1 Was Phase 2 geliefert hat

#### Technische Normen und Standards
- ✓ ÖNORM H 5155, H 5160, H 5195 (TABS-relevant)
- ✓ VDI 2067, 3803 (Wirtschaftlichkeit, HLK)
- ✓ EN ISO 52120-1 (BAC-Faktor-Methode)

#### Benchmarks
- ✓ Österreich Kühlverbrauch: 33-37 kWh/m²a (typisch), 15-20 kWh/m²a (good practice)
- ✓ TURBOCOR COP: 8-10 (durchschnittlich), bis 9,5 IPLV
- ✓ VFD-Pumpen: 20-40% (typisch), bis 75-80% (Spitzenwerte)
- ✓ Hydraulische Einregulierung: 5-15% (statisch), 20-30% (dynamisch PICV)
- ✓ TABS PWM: >50% Pumpenergie, >41% thermische Energie

#### Produktspezifikationen
- ✓ TURBOCOR: TT/VTX-Serie, R134a, 200-1.600 kW, Magnetlager ölfrei
- ✓ Wilo Stratos / KSB Rio-Eco: VFD-Pumpen mit EC-Motor, 75-90% Einsparung
- ✓ BELIMO Energy Valve: PICV, 50% Installationskostenreduktion, dokumentierte Einsparungen
- ✓ TABS: 15-20°C Kühlung, 28-32°C Heizung, 3-5 K ΔT
- ✓ Fan-Coils: 7°C Vorlauf, 5 K ΔT Wasser, 11 K ΔT Luft

#### Klimadaten und Energiekontext
- ✓ Wien HDD: 7.079°C-Tage (2020), CDD: 148°C-Tage (2020)
- ✓ Strompreis Österreich: EUR 0,20-0,27/kWh (2025)
- ✓ CO₂-Faktor: 65-95 g CO₂eq/kWh (83-89% erneuerbar)
- ✓ Förderungen: UFI (Kommunalkredit), Climate and Energy Fund, MA 20 (Wien)

#### Literaturquellen
- ✓ Peer-reviewed papers (TABS, Chillers, Hydraulics)
- ✓ Herstellerdokumentation (Danfoss, Wilo, KSB, BELIMO, Siemens, Uponor)
- ✓ Normen (ÖNORM, VDI, ISO)
- ✓ Klimadaten (CEIC, World Bank, IEA)
- ✓ Energiedaten (STATISTIK AUSTRIA, Nowtricity, GlobalPetrolPrices)

### 13.2 Was Phase 2 NICHT geliefert hat (by Design)

❌ Projektspezifische Berechnungen (→ Phase 7)
❌ Jahresverbrauch aus Messdaten (→ Phase 7)
❌ ROI-Berechnungen (→ Phase 8)
❌ Optimierungsempfehlungen (→ Phase 8)
❌ Maßnahmenkatalog mit Priorisierung (→ Phase 8)

### 13.3 Handlungsempfehlungen für nächste Phasen

#### Phase 3-6: Messdaten-Analyse und Modellierung
- Messdaten aus BMS (Siemens Desigo) extrahieren
- Kältemaschinen-Laufzeiten, Leistungsaufnahmen, Vorlauf-/Rücklauftemperaturen analysieren
- Pumpen-Betriebspunkte ermitteln
- TABS-Temperaturen über Jahresverlauf tracken
- COP-Kurven aus realen Betriebsdaten erstellen

#### Phase 7: Berechnungen (mit Literaturkontext aus Phase 2)
- Jahresverbrauch berechnen (Formeln aus 12.1)
- Ist-COP vs. Literatur-Benchmark vergleichen
- VFD-Potenzial quantifizieren (Formeln aus 12.3)
- TABS-Effizienz bewerten (Vorlauf-Temps vs. Literatur aus 5.2)
- Hydraulische Einregulierung bewerten (statisch vs. dynamisch aus 5.5)

#### Phase 8: Wirtschaftlichkeit und Optimierung
- Maßnahmen identifizieren (VFD-Retrofit, BELIMO Energy Valves, Regelungsoptimierung)
- Investitionskosten schätzen (Herstellerangebote einholen)
- Einsparungen berechnen (€/a, kg CO₂/a)
- ROI ermitteln (Formeln aus 12.5)
- Förderungen integrieren (UFI-Antrag vorbereiten)
- Priorisierung erstellen (Quick Wins vs. Langfristprojekte)

### 13.4 Offene Fragen für Klärung

1. **Genaue TURBOCOR-Modellbezeichnung**: TT oder VTX? Welche Nennleistung pro Einheit?
2. **Messdaten-Verfügbarkeit**: Welche Parameter liefert Siemens Desigo? Zeitauflösung?
3. **Pumpen-Ist-Zustand**: Feste Drehzahl oder bereits VFD? Falls fest: Stromaufnahme bekannt?
4. **TABS-Zonen**: Wie viele Kreise? Separate Regelung pro Etage/Zone?
5. **Fan-Coil-Anzahl und -Typen**: Kassetten, Konsolen? Zweirohr oder Vierrohr?
6. **Budget**: Investitionsrahmen für Optimierungen? Priorität auf schnellem ROI oder maximaler Einsparung?
7. **Förderantrag**: Soll UFI-Antrag vorbereitet werden? Zeitrahmen?

---

## ANHANG A: ABKÜRZUNGSVERZEICHNIS

| Abkürzung | Bedeutung (Deutsch/Englisch) |
|-----------|------------------------------|
| **BAC** | Building Automation and Controls / Gebäudeautomation |
| **CDD** | Cooling Degree Days / Kühlgradtage |
| **COP** | Coefficient of Performance / Leistungszahl |
| **DOAS** | Dedicated Outdoor Air System / Dediziertes Außenluftsystem |
| **DPCV** | Differential Pressure Control Valve / Differenzdruckregelventil |
| **EER** | Energy Efficiency Ratio / Energieeffizienzrate |
| **ESEER** | European Seasonal Energy Efficiency Ratio |
| **GWP** | Global Warming Potential / Treibhauspotenzial |
| **HDD** | Heating Degree Days / Heizgradtage |
| **HLK** | Heizung, Lüftung, Klimatechnik |
| **HVAC** | Heating, Ventilation, Air Conditioning |
| **IPLV** | Integrated Part Load Value |
| **MPC** | Model Predictive Control / Modellprädiktive Regelung |
| **ODP** | Ozone Depletion Potential / Ozonabbaupotenzial |
| **PICV** | Pressure Independent Control Valve / Druckunabhängiges Regelventil |
| **PLR** | Part Load Ratio / Teillast-Verhältnis |
| **PWM** | Pulse Width Modulation / Pulsweitenmodulation |
| **ROI** | Return on Investment / Kapitalrendite |
| **SEER** | Seasonal Energy Efficiency Ratio |
| **TABS** | Thermally Activated Building Systems / Thermisch aktivierte Bauteilsysteme (Betonkernaktivierung) |
| **UFI** | Umweltförderung im Inland (Österreich) |
| **VFD** | Variable Frequency Drive / Frequenzumrichter |

---

## ANHANG B: UMRECHNUNGSTABELLE

| Von | Nach | Faktor |
|-----|------|--------|
| **Ton (Kälte, US)** | kW | × 3,517 |
| **kW** | Ton (US) | × 0,284 |
| **Gallonen (US)** | Liter | × 3,785 |
| **Liter** | Gallonen (US) | × 0,264 |
| **°F** | °C | (°F - 32) × 5/9 |
| **°C** | °F | (°C × 9/5) + 32 |
| **fpm** | m/s | × 0,00508 |
| **m/s** | fpm | × 196,85 |
| **BTUH** | kW | × 0,000293 |
| **kW** | BTUH | × 3.412 |
| **psi** | bar | × 0,0689 |
| **bar** | psi | × 14,504 |

---

**Ende Phase 2 Bericht**
**Nächster Schritt**: Messdaten-Analyse (Phase 3-6) → Berechnungen mit Literatur-Benchmarks (Phase 7)
