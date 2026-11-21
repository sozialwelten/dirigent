#!/usr/bin/env python3
"""Dirigent: lässt auf medienlabor die langen Arbeiten für die Grafikkarten nacheinander laufen.

Aufruf:
  dirigent.py start [--bis HH:MM] [--ohne-lesesaal]
                        im Hintergrund starten; zuerst der Lesesaal-Tagesbetrieb, danach die
                        Bestände im Wechsel, bis die Uhrzeit erreicht oder alles erledigt ist
  dirigent.py stand     zeigen, ob er läuft, was gerade dran ist und wie viel noch offen ist
  dirigent.py stopp     nach dem laufenden Schritt anhalten; keine neue Aufgabe mehr
  dirigent.py log       das Log fortlaufend anzeigen (Strg+C beendet nur die Anzeige)

Ablauf eines Laufs:
  1. Lesesaal-Tagesbetrieb ohne Aufarbeitung: neue Folgen, Szene, Termine, Seite, Threads,
     Direktnachricht. Das kommt immer zuerst, weil es täglich fertig sein soll.
  2. Danach die Bestände aus [aufgaben] reihe im Wechsel, je höchstens [zeit] block_minuten:
       abschrift      Zettelkasten: Bilder abschreiben (abschreiben.py)
       anreicherung   Zettelkasten: Titel, Bereich, Hashtags, Lesefassung (anreichern.py)
       aufarbeitung   Lesesaal: ältere Folgen ins Archiv (tagesbetrieb.py --nur-archiv)
       videos         Kursaufnahmen verkleinern (ffmpeg, HEVC über die Grafikkarte)
     Eine Aufgabe ohne offene Arbeit wird übersprungen. Sind alle leer, endet der Lauf.
  3. Ab [zeit] bis beginnt nichts Neues mehr. Der laufende Schritt wird noch fertig.

Der Dirigent fährt den Rechner nie herunter und startet nichts von selbst. Jede Aufgabe
nutzt ihre eigene Sperre, Stoppdatei und Temperaturpause. Die Ausgaben der Aufgaben landen
je Tag und Aufgabe unter DATEN/logs/, die Zeilen des Dirigenten in DATEN/dirigent.log.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

PROJEKT = Path(__file__).resolve().parent


def lade_konfiguration():
    with open(PROJEKT / "config.toml", "rb") as datei:
        return tomllib.load(datei)


class Dirigent:
    def __init__(self, konf):
        self.konf = konf
        self.daten = Path(konf["pfade"]["daten"])
        self.daten.mkdir(parents=True, exist_ok=True)
        (self.daten / "logs").mkdir(exist_ok=True)
        self.log_pfad = self.daten / "dirigent.log"
        self.stoppdatei = self.daten / "stopp"
        self.zustand_pfad = self.daten / "zustand.json"
        self.lesesaal = Path(konf["lesesaal"]["ordner"])
        self.zettelkasten = Path(konf["zettelkasten"]["ordner"])
        self.video_quelle = Path(konf["videos"]["quelle"])
        self.video_ziel = Path(konf["videos"]["ziel"])

    # Hilfen

    def log(self, text):
        zeile = f"{datetime.now():%d.%m. %H:%M:%S} {text}"
        print(zeile, flush=True)

    def gestoppt(self):
        return self.stoppdatei.exists()

    def zustand(self, **werte):
        alt = json.loads(self.zustand_pfad.read_text(encoding="utf-8")) if self.zustand_pfad.exists() else {}
        alt.update(werte)
        self.zustand_pfad.write_text(json.dumps(alt, ensure_ascii=False, indent=1), encoding="utf-8")

    def python(self, ordner):
        venv = ordner / ".venv" / "bin" / "python"
        return str(venv if venv.exists() else sys.executable)

    def ausgabe(self, befehl, ordner):
        """Kurzer Aufruf, dessen Ausgabe gebraucht wird (Bestand zählen)."""
        try:
            erg = subprocess.run(befehl, cwd=ordner, capture_output=True, text=True, timeout=900)
            return erg.stdout + erg.stderr
        except (OSError, subprocess.SubprocessError) as fehler:
            return f"FEHLER {fehler}"

    def aufgabe_ausfuehren(self, name, befehl, ordner):
        """Langer Aufruf; Ausgabe ins Tageslog der Aufgabe. Gibt (Exit-Code, Sekunden) zurück."""
        log = self.daten / "logs" / f"{datetime.now():%Y-%m-%d}_{name}.log"
        beginn = time.monotonic()
        with open(log, "a", encoding="utf-8") as datei:
            datei.write(f"\n===== {datetime.now():%d.%m.%Y %H:%M} {' '.join(befehl)}\n")
            datei.flush()
            prozess = subprocess.Popen(befehl, cwd=ordner, stdout=datei, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONUNBUFFERED="1"))
            code = prozess.wait()
        return code, time.monotonic() - beginn

    # Bestände

    def offen(self, name):
        """Wie viel bei einer Aufgabe noch offen ist, oder None, wenn es sich nicht feststellen lässt."""
        if name == "abschrift":
            text = self.ausgabe([self.python(self.zettelkasten), "abschreiben.py", "--stand"], self.zettelkasten)
            treffer = re.search(r"(\d+) offen", text)
        elif name == "anreicherung":
            text = self.ausgabe([self.python(self.zettelkasten), "anreichern.py", "--stand"], self.zettelkasten)
            treffer = re.search(r"(\d+) bereit", text)
        elif name == "aufarbeitung":
            text = self.ausgabe([self.python(self.lesesaal), "tagesbetrieb.py", "--stand"], self.lesesaal)
            treffer = re.search(r"Aufarbeitung \(A\): \d+ erledigt, (\d+) offen", text)
        elif name == "videos":
            return len(self.offene_videos())
        else:
            return None
        return int(treffer.group(1)) if treffer else None

    def offene_videos(self):
        if not self.video_quelle.exists():
            return []
        quellen = sorted(p for p in self.video_quelle.iterdir() if p.suffix.lower() in (".mkv", ".mp4"))
        return [p for p in quellen if not (self.video_ziel / f"{p.stem}.mkv").exists()]

    # Aufgaben

    def lesesaal_tagesbetrieb(self):
        self.zustand(aufgabe="lesesaal", seit=datetime.now().isoformat(timespec="seconds"))
        self.log("Lesesaal-Tagesbetrieb")
        code, sek = self.aufgabe_ausfuehren(
            "lesesaal", [self.python(self.lesesaal), "tagesbetrieb.py", "--ohne-archiv"], self.lesesaal)
        self.log(f"  Lesesaal fertig nach {sek / 60:.0f} min, Exit-Code {code}")

    def fuehre(self, name, ende):
        minuten = max(1, int((ende - datetime.now()).total_seconds() // 60))
        self.zustand(aufgabe=name, seit=datetime.now().isoformat(timespec="seconds"),
                     block_bis=ende.isoformat(timespec="minutes"))
        self.log(f"{name} bis {ende:%H:%M}")
        if name == "abschrift":
            code, sek = self.aufgabe_ausfuehren(
                name, [self.python(self.zettelkasten), "abschreiben.py", "--minuten", str(minuten)], self.zettelkasten)
        elif name == "anreicherung":
            code, sek = self.aufgabe_ausfuehren(
                name, [self.python(self.zettelkasten), "anreichern.py", "--minuten", str(minuten)], self.zettelkasten)
        elif name == "aufarbeitung":
            code, sek = self.aufgabe_ausfuehren(
                name, [self.python(self.lesesaal), "tagesbetrieb.py", "--nur-archiv", "--archiv-bis", f"{ende:%H:%M}"],
                self.lesesaal)
        elif name == "videos":
            code, sek = self.videos(ende)
        else:
            self.log(f"  unbekannte Aufgabe {name}, übersprungen")
            return 0.0
        self.log(f"  {name} fertig nach {sek / 60:.0f} min, Exit-Code {code}")
        return sek

    def videos(self, ende):
        """Verkleinert Aufnahmen, bis die Zeit um ist; eine angefangene Datei wird fertig."""
        beginn = time.monotonic()
        self.video_ziel.mkdir(parents=True, exist_ok=True)
        code = 0
        for quelle in self.offene_videos():
            if self.gestoppt() or datetime.now() >= ende:
                break
            ziel = self.video_ziel / f"{quelle.stem}.mkv"
            teil = self.video_ziel / f"{quelle.stem}.mkv.teil.mkv"
            self.log(f"  verkleinere {quelle.name}")
            code, _ = self.aufgabe_ausfuehren("videos", [
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(quelle),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", "30", "-b:v", "0", "-r", "15", "-c:a", "aac", "-b:a", "96k", str(teil)], self.video_ziel)
            if code != 0:
                self.log(f"  Fehler bei {quelle.name}, Exit-Code {code}")
                continue
            dq, dz = self.laenge(quelle), self.laenge(teil)
            if dz is None:
                self.log(f"  Ergebnis ohne lesbare Länge: {quelle.name}")
                continue
            if dq is not None and abs(dq - dz) > 5:
                self.log(f"  Länge weicht ab ({dq:.0f} s / {dz:.0f} s): {quelle.name}")
                continue
            teil.rename(ziel)
            self.log(f"  fertig: {ziel.name}, {ziel.stat().st_size / 1e6:.0f} MB, {dz / 60:.0f} min"
                     + ("" if dq is not None else ", Original ohne Längenangabe"))
        return code, time.monotonic() - beginn

    def laenge(self, pfad):
        text = self.ausgabe(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                             str(pfad)], pfad.parent).strip()
        try:
            return float(text)
        except ValueError:
            return None

    # Lauf

    def lauf(self, bis, ohne_lesesaal):
        self.stoppdatei.unlink(missing_ok=True)
        self.zustand(pid=os.getpid(), beginn=datetime.now().isoformat(timespec="seconds"),
                     bis=bis.isoformat(timespec="minutes"), aufgabe="", seit="", block_bis="")
        self.log(f"===== Dirigent, bis {bis:%H:%M}")
        if not ohne_lesesaal and not self.gestoppt() and datetime.now() < bis:
            self.lesesaal_tagesbetrieb()
        block = timedelta(minutes=int(self.konf["zeit"].get("block_minuten", 90)))
        reihe = self.konf["aufgaben"]["reihe"]
        ohne_fortschritt = 0
        while not self.gestoppt() and datetime.now() < bis:
            gearbeitet, lange = False, False
            for name in reihe:
                if self.gestoppt() or datetime.now() >= bis:
                    break
                offen = self.offen(name)
                if offen == 0:
                    self.log(f"{name}: nichts offen")
                    continue
                gearbeitet = True
                if self.fuehre(name, min(datetime.now() + block, bis)) > 120:
                    lange = True
            if not gearbeitet:
                self.log("Alle Bestände erledigt.")
                break
            ohne_fortschritt = 0 if lange else ohne_fortschritt + 1
            if ohne_fortschritt >= 2:
                self.log("Zwei Runden ohne Fortschritt: Läuft etwas anderes auf den Grafikkarten? Ende.")
                break
        grund = "Stoppsignal" if self.gestoppt() else ("Uhrzeit erreicht" if datetime.now() >= bis else "fertig")
        self.log(f"===== Ende ({grund})")
        self.zustand(aufgabe="", seit="", block_bis="", ende=datetime.now().isoformat(timespec="seconds"))
        self.stoppdatei.unlink(missing_ok=True)


def sperre_halten(dirigent):
    sperre = open(dirigent.daten / "dirigent.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(sperre, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return sperre


def laeuft(dirigent):
    sperre = sperre_halten(dirigent)
    if sperre is None:
        return True
    sperre.close()
    return False


def uhrzeit(text):
    stunde, _, minute = text.partition(":")
    ziel = datetime.now().replace(hour=int(stunde), minute=int(minute or 0), second=0, microsecond=0)
    return ziel


def befehl_start(dirigent, args):
    if laeuft(dirigent):
        print("Der Dirigent läuft schon. Stand: dirigent stand")
        return
    bis = args.bis or dirigent.konf["zeit"]["bis"]
    if datetime.now() >= uhrzeit(bis):
        print(f"Es ist schon nach {bis}. Mit --bis HH:MM eine spätere Uhrzeit angeben.")
        return
    befehl = [sys.executable, str(Path(__file__).resolve()), "lauf", "--bis", bis]
    if args.ohne_lesesaal:
        befehl.append("--ohne-lesesaal")
    with open(dirigent.log_pfad, "a", encoding="utf-8") as log:
        subprocess.Popen(befehl, cwd=PROJEKT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    print(f"Dirigent gestartet, bis {bis}. Stand: dirigent stand, Log: dirigent log, Anhalten: dirigent stopp")


def befehl_lauf(dirigent, args):
    sperre = sperre_halten(dirigent)
    if sperre is None:
        raise SystemExit("Der Dirigent läuft schon.")
    dirigent.lauf(uhrzeit(args.bis or dirigent.konf["zeit"]["bis"]), args.ohne_lesesaal)


def befehl_stand(dirigent, _args):
    z = json.loads(dirigent.zustand_pfad.read_text(encoding="utf-8")) if dirigent.zustand_pfad.exists() else {}
    if laeuft(dirigent):
        jetzt = z.get("aufgabe") or "zwischen zwei Aufgaben"
        seit = f" seit {z['seit'][11:16]}" if z.get("seit") else ""
        block = f", Block bis {z['block_bis'][11:16]}" if z.get("block_bis") else ""
        print(f"Läuft seit {z.get('beginn', '?')[11:16]}, bis {z.get('bis', '?')[11:16]}. Gerade: {jetzt}{seit}{block}"
              + (", Stoppsignal gesetzt" if dirigent.gestoppt() else ""))
    else:
        print("Läuft nicht." + (f" Letztes Ende: {z['ende'][:16].replace('T', ' ')}" if z.get("ende") else ""))
    print("\nOffen:")
    for name in dirigent.konf["aufgaben"]["reihe"]:
        offen = dirigent.offen(name)
        print(f"  {name:<14} {'?' if offen is None else offen}")
    if dirigent.log_pfad.exists():
        print("\nLetzte Zeilen:")
        for zeile in dirigent.log_pfad.read_text(encoding="utf-8").splitlines()[-6:]:
            print(f"  {zeile}")


def befehl_stopp(dirigent, _args):
    if not laeuft(dirigent):
        print("Der Dirigent läuft nicht.")
        return
    dirigent.stoppdatei.touch()
    z = json.loads(dirigent.zustand_pfad.read_text(encoding="utf-8")) if dirigent.zustand_pfad.exists() else {}
    aufgabe = z.get("aufgabe", "")
    if aufgabe in ("lesesaal", "aufarbeitung"):
        (Path(dirigent.konf["lesesaal"]["daten"]) / "stopp").touch()
    elif aufgabe in ("abschrift", "anreicherung"):
        (Path(dirigent.konf["zettelkasten"]["daten"]) / "stopp").touch()
    print(f"Stoppsignal gesetzt. {aufgabe or 'Der laufende Schritt'} wird noch fertig, dann endet der Dirigent.")


def befehl_log(dirigent, _args):
    dirigent.log_pfad.touch()
    os.execvp("tail", ["tail", "-n", "30", "-f", str(dirigent.log_pfad)])


def main():
    parser = argparse.ArgumentParser(description="Lange Arbeiten auf medienlabor nacheinander laufen lassen")
    unter = parser.add_subparsers(dest="befehl")
    start = unter.add_parser("start", help="im Hintergrund starten")
    lauf = unter.add_parser("lauf", help="im Vordergrund laufen (so startet ihn start)")
    for p in (start, lauf):
        p.add_argument("--bis", metavar="HH:MM", help="ab dieser Uhrzeit nichts Neues mehr beginnen")
        p.add_argument("--ohne-lesesaal", action="store_true", help="den Lesesaal-Tagesbetrieb auslassen")
    unter.add_parser("stand", help="zeigen, was läuft und was offen ist")
    unter.add_parser("stopp", help="nach dem laufenden Schritt anhalten")
    unter.add_parser("log", help="das Log fortlaufend anzeigen")
    args = parser.parse_args()
    dirigent = Dirigent(lade_konfiguration())
    {"start": befehl_start, "lauf": befehl_lauf, "stand": befehl_stand, "stopp": befehl_stopp,
     "log": befehl_log, None: befehl_stand}[args.befehl](dirigent, args)


if __name__ == "__main__":
    main()
