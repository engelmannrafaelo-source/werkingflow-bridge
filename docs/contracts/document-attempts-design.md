# Dokument-Attempts — Architektur und offene Abnahme

Stand 23.10.2026. Der ursprüngliche Entwurf wurde im Feature-Zweig in drei
Abschnitten umgesetzt; kein Deploy und keine Vollabnahme. Der aktuelle Wire-
und Lebensdauervertrag steht in [document-jobs.md](document-jobs.md).

## Verantwortlichkeiten

1. Der öffentliche Jobstore bindet Owner, Home-Bridge, Payload und Submit-
   Idempotenz. Dokumente verwenden den vorhandenen monotonen Versuchszähler
   als Claim-Fence, ohne lokalen Versuchszähler-Abbruch.
2. `jobs/document.py` führt einen bedingt schreibenden Reconciliation-Runner.
   Er liest bei jedem Wiederanlauf dieselbe private Remote-Identität und hält
   keine zweite, ungesicherte synchrone Ausführung als Fallback bereit.
3. `privacy_service/document_attempts.py` verwaltet dauerhafte Quelldaten und
   Attempts. Atomare JSON-Veröffentlichung plus fsync, Admission-flock und
   Execution-flock schützen konkurrierende Annahmen und Starts. Der Guardian
   erbt den Execution-lock; ein Dienstneustart verliert die Sperre nicht.
4. `document_process.py` ist der unabhängige Linux-Subreaper. Er beobachtet
   Converter-Ende mit WNOWAIT (keine PID-Wiederverwendung vor Gruppenkill),
   beendet die Gruppe und sammelt adoptierte Nachkommen ein. Quittung erst danach.
   Owner-EOF und absolute Gesamtfrist wirken auch ohne lebende HTTP-Anfrage.
5. `document_worker.py` führt dieselben Adapter, ActualText-Reparatur und
   Bildbeschreibung im isolierten Prozess aus. `document_execution.py` markiert
   nur persistente Job-Ausführungen als managed; der synchrone Reaper-Pfad
   und Legacy-Threads behalten ihre Schutzfristen.

## Ungewissheit ist kein Retry-Beleg

Annahme vor Start ist dauerhaft. Ein Crash zwischen Startmarkierung und
Prozessstart kann konservativ unknown hinterlassen. Ein noch laufender Guardian
hält den Lock; ein abgestürzter Guardian ohne Quittung erlaubt keinen neuen
Attempt. SIGKILL des Guardians in derselben äußeren Instanz ist kein nachgewiesener
Prozessbaumstopp. Ein anderer Kernel-Boot/PID-Namespace/PID-1-Start belegt
dagegen den Verlust des alten Containers und führt zu einem lokalen
`service_instance_gone`-Receipt; Anbieterarbeit bleibt gegebenenfalls unbekannt. Das Protokoll lügt nicht darüber, hat dafür aber noch keinen
vollautomatischen infrastrukturellen Wiederanlauf.

Vision-Pending-Dateien werden vor dem Send persistiert und nur nach empfangener
Antwort entfernt. Eine TCP-Unterbrechung bleibt daher unbekannter Anbieter-
Ausgang, auch wenn der lokale Prozess erfolgreich eingesammelt wurde. Es gibt
keine hier verfügbare Anbieter-Status-/Stop-API. Bezahlt getestete genau-einmalige
Vision oder vollständige Teilplanfortsetzung wird ausdrücklich nicht behauptet.

Eine spätere neue Remote-Attempt-Ausführung benötigt belegtes Ende bzw.
Nichtannahme des alten Attempts. Dieser Bau startet nach interrupted/unknown
keine neue Konvertierung; er übernimmt laufende/fertige Attempts und meldet
Unterbrechung bzw. Ungewissheit. Neue fachliche Generation = neuer öffentlicher
Job, nicht Überschreiben von Frist, Quelle oder Abbruchwunsch.

## Auslieferung und Abnahme

Der neue Store-Weg muss vor dem neuen Runner verfügbar sein. Alte generische
Mutatoren sind für Dokumentzeilen gesperrt. Das Dokumentdienst-Volume muss
persistent, privat und über Recreation erhalten sein; Dockerfile deklariert
das Volume, die tatsächlich verwendete GPU-Compose-Bindung ist vor Deploy zu
prüfen. Dev und Prod teilen derzeit diesen Dienst: kein impliziter Dev-only-
Rollout, keine Produktionsfreigabe aus diesem Bau ableiten.

Bisherige Teilprüfungen 31df0a6 bleiben Historie. Sie geben den neuen Code nicht
frei. Opus KI 2 prüft jeden Abschnitt, gestartet durch den Berater, höchstens
zwei Runden. SQL-/Platform-API-Integration und große PDF-/GPU-Kapazität fehlen
noch als Laufbeleg. Ein lokaler CSV-/Kontrollprotokoll-Lauf ersetzt diese nicht.

Pyramidenkontext: konkurrierende Claims und späte Abschlüsse, Cancel-vs-done,
verlorene Submit-/Stop-/Store-Antwort, Dienst-/Worker-/Guardianverlust,
ursprüngliche Gesamtfrist über Workerwechsel, Retention ohne Datenverlust,
großer Rückstau, komprimierte große PDFs und kleine Dateien mit großer
Renderausdehnung. Peak-RSS je Prozess und bei Parallelität unter 2 GB messen.
Kein neuer Unit-/Regressionstest und keine bezahlten KI-Läufe nur zum Testen.


R1-Mindestfixes und die bewusste P2-2-Entscheidung stehen im Wire-Vertrag:
permanente Remote-Ablehnung terminal, alter Protokolljob ohne call_id terminal,
bekannter Attempt plus Remote-404 terminal statt erneutem PUT. Unaufklärbares
unknown wird mit Diagnosecode und Beginn im öffentlichen Fortschritt sichtbar.
ACK/Retention (P2-1) und L4-Lebendigkeit/Fortsetzung bleiben vor Deploy offen.
