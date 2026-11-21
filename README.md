# Dirigent

Lässt auf einem Rechner mit Grafikkarten die langen Arbeiten nacheinander laufen, während niemand davor sitzt: zuerst den Tagesbetrieb des [Lesesaals](https://github.com/sozialwelten/lesesaal), danach im Wechsel die großen Bestände aus [Zettelkasten](https://github.com/sozialwelten/zettelkasten), Lesesaal und Kursaufnahmen. Die Zeit vor dem Rechner bleibt so für das Planen und Bauen frei, und niemand muss auf die Grafikkarte warten.

Der Code ist mit Hilfe eines KI-Assistenten entstanden und wird von Hand getestet.

## Grundsätze

- **Start nur von Hand.** Der Dirigent startet nichts von selbst und fährt den Rechner nie herunter oder hoch.
- **Eine Arbeit nach der anderen.** Die Aufgaben teilen sich die Grafikkarten und laufen nie gleichzeitig. Jede nutzt ihre eigene Sperre, Stoppdatei und Temperaturpause.
- **Anhalten an sicheren Stellen.** Ein Stoppsignal lässt den laufenden Schritt fertig werden, danach beginnt nichts Neues.
- **Feste Uhrzeit als Sicherung.** Ab `[zeit] bis` beginnt keine neue Aufgabe mehr, damit abends die Grafikkarten frei sind und nachts nichts läuft.

## Ablauf

1. Neue Kursaufnahmen mit [noScribe](https://github.com/kaixxx/noScribe) transkribieren, ohne Fenster und mit denselben Einstellungen wie von Hand. Das kommt zuerst, damit die Abschrift am Tag nach dem Kurs vorliegt.
2. Lesesaal-Tagesbetrieb ohne Aufarbeitung: neue Folgen, Szene, Termine, Seite, Threads, Direktnachricht.
3. Danach die Bestände im Wechsel, je höchstens `[zeit] block_minuten`:

| Aufgabe | Was | Aufruf |
|---|---|---|
| abschrift | Bilder aus den Mastodon-Archiven abschreiben | `zettelkasten/abschreiben.py --minuten N` |
| anreicherung | Titel, Bereich, Hashtags und Lesefassung je Einheit | `zettelkasten/anreichern.py --minuten N` |
| aufarbeitung | ältere Folgen ins Archiv des Lesesaals | `lesesaal/tagesbetrieb.py --nur-archiv --archiv-bis HH:MM` |
| kursaufnahmen | je Aufnahme: transkribieren, falls noch nötig; verkleinern (HEVC über die Grafikkarte, 15 Bilder je Sekunde); Original auf die Archivplatte | noScribe, `ffmpeg` mit `hevc_nvenc` |

Das Original einer Kursaufnahme wird auf der Datenplatte erst gelöscht, wenn die Kopie auf der Archivplatte dieselbe SHA-256-Prüfsumme hat. Die Prüfsumme liegt als `.sha256` daneben, ebenso eine Kopie des Transkripts. Ist die Archivplatte nicht eingehängt oder zu voll, bleibt das Original liegen. Halbfertige Ergebnisse tragen `.teil` im Namen und erhalten ihren endgültigen Namen erst, wenn sie vollständig sind.

Eine Aufgabe ohne offene Arbeit wird übersprungen. Sind alle leer, endet der Lauf. Kommt zwei Runden lang keine Aufgabe voran, etwa weil etwas anderes die Grafikkarten belegt, endet er ebenfalls.

## Aufrufe

```
dirigent.py start                im Hintergrund starten, bis zur Uhrzeit aus config.toml
dirigent.py start --bis 15:00    heute nur bis 15 Uhr
dirigent.py start --ohne-lesesaal
                                 den Tagesbetrieb des Lesesaals auslassen
dirigent.py stand                läuft er, was ist dran, was ist offen (auch ohne Befehl)
dirigent.py stopp                nach dem laufenden Schritt anhalten
dirigent.py log                  das Log fortlaufend anzeigen
```

Der Lauf geht weiter, wenn das Terminal geschlossen wird. Die Zeilen des Dirigenten stehen in `DATEN/dirigent.log`, die Ausgaben der Aufgaben je Tag und Aufgabe in `DATEN/logs/`.

## Voraussetzungen

Python 3.11 oder neuer, nur die Standardbibliothek. Lesesaal und Zettelkasten mit ihren eigenen `.venv`, noScribe ab Version 0.7 (Kommandozeile mit `--no-gui`), ffmpeg mit `hevc_nvenc`. Pfade, Uhrzeit, Blocklänge und Reihenfolge stehen in `config.toml`.

## Lizenz

GPL-3.0, siehe `LICENSE`.
