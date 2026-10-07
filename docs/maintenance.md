# Wartung der Bridge

Diese Seite ist der Einstieg fuer einen konkreten Auftrag zur Implementierung,
Diagnose oder zum Betrieb der Bridge. Fuer API-Nutzung und Recherche genuegt
die [Nutzungsanleitung](../README.md).

Vor Arbeiten am Dienst die aufgabenbezogenen Unterlagen gezielt lesen:

- [Betriebsregeln und Architektur](operations/session-context.md)
- [Setup-Referenz](operations/setup-reference.md)
- [Ergaenzende Betriebsreferenzen](operations/additional-references.md)
- [Architektur-Entscheidungen](adr/)

Die Setup-Referenz enthaelt auch historische Anleitungen. Massgeblich sind die
aktuellen Betriebsregeln und Architektur-Entscheidungen; Live-Zustand erneut pruefen.
Bestehende Freigaben bleiben verbindlich. Keine eigenmaechtigen Eingriffe oder
manuellen Konfigurationsaenderungen auf laufenden Hosts. Deploys nur ueber den
vorgesehenen Repository-Prozess aus einem freigegebenen, committeten Stand.

## Git-Identitaet in parallelen Worktrees

`user.name` und `user.email` werden pro Sitzung im eigenen Worktree gesetzt,
mit `git config --worktree user.name ...` und `git config --worktree user.email ...`.
Dafuer aktiviert das Repository `extensions.worktreeConfig=true`.
Die gemeinsame Repo-Konfiguration enthaelt keine Sitzungsidentitaet;
`user.useConfigOnly=true` verhindert eine automatisch erratene Identitaet.
Vor dem Commit `git var GIT_AUTHOR_IDENT` und
`git config --show-origin --get-regexp '^user\.'` pruefen.
`git config --local user.name/email` gilt dagegen fuer alle Worktrees.
Bereits veroeffentlichte Commits werden wegen einer falschen Identitaet nicht
umgeschrieben; die Herkunftsberichtigung gehoert in den zugehoerigen Befund.

## Erkunder im Deploy

Auf dem Dev-Ziel gehoeren Ausgang, drei Plaetze und Leitstand zu einer
Release-Gruppe. Auch eine explizite Auswahl eines einzelnen Erkunder-Dienstes
rollt diese Gruppe aus. Das gemeinsame Image wird einmal mit `GIT_COMMIT`
gebaut; derselbe SHA wird bei `compose up` fuer den Image-Tag verwendet.
Vor jedem Container-Eingriff wartet der Deploy am Leitstand auf Leerlauf:
`ERKUNDER_DEPLOY_WAIT_S` setzt die Frist in Sekunden (Default 900, 0 prueft
sofort). Laufende Bericht-IDs erscheinen im Log. Bei Fristablauf, nicht
pruefbarem Zustand oder unerreichbarem Leitstand scheitert der Deploy laut;
kein Container wird gestoppt, nur der Checkout wird zurueckgesetzt.

`POST /deploy/pruefen` prueft laufende Berichte unter derselben Sperre wie
`/start` und schliesst die Annahme neuer Berichte erst bei Leerlauf. Dadurch
kann zwischen Pruefung und Stop kein neuer Bericht hineinrutschen. Die
Erkunder-Gruppe kommt danach zuerst, der Leitstand startet nach den Plaetzen.
Auch Rollback prueft einen noch laufenden Leitstand vor dem Stop. Fehler beim
Freigeben der Annahme (`DELETE /deploy/pruefen`) bleiben laut; ein abgerissener
Deploy nach erfolgreicher Sperre verlangt eine bewusste Freigabe oder Neustart.
Alle Deploy-Routen verlangen den internen Schluessel.

Ein Altimage ohne dieses Deploy-Protokoll wird bewusst abgewiesen: reine
Dateizaehlung koennte einen neuen Bericht zwischen Pruefung und Stop uebersehen.
Die erstmalige Einfuehrung des Protokolls braucht daher eine separat geplante,
freigegebene Umstellung bei gesperrten Auftraggebern; dieser Deploy-Pfad hat
keinen unsicheren Altimage-Bypass.

Der Job-Executor parkt Transportfehler und HTTP 502/503/504 ueber den bestehenden
Registry-Vertrag `DEPENDENCY_UNAVAILABLE_STATUS` (424). Der Watchdog uebernimmt
den gleichen Job nach 60 Sekunden erneut, hoechstens 240-mal. `/start` mit
derselben Bericht-ID uebergibt einen frischen Token an B1m: Beim Neustart werden
alte Platzprozesse beendet, abgeschlossene Schritte bleiben erhalten und nur
unterbrochene Schritte laufen erneut. Auch zwischen normalen Statusabfragen
haengt sich der Executor idempotent an, damit ein kurzer, unbemerkter Neustart
nicht auf den Token warten bleibt. Fachliche Abbrueche, ungueltige Antworten
und die Gesamtfrist bleiben endgueltige Fehler.

Ein normaler Gesamtdeploy ueberspringt die Gruppe, wenn alle fuenf Container
gesund sind und ihre tatsaechlichen Image-Commits fuer die Erkunder-Eingaben
mit dem Zielstand uebereinstimmen. Ein SSH-Aufruf prueft diese Voraussetzung;
es gibt dann keinen Imagebau, Neustart oder Health-Wartezyklus. Der Vergleich
umfasst Code, SDK-Parser, Abhaengigkeiten, Docker-/Proxy-Konfiguration und
Compose. Compose-Aenderungen loesen konservativ einen Deploy aus, auch wenn
nur ein anderer Dienst betroffen ist. Unbekannte Labels, fehlende Git-Objekte
oder Container und ungesunde Dienste erlauben kein Ueberspringen. Neue externe
Importe der Erkunder-Daemons muessen im Dockerfile und im Vergleich enthalten
sein; das Image kopiert bewusst nur diese Laufzeitquellen.

Die Gesundheitsprobe prueft nach abgeschlossenem Lifespan eine authentifizierte
HTTP-Anfrage auf eine nicht vorhandene Route (erwartet 404, keine Mutation).
Beim Proxy prueft sie einen lokal verweigerten CONNECT (403, ohne externen
Netzverkehr). Das ist Dienstbereitschaft, kein Modell-/Bericht-End-to-End-Test.
Rollback prueft aeltere Images ohne Docker-Healthcheck mit derselben Probe aus
dem Deploy-Werkzeug. Ein fehlgeschlagener oder nur teilweise gestarteter
Erkunder-Release wird als ganze Gruppe zurueckgerollt; Fehler bleiben laut.
