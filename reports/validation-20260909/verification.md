# CTS-G — Diagramme und VST-Abnahme, 9. September 2026

Die neuen Diagramme und die anschließend gefundenen Korrekturen sind lokal
implementiert und committet. Auf dem Server läuft **9fb4aac**; die neueren
Commits **d2e2cbe** und **8bca871** sind dort noch nicht installiert.
Die vollständige Browser- und Exchange-Abnahme des neuen Stands ist offen.

## Neue Statistikansichten

Unter Results → Indications und Results → Strategies gibt es jetzt:

- Eine anklickbare Matrix Indikation × Strategie mit vollständigen
  Konfigurationszahlen aus dem übermittelten Katalog.
- Ein Punktdiagramm für unabhängigen Cost PF × maximale Drawdown-Dauer.
  Die Farbe kennzeichnet die Strategie, die Punktfläche die Stichprobengröße.
- Eine sortierbare Detailtabelle mit PF, DDT, Winrate, Nettoerwartung,
  Haltedauer, Konfigurationsidentität und Berechnungsfreigabe; CSV-Export
  umfasst alle ausgewählten Vorschauzeilen.
- Gemeinsame Filter für System/Exchange, Indikation, TP-Bereich und Strategie.
  Deaktivierte Axis-Strategien bleiben ausgeblendet.

Die Matrix verwendet Katalogzahlen; PF/DDT und Detailzeilen verwenden die
begrenzte Vorschau pro Gruppe. Das ist in der Oberfläche ausdrücklich benannt.
Das Punktdiagramm zeigt maximal 350 Punkte, gleichmäßig über vorhandene
Verbindungen, Indikationen und Strategien verteilt. Fehlende Werte werden nicht
als Null dargestellt; echte Nullwerte bleiben erhalten. PF/DDT werden weder
zwischen Sets gemittelt noch zwischen System und Exchange vermischt.

## Gefundene und lokal behobene Fehler

| Fehler | Korrektur | Nachweis |
|---|---|---|
| Deaktiviertes Common führte zu einem internen QA-Fehler | Alle acht gemeldeten Typen werden mit den tatsächlich angewendeten Einstellungen verglichen | Alle 256 Aktivierungskombinationen, fehlende Flags und echte Abweichungen getestet |
| QA-Zähler drifteten bei Statuswechseln; Fehler konnten aus der Vorschau verschwinden | Aktuelle Pass/Fail-Zähler und priorisierte Fehleranzeige | Pass → Fail → Pass und begrenzte Vorschau getestet |
| Historische Teilläufe verloren Run-ID und Fortschrittsmetadaten | Fortschritt bleibt an den eingefrorenen Lauf und dessen Symbolmenge gebunden | Mehrere Teilläufe, entfernte Altsymbole und konkurrierende Generationen getestet |
| Ein neuer Auftrag wartete auf das Ende langer Symbolberechnungen | Auch innere Fortschritts-Callbacks erkennen überholte Generationen | Veraltete Ergebnisse überschreiben den neuen Katalog nicht |
| Mehrere temporäre Replay-Datensätze erhöhten den Speicherdruck | Bei busy/overload nur ein Replay-Worker | Workergrenzen und vollständige Engine-Suite bestanden |
| Temporärer Fehler bei Statistik-Wartung unter gleichzeitigem Schreiben | Wartungszugriffe warten begrenzt bis zwei Sekunden; Engine-Timeout bleibt kurz | Echter SQLite-Schreibkonflikt mit anschließender intakter Sicherung getestet |
| Lange Set-Namen verbreiterten die mobile Startseite | Explizites einspaltiges Raster und schrumpfbare Namensspalten | DOM-Vorschau 390/390 Pixel; vollständiger Browserlauf des Quellcodefixes noch offen |
| Gebaute Vorschau beantwortete `/stats.json` mit 404 | Statistikroute vor dem Nitro-Handler bedienen | Typprüfung, Build und Verträge bestanden; Browserabnahme des Fixes noch offen |

## Abgeschlossene Tests

Implementierungsstand: `8bca87130eb785da5cd0a240557b39b0d036b9c6`.

| Prüfung | Ergebnis |
|---|---|
| Python-Gesamtsuite | 191 bestanden, 25,68 Sekunden |
| Engine-Prüfungen | 399/399 bestanden |
| Release-Verträge | 13 bestanden |
| JavaScript | 191 bestanden, 4 bestehende Prüfungen auf nicht vorhandene Skill-Dateien übersprungen |
| TypeScript | 54 bestanden, einschließlich 5 neuer Diagrammtests |
| Typprüfung, ESLint, Produktionsbuild | Bestanden |
| Git-Whitespace-Prüfung | Bestanden |
| Frisch eingerichtete Python-Umgebung auf dem Server, Stand 9fb4aac | 185 Tests bestanden |
| Native Redis-Prüfung auf dem Server | 16 Tests plus echter MEMORY-USAGE-/Übergrößenschutz bestanden |

Die native Redis-Prüfung verwendete einen eigenen temporären Prozess ohne
TCP-Port und ohne Persistenz. Vor dem Leeren der Testdatenbank wurden Prozess-ID
und Port 0 geprüft. Die gemeinsame produktive Redis-Datenbank wurde nicht geleert.
Das belegt die Redis-Funktionalität, aber keinen unbegrenzten Dauerbetrieb.

Der zuvor abgeschlossene Marktdaten-Replay bleibt dokumentiert im
[12-Stunden-Prüfbericht](../validation-20260908-system/verification.md):
43.680 Sets, 131.040 Symbol-/Konfigurationsevaluierungen, alle acht
Indikationstypen, 62,89 Sekunden. Der getrennte Offline-Lasttest erzeugte 500
eindeutige simulierte Orders aus 250 Sets mit geprüften Normal-/Block-Volumina.
Diese 500 Orders sind **kein** Nachweis für 500 geöffnete Exchange-Orders.

## Tatsächliche Serverabnahme

Die bereits konkret freigegebene Version 9fb4aac wurde mit gesichertem Quellcode,
Git-Historie, Konfiguration und vorhandenen Zustandsdateien neu installiert.
Das Installationsskript lief erfolgreich mit `--no-live`.
Der vorher bestehende X01-Live-Prozess blieb bei PID 3818957 unverändert.

X02 wurde als VST-Verbindung mit dem Endpunkt
`https://open-api-vst.bingx.com` geprüft. Ein vorhandener Rückblick von
6.180 Minuten entsprach 103 Stunden und führte zu hohem Speicherverbrauch.
Für den gewünschten Test wurden 720 Minuten und ein Rechen-Worker gespeichert;
die ursprüngliche Einstellung wurde vorher gesichert. Ein kontrollierter
VST-Neustart erfolgte am 09.09.2026 um 00:56:51 UTC, neue PID 3863988.

| Neustartprüfung | Ergebnis |
|---|---|
| Neue Sitzung erkannt | Ja |
| Gespeicherte Statistik-Summen unverändert | Ja, einschließlich aller 56 zuvor erfassten Abschlüsse |
| Sitzungszähler | 1 → 2 |
| Abstürze / unbereinigte Stops | 0 |
| Recoveries | 0 |
| Vorhandener REST-Anfragezähler erhalten | Ja |
| VST-Statistik-Backup | SQLite-Sicherung erfolgreich und geprüft |
| Live-Prozess X01 | Kein Start, Stop oder Neustart durch diese Arbeit |

Die unmittelbar nach dem Neustart gemessenen 1.280 MiB sind eine Anlaufmessung,
kein Dauerverbrauch. Nach rund zwei Minuten wurden 2.284 MiB gemessen; der
12-Stunden-Replay verarbeitete zu diesem Zeitpunkt 5/50 Symbole.
28.080 Sets waren im vollständigen Katalog vorhanden. Der Redis-Cache lag bei
295 Sets und rund 4,3 MiB; seine Grenze blieb 350 → 280 neueste Einträge.
Bei geringer Wiederverwendung wurden Berechnungen lokal ausgeführt, um
unnötige Redis-/AOF-Schreiblast zu vermeiden.

Die letzte [Servermessung](qa/vst-acceptance.json) um **01:08:28 UTC** erfasste
nach rund 11,5 Minuten **12/50** abgeschlossene Symbole, 2.961 MiB Prozessspeicher,
einen Kern mit rund 100 % Auslastung und 1,01 MiB Statistikdatenbank. Beide
Trading-Dienste waren aktiv, ohne automatischen Neustart; Fehler-, Crash- und
Recovery-Zähler blieben bei null. Der Lastregler meldete weiterhin `overload`.
Der vollständige 50-Symbol-Lauf war somit noch nicht abgeschlossen und ein
stabiler Dauerverbrauch ist noch nicht belegt. Die separaten QA-Webserver und
der QA-Browser wurden anschließend beendet, der VST-Dienst blieb aktiv.

Der separate VST-REST-Positionsabruf meldete **0 offene Exchange-Positionen**.
Die gespeicherte Mindest-PF-Schwelle 1,30 blieb aktiv. Die gewünschte hohe Anzahl
echter Demo-Orders ist noch nicht nachgewiesen. Bisherige historische Abschlüsse
wurden nicht als neue Orders dieser Testsitzung gezählt.

Normal (General) bleibt für neue Profile standardmäßig deaktiviert. Das
ausdrücklich gespeicherte `true` des vorhandenen VST-Profils wurde beibehalten.
Die unabhängigen internen General-Berechnungen bleiben in beiden Fällen aktiv.

## Browserprüfung und Grenzen

Browserprüfungen auf dem Server verwendeten exakt 9fb4aac und eine isolierte,
nur lesbare Testdatenquelle. Desktop- und mobile Screenshots wurden angesehen.
Die Auswahl System/Exchange → Break → TP 0,3 % → DCA zeigte korrekt zwei
Vorschauzeilen aus vier Konfigurationen. System-PF 1,30 und Exchange-PF 0,70
blieben getrennt; Null-Winrate und negative Nettoerwartung blieben sichtbar.
Settings/Overview und Settings/System waren im mobilen Browser bedienbar.

Die Startseitenprüfung fand mobilen Überlauf. Die gebaute Vorschau fand die
Statistik-404-Antwort. Das sind fehlgeschlagene Abnahmeprüfungen, keine grünen
Gates. Zusätzlich war der erste Baseline-Dateipfad falsch; dieser Fehler erklärt
nicht die unabhängig bestätigte 404-Antwort. Die Screenshots unten zeigen den
getesteten älteren Stand, nicht die neuen Diagramme.

- [Desktop-Gruppenauswahl](qa/set-groups-desktop.png)
- [Mobile Gruppenauswahl und Ressourcenanzeige](qa/set-groups-mobile.png)
- [Mobile CSS-Diagnose des korrigierten Set-Rasters](qa/mobile-grid-fix-preview.png)
- [Entwicklungs-Verdikt](qa/dev.json) und [Build-Verdikt](qa/built.json)

Die Ressourcenanzeige wurde zusätzlich am tatsächlichen VST-Dienst geprüft:
persistenter Status, CPU, Speicher, Datenbankgröße, Requests, Recoveries,
Abstürze und neue Sitzungsdauer waren sichtbar. Die vorübergehende CSS-Diagnose
ersetzt nicht die ausstehende Abnahme des committeten Quellcodes.

## Sicherung, Veröffentlichung und offene Schritte

Der Server enthält das verifizierte Archiv und Git-Bundle für 9fb4aac unter
`/var/backups/cts-g-release/9fb4aac/`; Sicherungen vor Installation liegen
im Unterverzeichnis `preinstall`. Zugangsdaten und rohe Kontodaten bleiben
außerhalb dieses Berichts und außerhalb der neuen Git-Änderungen.

Die automatische Freigabeprüfung lehnte die anschließende Übertragung des neuen
Diagrammstands d2e2cbe an dessen neues Release-Verzeichnis ab: Der vorherige
konkrete Payload-/Zielumfang bezog sich auf 9fb4aac. Diese Ablehnung wurde nicht
über einen anderen Transport oder Zielpfad umgangen.

Offen bleiben die freigegebene Übertragung des endgültigen neuen Release-Stands,
dessen Desktop-/Mobil- und Build-Abnahme, vollständige aktuelle
50-Symbol-VST-Abnahme mit korrekter Order-/Kontrollabdeckung sowie öffentliche
Veröffentlichung und Merge nach bestandenen Prüfungen. Es gibt noch keinen
öffentlichen Push/PR/Merge für diese Feature-Commits und keine Zusage
unterbrechungsfreien unbegrenzten Betriebs.
