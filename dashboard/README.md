# Unternehmer-Dashboard (Kiosk/Vectron + Automaten/Televend)

Ein-Blick-Übersicht: Umsatz heute/Zeitraum inkl. Vergleich zur Vorperiode, Tages- und Stundenverlauf,
Top-Artikel, Standorte/Automaten (mit Warnung bei fehlenden Verkäufen), Warengruppen, Zahlarten, Datenstand.

## Start
    python3 server.py --demo    # mit Beispieldaten ausprobieren (Python 3.9+, keine Abhängigkeiten)
    python3 server.py           # produktiv, http://localhost:8080
Zum Zurücksetzen `dashboard.db` löschen.

## Wie die Daten fließen
Beide Systeme liefern CSV-Exporte; das Dashboard liest sie automatisch (alle 5 Min.):

| System | Ablageordner | Export |
|---|---|---|
| Vectron | `inbox/vectron/` | Vectron Commander: Berichte/Export Artikelumsätze je Bon (CSV), als automatischer Export auf Netzlaufwerk/FTP |
| Televend | `inbox/televend/` | Televend-Portal: Verkaufsdaten/Transaktionen CSV-Export (auch per E-Mail/FTP planbar) |

Alternativ per HTTP: `curl -X POST --data-binary @export.csv http://localhost:8080/api/ingest/televend`
(Schutz: `ingest_token` in `config.json` oder Umgebungsvariable `DASHBOARD_TOKEN`, dann Header `Authorization: Bearer <token>`).

Doppelte Zeilen werden erkannt – dieselbe Datei kann bedenkenlos mehrfach geliefert werden.

## Spaltennamen anpassen
Die genauen Spaltenüberschriften hängen von Ihrer Vectron-/Televend-Konfiguration ab. In `config.json`
unter `columns` stehen pro Feld mögliche Überschriften (klein geschrieben). Weicht Ihr Export ab,
einfach den Namen ergänzen. Erkannt werden Trennzeichen `;` `,` Tab, Dezimalkomma und Datum `TT.MM.JJJJ HH:MM`.
