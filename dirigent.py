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
  1. Neue Kursaufnahmen mit noScribe transkribieren (ohne Fenster, Einstellungen aus
     [kursaufnahmen]). Das kommt zuerst, weil die Abschrift am Tag nach dem Kurs gebraucht wird.
  2. Lesesaal-Tagesbetrieb ohne Aufarbeitung: neue Folgen, Szene, Termine, Seite, Threads,
     Direktnachricht. Das soll täglich fertig sein.
  3. Danach die Bestände aus [aufgaben] reihe im Wechsel, je höchstens [zeit] block_minuten:
       abschrift      Zettelkasten: Bilder abschreiben (abschreiben.py)
       anreicherung   Zettelkasten: Titel, Bereich, Hashtags, Lesefassung (anreichern.py)
       aufarbeitung   Lesesaal: ältere Folgen ins Archiv (tagesbetrieb.py --nur-archiv)
       kursaufnahmen  je Aufnahme: transkribieren, falls noch nötig; verkleinern (ffmpeg, HEVC
                      über die Grafikkarte); Original mit Prüfsumme auf die Archivplatte kopieren
                      und erst nach bestandener Prüfung auf der Datenplatte löschen. Danach liegt
                      die verkleinerte Fassung unter dem Namen des Originals neben dem Transkript
     Eine Aufgabe ohne offene Arbeit wird übersprungen. Sind alle leer, endet der Lauf.
  4. Ab [zeit] bis beginnt nichts Neues mehr. Der laufende Schritt wird noch fertig.

Der Dirigent fährt den Rechner nie herunter und startet nichts von selbst. Jede Aufgabe
nutzt ihre eigene Sperre, Stoppdatei und Temperaturpause. Die Ausgaben der Aufgaben landen
je Tag und Aufgabe unter DATEN/logs/, die Zeilen des Dirigenten in DATEN/dirigent.log.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
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
        kurs = konf["kursaufnahmen"]
        self.video_quelle = Path(kurs["quelle"])
        self.video_ziel = Path(kurs["klein"])
        self.video_archiv = Path(kurs["archiv"])
        self.archiv_platte = Path(kurs["archiv_platte"])
        self.transkribieren_ab = kurs.get("transkribieren_ab", "")
        self.noscribe = Path(kurs["noscribe"])
        self.noscribe_optionen = list(kurs.get("noscribe_optionen", []))

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
        elif name == "kursaufnahmen":
            return len(self.aufnahmen())
        else:
            return None
        return int(treffer.group(1)) if treffer else None

    def archiviert(self, aufnahme):
        return (self.video_archiv / f"{aufnahme.name}.sha256").exists()

    def aufnahmen(self):
        """Originale auf der Datenplatte; jede ist offen, bis sie im Archiv liegt.

        Nach dem Archivieren liegt unter demselben Namen die verkleinerte Fassung in video/, neben dem
        Transkript. Daran, dass im Archiv eine Prüfsumme zu diesem Namen liegt, ist sie zu erkennen."""
        if not self.video_quelle.exists():
            return []
        return sorted(p for p in self.video_quelle.iterdir()
                      if p.suffix.lower() in (".mkv", ".mp4") and ".teil" not in p.name and not self.archiviert(p))

    def einsortieren(self):
        """Verkleinerte Fassungen archivierter Aufnahmen neben ihr Transkript in video/ legen."""
        if not self.video_archiv.exists() or not self.video_ziel.exists():
            return
        for summe in sorted(self.video_archiv.glob("*.sha256")):
            original = self.video_quelle / summe.name.removesuffix(".sha256")
            klein = self.video_ziel / f"{original.stem}.mkv"
            ziel = original.with_suffix(".mkv")
            if klein.exists() and not ziel.exists() and not original.exists():
                shutil.move(klein, ziel)
                self.log(f"  verkleinerte Fassung nach {ziel}")

    def transkript(self, aufnahme):
        return aufnahme.with_suffix(".html")

    def braucht_transkript(self, aufnahme):
        """Ab dem Datum in [kursaufnahmen] transkribieren_ab; ältere Aufnahmen sind schon von Hand erledigt."""
        if self.transkript(aufnahme).exists():
            return False
        return aufnahme.name[:10] >= self.transkribieren_ab if self.transkribieren_ab else True

    def klein(self, aufnahme):
        return self.video_ziel / f"{aufnahme.stem}.mkv"

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
        elif name == "kursaufnahmen":
            code, sek = self.kursaufnahmen(ende)
        else:
            self.log(f"  unbekannte Aufgabe {name}, übersprungen")
            return 0.0
        self.log(f"  {name} fertig nach {sek / 60:.0f} min, Exit-Code {code}")
        return sek

    def transkribieren(self, aufnahme):
        """noScribe ohne Fenster; das Ergebnis erhält seinen Namen erst, wenn es vollständig ist."""
        teil = aufnahme.with_name(f"{aufnahme.stem}.teil.html")
        self.log(f"  transkribiere {aufnahme.name}")
        code, sek = self.aufgabe_ausfuehren("kursaufnahmen", [
            str(self.noscribe / "venv" / "bin" / "python"), "-m", "noScribe", str(aufnahme), str(teil), "--no-gui",
            *self.noscribe_optionen], self.noscribe)
        if code != 0 or not teil.exists() or teil.stat().st_size < 2000:
            self.log(f"  Transkription fehlgeschlagen: {aufnahme.name}, Exit-Code {code}")
            return False
        teil.rename(self.transkript(aufnahme))
        self.log(f"  Transkript fertig nach {sek / 60:.0f} min: {self.transkript(aufnahme).name}")
        return True

    def neue_transkripte(self, bis):
        """Schritt 1 des Laufs: alle Aufnahmen ohne Transkript, ohne Blockgrenze, aber nicht nach bis beginnen."""
        for aufnahme in self.aufnahmen():
            if self.gestoppt() or datetime.now() >= bis:
                return
            if self.braucht_transkript(aufnahme):
                self.zustand(aufgabe="transkription", seit=datetime.now().isoformat(timespec="seconds"),
                             block_bis="")
                self.transkribieren(aufnahme)

    def verkleinern(self, aufnahme):
        ziel = self.klein(aufnahme)
        teil = self.video_ziel / f"{aufnahme.stem}.teil.mkv"
        self.video_ziel.mkdir(parents=True, exist_ok=True)
        self.log(f"  verkleinere {aufnahme.name}")
        code, _ = self.aufgabe_ausfuehren("kursaufnahmen", [
            "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(aufnahme),
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr",
            "-cq", "30", "-b:v", "0", "-r", "15", "-c:a", "aac", "-b:a", "96k", str(teil)], self.video_ziel)
        if code != 0:
            self.log(f"  Fehler beim Verkleinern von {aufnahme.name}, Exit-Code {code}")
            return False
        dq, dz = self.laenge(aufnahme), self.laenge(teil)
        if dz is None:
            self.log(f"  Ergebnis ohne lesbare Länge: {aufnahme.name}")
            return False
        if dq is not None and abs(dq - dz) > 5:
            self.log(f"  Länge weicht ab ({dq:.0f} s / {dz:.0f} s): {aufnahme.name}")
            return False
        teil.rename(ziel)
        self.log(f"  verkleinert: {ziel.name}, {ziel.stat().st_size / 1e6:.0f} MB, {dz / 60:.0f} min"
                 + ("" if dq is not None else ", Original ohne Längenangabe"))
        return True

    @staticmethod
    def pruefsumme(pfad):
        h = hashlib.sha256()
        with open(pfad, "rb") as datei:
            while block := datei.read(16 * 1024 * 1024):
                h.update(block)
        return h.hexdigest()

    def archivieren(self, aufnahme):
        """Original auf die Archivplatte; gelöscht wird es erst, wenn die Kopie dieselbe Prüfsumme hat."""
        if not os.path.ismount(self.archiv_platte):
            self.log(f"  Archivplatte {self.archiv_platte} nicht eingehängt, {aufnahme.name} bleibt liegen")
            return False
        self.video_archiv.mkdir(parents=True, exist_ok=True)
        groesse = aufnahme.stat().st_size
        if shutil.disk_usage(self.video_archiv).free < groesse + 5e9:
            self.log(f"  Archivplatte voll, {aufnahme.name} bleibt liegen")
            return False
        ziel = self.video_archiv / aufnahme.name
        teil = self.video_archiv / f"{aufnahme.stem}.teil{aufnahme.suffix}"
        self.log(f"  archiviere {aufnahme.name} ({groesse / 1e9:.1f} GB)")
        beginn = time.monotonic()
        try:
            shutil.copyfile(aufnahme, teil)
            os.sync()
            quelle_summe, kopie_summe = self.pruefsumme(aufnahme), self.pruefsumme(teil)
        except OSError as fehler:
            self.log(f"  Kopieren fehlgeschlagen: {fehler}")
            teil.unlink(missing_ok=True)
            return False
        if quelle_summe != kopie_summe:
            self.log(f"  Prüfsumme weicht ab, Kopie verworfen, Original bleibt: {aufnahme.name}")
            teil.unlink(missing_ok=True)
            return False
        teil.rename(ziel)
        (self.video_archiv / f"{aufnahme.name}.sha256").write_text(f"{kopie_summe}  {aufnahme.name}\n")
        if self.transkript(aufnahme).exists():
            shutil.copyfile(self.transkript(aufnahme), self.video_archiv / self.transkript(aufnahme).name)
        aufnahme.unlink()
        self.log(f"  archiviert und geprüft nach {(time.monotonic() - beginn) / 60:.0f} min, Original gelöscht")
        self.einsortieren()
        return True

    def kursaufnahmen(self, ende):
        """Je Aufnahme der Reihe nach: transkribieren, verkleinern, archivieren. Ein begonnener Schritt wird fertig."""
        beginn = time.monotonic()
        for aufnahme in self.aufnahmen():
            for schritt in ("transkript", "klein", "archiv"):
                if self.gestoppt() or datetime.now() >= ende:
                    return 0, time.monotonic() - beginn
                if schritt == "transkript" and self.braucht_transkript(aufnahme):
                    if not self.transkribieren(aufnahme):
                        break
                elif schritt == "klein" and not self.klein(aufnahme).exists():
                    if not self.verkleinern(aufnahme):
                        break
                elif schritt == "archiv":
                    self.archivieren(aufnahme)
        return 0, time.monotonic() - beginn

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
        self.einsortieren()
        self.neue_transkripte(bis)
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
        if name == "kursaufnahmen":
            auf = dirigent.aufnahmen()
            print(f"    davon ohne Transkript {sum(dirigent.braucht_transkript(x) for x in auf)}, "
                  f"noch nicht verkleinert {sum(not dirigent.klein(x).exists() for x in auf)}, "
                  f"Archivplatte {'bereit' if os.path.ismount(dirigent.archiv_platte) else 'nicht eingehängt'}")
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
    if aufgabe in ("transkription", "kursaufnahmen"):
        print("Eine laufende Transkription oder Kopie lässt sich nicht unterbrechen, sie wird fertig.")
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
