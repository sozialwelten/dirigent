#!/usr/bin/env python3
"""Dirigent: lässt auf medienlabor die langen Arbeiten für die Grafikkarten nacheinander laufen.

Aufruf:
  dirigent.py start [--bis HH:MM] [--ohne-lesesaal]
                        im Hintergrund starten; zuerst der Lesesaal-Tagesbetrieb, danach die
                        Bestände im Wechsel, bis die Uhrzeit erreicht oder alles erledigt ist
  dirigent.py stand     zeigen, ob er läuft, was gerade dran ist und wie viel noch offen ist
  dirigent.py stopp     nach dem laufenden Schritt anhalten; keine neue Aufgabe mehr
  dirigent.py log       das Log fortlaufend anzeigen (Strg+C beendet nur die Anzeige)
  dirigent.py umbenennen [--ausfuehren]
                        archivierte Kursaufnahmen nach dem Kursplan benennen (ohne Schalter nur anzeigen)
  dirigent.py profil [--probe]
                        den Fortschritt jetzt in die Profilfelder von @lesesaal schreiben (--probe: nur zeigen)

Ablauf eines Laufs:
  1. Neue Kursaufnahmen nach dem Kursplan benennen (Datum_Fach_Dozent) und mit noScribe transkribieren (ohne Fenster, Einstellungen aus
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

Überzogene Blöcke: Abschrift, Anreicherung und Aufarbeitung enden normalerweise selbst an der
Blockgrenze, der Lesesaal-Tagesbetrieb vor [zeit] bis. Hängt ein Schritt und überzieht seine
Grenze um mehr als [zeit] nachlauf_minuten, beendet ihn der Dirigent samt aller Unterprozesse:
erst SIGINT, nach je einer Minute SIGTERM und SIGKILL. Die Programme setzen beim nächsten
Block dort fort, wo ihr Stand steht. Kursaufnahmen haben keine solche Grenze.

Am Ende jedes Laufs: Bericht als Direktnachricht ([nachricht]) und der Fortschritt als Balken
in den Profilfeldern des Lesesaal-Kontos ([profil]), beides über Skripte des Lesesaals.

Der Dirigent fährt den Rechner nie herunter und startet nichts von selbst. Jede Aufgabe
nutzt ihre eigene Sperre, Stoppdatei und Temperaturpause. Die Ausgaben der Aufgaben landen
je Tag und Aufgabe unter DATEN/logs/, die Zeilen des Dirigenten in DATEN/dirigent.log.
"""

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

PROJEKT = Path(__file__).resolve().parent
BERICHT_ZEICHEN = 470  # nachricht.py setzt „@konto “ davor und schneidet nach 490 Zeichen ab


def lade_konfiguration():
    """config.toml aus dem Repo, darüber die optionale config.lokal.toml für Einstellungen, die nicht ins Repo gehören.

    Abschnitte der lokalen Datei ergänzen die der config.toml, gleiche Schlüssel überschreiben sie."""
    with open(PROJEKT / "config.toml", "rb") as datei:
        konf = tomllib.load(datei)
    lokal = PROJEKT / "config.lokal.toml"
    if lokal.exists():
        with open(lokal, "rb") as datei:
            for abschnitt, werte in tomllib.load(datei).items():
                if isinstance(werte, dict):
                    konf.setdefault(abschnitt, {}).update(werte)
                else:
                    konf[abschnitt] = werte
    return konf


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
        self.kursplan_pfad = Path(kurs["kursplan"]) if kurs.get("kursplan") else None
        self.nachlauf = timedelta(minutes=int(konf["zeit"].get("nachlauf_minuten", 15)))
        # für den Bericht am Ende eines Laufs
        self.bloecke = []      # (Aufgabe, Sekunden)
        self.vorfaelle = []    # beendete Schritte, Exit-Codes ungleich 0, Warnungen
        self.beendet = False   # wurde der letzte Schritt vom Dirigenten beendet?

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

    def aufgabe_ausfuehren(self, name, befehl, ordner, frist=None):
        """Langer Aufruf; Ausgabe ins Tageslog der Aufgabe. Gibt (Exit-Code, Sekunden) zurück.

        Mit frist wird der Aufruf beendet, wenn er frist plus nachlauf überschreitet. Er läuft dafür
        in einer eigenen Prozessgruppe, damit auch Unterprozesse wie lftp oder ffmpeg mit enden."""
        log = self.daten / "logs" / f"{datetime.now():%Y-%m-%d}_{name}.log"
        beginn = time.monotonic()
        self.beendet = False
        with open(log, "a", encoding="utf-8") as datei:
            datei.write(f"\n===== {datetime.now():%d.%m.%Y %H:%M} {' '.join(befehl)}\n")
            datei.flush()
            prozess = subprocess.Popen(befehl, cwd=ordner, stdout=datei, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONUNBUFFERED="1"),
                                       start_new_session=frist is not None)
            code = self.abwarten(name, prozess, frist + self.nachlauf if frist else None, datei)
        return code, time.monotonic() - beginn

    def abwarten(self, name, prozess, grenze, datei):
        """Wartet auf den Prozess. Ist die Grenze überschritten, wird seine Prozessgruppe beendet."""
        if grenze is None:
            return prozess.wait()
        while True:
            try:
                return prozess.wait(timeout=30)
            except subprocess.TimeoutExpired:
                if datetime.now() >= grenze:
                    break
        minuten = self.nachlauf.total_seconds() / 60
        self.log(f"  {name} überzieht die Grenze um mehr als {minuten:.0f} min und wird beendet")
        self.vorfaelle.append(f"{name} über Grenze {grenze - self.nachlauf:%H:%M}, um {datetime.now():%H:%M} beendet")
        self.beendet = True
        datei.write(f"\n===== {datetime.now():%d.%m.%Y %H:%M} vom Dirigenten beendet, "
                    f"Grenze {grenze:%H:%M} überschritten\n")
        datei.flush()
        code = None
        for zeichen in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(prozess.pid, zeichen)
            except ProcessLookupError:
                break
            try:
                code = prozess.wait(timeout=60)
                break
            except subprocess.TimeoutExpired:
                self.log(f"  {name} reagiert nicht auf {zeichen.name}")
        if code is None:
            code = prozess.wait()
        try:
            os.killpg(prozess.pid, signal.SIGKILL)  # übrig gebliebene Unterprozesse
        except ProcessLookupError:
            pass
        self.log(f"  {name} beendet mit Exit-Code {code}")
        return code

    # Bestände

    def offen(self, name):
        """Wie viel bei einer Aufgabe noch offen ist, oder None, wenn es sich nicht feststellen lässt."""
        return self.zaehlen(name)[0]

    def zaehlen(self, name):
        """(offen, erledigt) einer Aufgabe; was sich nicht feststellen lässt, ist None.

        erledigt gibt es nur, wenn die --stand-Ausgabe des Programms eine Zahl vor „erledigt“ nennt."""
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
            return len(self.aufnahmen()), None
        else:
            return None, None
        if name == "aufarbeitung":
            fertig = re.search(r"Aufarbeitung \(A\): (\d+) erledigt", text)
        else:
            fertig = re.search(r"(\d+) erledigt", text)
        return (int(treffer.group(1)) if treffer else None), (int(fertig.group(1)) if fertig else None)

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

    def lesesaal_tagesbetrieb(self, bis):
        self.zustand(aufgabe="lesesaal", seit=datetime.now().isoformat(timespec="seconds"))
        self.log("Lesesaal-Tagesbetrieb")
        code, sek = self.aufgabe_ausfuehren(
            "lesesaal", [self.python(self.lesesaal), "tagesbetrieb.py", "--ohne-archiv"], self.lesesaal, frist=bis)
        self.log(f"  Lesesaal fertig nach {sek / 60:.0f} min, Exit-Code {code}")
        self.bloecke.append(("lesesaal", sek))
        if code and not self.beendet:
            self.vorfaelle.append(f"lesesaal Exit-Code {code} ({datetime.now():%H:%M})")

    def fuehre(self, name, ende):
        minuten = max(1, int((ende - datetime.now()).total_seconds() // 60))
        self.zustand(aufgabe=name, seit=datetime.now().isoformat(timespec="seconds"),
                     block_bis=ende.isoformat(timespec="minutes"))
        self.log(f"{name} bis {ende:%H:%M}")
        if name == "abschrift":
            code, sek = self.aufgabe_ausfuehren(
                name, [self.python(self.zettelkasten), "abschreiben.py", "--minuten", str(minuten)], self.zettelkasten,
                frist=ende)
        elif name == "anreicherung":
            code, sek = self.aufgabe_ausfuehren(
                name, [self.python(self.zettelkasten), "anreichern.py", "--minuten", str(minuten)], self.zettelkasten,
                frist=ende)
        elif name == "aufarbeitung":
            code, sek = self.aufgabe_ausfuehren(
                name, [self.python(self.lesesaal), "tagesbetrieb.py", "--nur-archiv", "--archiv-bis", f"{ende:%H:%M}"],
                self.lesesaal, frist=ende)
        elif name == "kursaufnahmen":
            code, sek = self.kursaufnahmen(ende)
        else:
            self.log(f"  unbekannte Aufgabe {name}, übersprungen")
            return 0.0
        self.log(f"  {name} fertig nach {sek / 60:.0f} min, Exit-Code {code}")
        self.bloecke.append((name, sek))
        if code and not self.beendet:
            self.vorfaelle.append(f"{name} Exit-Code {code} ({datetime.now():%H:%M})")
        return sek

    # Namen nach dem Kursplan

    def kursplan(self):
        """Datum -> Namensteil, etwa "2026-11-17" -> "Finanzmanagement_Hey"."""
        if not self.kursplan_pfad or not self.kursplan_pfad.exists():
            return {}
        zeilen = [z for z in self.kursplan_pfad.read_text(encoding="utf-8").splitlines() if z and not z.startswith("#")]
        return {z["datum"]: z["kurz"] for z in csv.DictReader(zeilen, delimiter=";") if z.get("kurz")}

    def namensplan(self, dateien):
        """Gewünschter Name je Datei: Datum, Fach und Dozent; eine zweite Aufnahme desselben Abends bekommt _2."""
        plan = self.kursplan()
        je_tag = {}
        for datei in sorted(dateien, key=lambda p: p.name):
            if re.match(r"\d{4}-\d{2}-\d{2}_", datei.name) and datei.name[:10] in plan:
                je_tag.setdefault(datei.name[:10], []).append(datei)
        neu = {}
        for tag, liste in je_tag.items():
            for nr, datei in enumerate(liste, 1):
                neu[datei] = f"{tag}_{plan[tag]}" + (f"_{nr}" if nr > 1 else "")
        return neu

    def benennen(self):
        """Neue Originale und ihr Transkript in video/ nach dem Kursplan benennen, solange sie nicht archiviert sind."""
        for datei, stamm in self.namensplan(self.aufnahmen()).items():
            if datei.stem == stamm:
                continue
            ziel = datei.with_name(stamm + datei.suffix)
            if ziel.exists() or ziel.with_suffix(".html").exists():
                self.log(f"  {ziel.name} gibt es schon, {datei.name} bleibt, wie es ist")
                continue
            if self.transkript(datei).exists():
                self.transkript(datei).rename(ziel.with_suffix(".html"))
            datei.rename(ziel)
            self.log(f"  umbenannt: {datei.name} -> {ziel.name}")

    def umbenennen_bestand(self, ausfuehren):
        """Schon archivierte Aufnahmen nach dem Kursplan umbenennen: im Archiv (Original, Prüfsumme,
        Transkript) und in video/ (kleine Fassung, Transkript). Ohne ausfuehren nur anzeigen."""
        summen = sorted(self.video_archiv.glob("*.sha256")) if self.video_archiv.exists() else []
        originale = [self.video_archiv / s.name.removesuffix(".sha256") for s in summen]
        plan = self.namensplan(originale)
        aenderungen = [(o, stamm) for o, stamm in plan.items() if o.stem != stamm]
        for original, stamm in aenderungen:
            print(f"{original.name:32} -> {stamm}{original.suffix}")
            if not ausfuehren:
                continue
            paare = [
                (original, original.with_name(stamm + original.suffix)),
                (original.with_suffix(".html"), original.with_name(stamm + ".html")),
                (self.video_quelle / (original.stem + ".mkv"), self.video_quelle / (stamm + ".mkv")),
                (self.video_quelle / (original.stem + ".html"), self.video_quelle / (stamm + ".html")),
                (self.video_ziel / (original.stem + ".mkv"), self.video_ziel / (stamm + ".mkv")),
            ]
            if any(neu.exists() for alt, neu in paare if alt.exists()):
                print("  Ziel gibt es schon, übersprungen")
                continue
            for alt, neu in paare:
                if alt.exists():
                    alt.rename(neu)
            summe_alt = self.video_archiv / f"{original.name}.sha256"
            summe_neu = self.video_archiv / f"{stamm}{original.suffix}.sha256"
            wert = summe_alt.read_text().split()[0]
            summe_neu.write_text(f"{wert}  {stamm}{original.suffix}\n")
            summe_alt.unlink()
        if not aenderungen:
            print("Alles schon nach dem Kursplan benannt.")
        elif not ausfuehren:
            print(f"\n{len(aenderungen)} Aufnahmen. Mit --ausfuehren umbenennen.")

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
        if not os.path.ismount(self.archiv_platte):
            # Ohne Archivplatte fehlen die Prüfsummen, an denen die kleinen Fassungen in video/ zu erkennen sind.
            # Sie sähen dann wie Originale aus und würden ein zweites Mal verkleinert (so geschehen am 24.11.2026).
            self.log(f"  Archivplatte {self.archiv_platte} nicht eingehängt, Kursaufnahmen ausgelassen")
            return 0, time.monotonic() - beginn
        self.benennen()
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

    def bestandsbericht(self):
        """Zu Beginn eines Laufs den Befehl aus [bericht] befehl ausführen, etwa ablage_bericht (Repo werkzeuge).

        Der Befehl steht in der Regel in config.lokal.toml, weil er Pfade enthält, die nicht ins Repo gehören.
        Ein Fehler hier hält den Lauf nicht auf."""
        befehl = self.konf.get("bericht", {}).get("befehl")
        if not befehl:
            return
        try:
            ergebnis = subprocess.run(befehl, capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as fehler:
            self.log(f"  Bestandsbericht fehlgeschlagen: {fehler}")
            return
        zeilen = (ergebnis.stdout + ergebnis.stderr).strip().splitlines() or [f"Exit-Code {ergebnis.returncode}"]
        for zeile in zeilen:
            self.log(f"  Bestandsbericht: {zeile}")

    def platz(self):
        """Freier Platz auf Daten- und Archivplatte, als kurze Zeile."""
        teile = []
        for name, pfad in (("Daten", self.daten), ("Archiv", self.archiv_platte)):
            if name == "Archiv" and not os.path.ismount(pfad):
                teile.append("Archiv nicht eingehängt")
                continue
            try:
                frei = shutil.disk_usage(pfad).free / 1e9
            except OSError:
                continue
            teile.append(f"{name} {frei:,.0f} GB frei".replace(",", "."))
        return ", ".join(teile)

    def tagesbericht(self, beginn, grund, vorher, nachher):
        """Kurzer Bericht über den Lauf als Direktnachricht, über nachricht.py des Lesesaals.

        Die Vorfälle stehen am Ende und werden gekürzt, damit der Bericht in eine Nachricht passt
        (nachricht.py schneidet nach 490 Zeichen ab). Abschalten mit [nachricht] aktiv = false.
        Ein Fehler beim Senden hält nichts auf."""
        if not self.konf.get("nachricht", {}).get("aktiv", True):
            return
        zeilen = [f"Dirigent {beginn:%d.%m.} {beginn:%H:%M} bis {datetime.now():%H:%M}, {grund}"]
        je = {}
        for name, sek in self.bloecke:
            anzahl, summe = je.get(name, (0, 0))
            je[name] = (anzahl + 1, summe + sek)
        if je:
            zeilen.append("Blöcke: " + ", ".join(
                f"{name} {summe / 60:.0f} min in {anzahl} Blöcken" if anzahl > 1 else f"{name} {summe / 60:.0f} min"
                for name, (anzahl, summe) in je.items()))
        zeilen.append("Offen:")
        for name in vorher:
            a, b = vorher[name], nachher.get(name)
            zeilen.append(f"- {name} {'?' if a is None else a} → {'?' if b is None else b}")
        zeilen.append(self.platz())
        zeilen += self.vorfall_zeilen(BERICHT_ZEICHEN - len("\n".join(zeilen)) - 1)
        text = "\n".join(zeilen)
        try:
            erg = subprocess.run([self.python(self.lesesaal), "nachricht.py", "--kennung",
                                  f"dirigent-{beginn:%Y%m%d%H%M}"], cwd=self.lesesaal, input=text,
                                 capture_output=True, text=True, timeout=120)
            meldung = (erg.stdout + erg.stderr).strip().splitlines()
            self.log(f"  Bericht: {meldung[-1] if meldung else f'Exit-Code {erg.returncode}'}")
        except (OSError, subprocess.TimeoutExpired) as fehler:
            self.log(f"  Bericht fehlgeschlagen: {fehler}")

    def vorfall_zeilen(self, platz):
        """Vorfälle als Zeilen, so viele, wie in platz Zeichen passen; der Rest als „und N weitere“."""
        if not self.vorfaelle:
            return ["Keine Vorfälle."]
        zeilen = ["Vorfälle:"]
        for nr, vorfall in enumerate(self.vorfaelle):
            rest = len(self.vorfaelle) - nr - 1
            reserve = len(f"\n- und {rest} weitere, siehe Protokoll") if rest else 0
            zeile = f"- {vorfall}"
            if len("\n".join(zeilen + [zeile])) + reserve > platz:
                zeilen.append(f"- und {len(self.vorfaelle) - nr} weitere, siehe Protokoll")
                break
            zeilen.append(zeile)
        return zeilen

    def profil_felder(self, zaehlung, beginn, ende):
        """Zeilen „Name|Wert“ für profil.py: je Aufgabe aus [profil] felder ein Balken, dazu der letzte Lauf."""
        einst = self.konf.get("profil", {})
        gesamt_vorgabe = einst.get("gesamt", {})
        hoechst = self.zustand_lesen().get("hoechst", {})
        zeilen = []
        for name in einst.get("felder", ["abschrift", "anreicherung", "aufarbeitung"])[:3]:
            offen, erledigt = zaehlung.get(name, (None, None))
            if offen is None:
                continue
            hoechst[name] = max(hoechst.get(name, 0), offen)
            gesamt = gesamt_vorgabe.get(name) or (offen + erledigt if erledigt is not None else hoechst[name])
            anteil = min(1.0, max(0.0, (gesamt - offen) / gesamt)) if gesamt else 1.0
            voll = round(anteil * 10)
            zeilen.append(f"{name.capitalize()}|{'█' * voll}{'▒' * (10 - voll)} {anteil * 100:.0f} % · "
                          f"{offen:,} offen".replace(",", "."))
        self.zustand(hoechst=hoechst)
        zeilen.append(f"Letzter Lauf|{beginn:%d.%m.} {beginn:%H:%M} bis {ende:%H:%M}")
        return zeilen

    def profil_aktualisieren(self, zaehlung, beginn, ende, probe=False):
        """Fortschritt in die Profilfelder von @lesesaal, über profil.py des Lesesaals.

        Abschalten mit [profil] aktiv = false. Ein Fehler hält nichts auf."""
        if not self.konf.get("profil", {}).get("aktiv", True):
            return
        text = "\n".join(self.profil_felder(zaehlung, beginn, ende))
        try:
            erg = subprocess.run([self.python(self.lesesaal), "profil.py", *(["--probe"] if probe else [])],
                                 cwd=self.lesesaal, input=text, capture_output=True, text=True, timeout=120)
            meldung = (erg.stdout + erg.stderr).strip().splitlines()
            if probe:
                print("\n".join(meldung))
            self.log(f"  Profil: {meldung[-1] if meldung else f'Exit-Code {erg.returncode}'}")
        except (OSError, subprocess.TimeoutExpired) as fehler:
            self.log(f"  Profil fehlgeschlagen: {fehler}")

    def zustand_lesen(self):
        return json.loads(self.zustand_pfad.read_text(encoding="utf-8")) if self.zustand_pfad.exists() else {}

    def lauf(self, bis, ohne_lesesaal):
        beginn = datetime.now()
        self.stoppdatei.unlink(missing_ok=True)
        self.zustand(pid=os.getpid(), beginn=datetime.now().isoformat(timespec="seconds"),
                     bis=bis.isoformat(timespec="minutes"), aufgabe="", seit="", block_bis="")
        self.log(f"===== Dirigent, bis {bis:%H:%M}")
        if not os.path.ismount(self.archiv_platte):
            self.log(f"  Achtung: Archivplatte {self.archiv_platte} nicht eingehängt, Kursaufnahmen werden ausgelassen")
            self.vorfaelle.append("Archivplatte beim Start nicht eingehängt")
        self.bestandsbericht()
        reihe = self.konf["aufgaben"]["reihe"]
        vorher = {name: self.offen(name) for name in reihe}
        self.einsortieren()
        self.benennen()
        self.neue_transkripte(bis)
        if not ohne_lesesaal and not self.gestoppt() and datetime.now() < bis:
            self.lesesaal_tagesbetrieb(bis)
        block = timedelta(minutes=int(self.konf["zeit"].get("block_minuten", 90)))
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
        zaehlung = {name: self.zaehlen(name) for name in reihe}
        self.tagesbericht(beginn, grund, vorher, {name: z[0] for name, z in zaehlung.items()})
        self.profil_aktualisieren(zaehlung, beginn, datetime.now())
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
    if not os.path.ismount(dirigent.archiv_platte):
        print(f"Achtung: Archivplatte {dirigent.archiv_platte} nicht eingehängt, Kursaufnahmen werden ausgelassen.\n"
              f"Gehäuse aus- und wieder einschalten, dann: sudo mount {dirigent.archiv_platte}")


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


def befehl_umbenennen(dirigent, args):
    if laeuft(dirigent):
        raise SystemExit("Der Dirigent läuft gerade. Erst nach seinem Ende umbenennen.")
    dirigent.umbenennen_bestand(args.ausfuehren)


def befehl_profil(dirigent, args):
    """Profilfelder jetzt schreiben, mit dem letzten Lauf aus zustand.json."""
    z = dirigent.zustand_lesen()
    try:
        beginn, ende = datetime.fromisoformat(z["beginn"]), datetime.fromisoformat(z["ende"])
    except (KeyError, ValueError):
        beginn = ende = datetime.now()
    zaehlung = {name: dirigent.zaehlen(name) for name in dirigent.konf["aufgaben"]["reihe"]}
    dirigent.profil_aktualisieren(zaehlung, beginn, ende, probe=args.probe)


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
    umb = unter.add_parser("umbenennen", help="archivierte Kursaufnahmen nach dem Kursplan benennen")
    umb.add_argument("--ausfuehren", action="store_true", help="wirklich umbenennen, sonst nur anzeigen")
    prof = unter.add_parser("profil", help="Fortschritt jetzt in die Profilfelder von @lesesaal schreiben")
    prof.add_argument("--probe", action="store_true", help="nur zeigen, nichts ändern")
    args = parser.parse_args()
    dirigent = Dirigent(lade_konfiguration())
    {"start": befehl_start, "lauf": befehl_lauf, "stand": befehl_stand, "stopp": befehl_stopp,
     "log": befehl_log, "umbenennen": befehl_umbenennen, "profil": befehl_profil, None: befehl_stand}[args.befehl](dirigent, args)


if __name__ == "__main__":
    main()
