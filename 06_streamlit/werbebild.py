"""Werbebild-Komposition: legt Headline, Call-to-Action und Logo auf ein Basisbild.

Bewusst per Code (Pillow) und NICHT durch das Bildmodell selbst:
  * Rechtschreibung inkl. Umlaute ist garantiert korrekt (Bildmodelle verschreiben
    sich bei Text, besonders auf Deutsch),
  * funktioniert identisch für hochgeladene Produktfotos (z. B. die echte Brezel,
    die nicht von einer KI verändert werden soll) und für KI-generierte Bilder,
  * CI-Farben, Schrift und Logo werden exakt eingehalten,
  * Text ist nachträglich editierbar, ohne das Bild neu (und kostenpflichtig) zu erzeugen,
  * dauert Millisekunden statt Sekunden.
"""

import glob
import io
import os
import re
import urllib.request

from PIL import Image, ImageDraw, ImageFont, ImageOps

# Muss zu WERBEBILD_VERSION_ERWARTET in app.py passen (erkennt veraltete Dateien)
VERSION = 4

# Zielformate in Pixeln laut Plattform-Spezifikation (Instagram/Facebook):
# Feed 4:5 = 1440 x 1800, Reels/Stories 9:16 = 1440 x 2560
FORMATE = {
    "ig_feed": (1440, 1800), "ig_reels": (1440, 2560), "ig_story": (1440, 2560),
    "fb_feed": (1440, 1800), "fb_reels": (1440, 2560), "fb_story": (1440, 2560),
}
# Unterer Bereich, den die App-Oberfläche überdeckt (Antwortfeld, Reels-Buttons/Caption)
# Oben: Profilzeile/Fortschrittsbalken bei Stories und Reels
OBERER_SICHERHEITSBEREICH = {"ig_story": 0.10, "fb_story": 0.10, "ig_reels": 0.08, "fb_reels": 0.08}
UNTERER_SICHERHEITSBEREICH = {"ig_story": 0.14, "fb_story": 0.14, "ig_reels": 0.22, "fb_reels": 0.22}

SCHRIFT_VERZEICHNIS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
GOOGLE_CACHE = os.path.join(SCHRIFT_VERZEICHNIS, "_google")
_google_fehlversuche = set()  # nicht bei jedem Rerun erneut versuchen


def _hex_zu_rgb(farbe: str, fallback=(201, 83, 46)):
    try:
        farbe = (farbe or "").lstrip("#")
        if len(farbe) == 3:
            farbe = "".join(c * 2 for c in farbe)
        return tuple(int(farbe[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def _luminanz(rgb) -> float:
    """Relative Luminanz nach WCAG 2.x."""
    def kanal(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (kanal(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def kontrast(a, b) -> float:
    """WCAG-Kontrastverhältnis (1 bis 21)."""
    la, lb = sorted((_luminanz(a), _luminanz(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _ist_hell(rgb) -> bool:
    return _luminanz(rgb) > 0.4


def _google_font_laden(name: str):
    """Lädt eine Google-Font (fett) einmalig als TTF in fonts/_google/ herunter.
    Funktioniert nur, wenn die Schrift bei Google Fonts existiert und Internet da ist."""
    if not name or name in _google_fehlversuche:
        return None
    ziel = os.path.join(GOOGLE_CACHE, f"{name.replace(' ', '')}-Bold.ttf")
    if os.path.exists(ziel):
        return ziel
    try:
        familie = name.strip().replace(" ", "+")
        for url in (f"https://fonts.googleapis.com/css2?family={familie}:wght@700",
                    f"https://fonts.googleapis.com/css2?family={familie}"):
            try:
                # Einfacher User-Agent -> Google liefert TTF statt WOFF2
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/4.0"})
                css = urllib.request.urlopen(req, timeout=8).read().decode("utf-8")
            except Exception:
                continue
            treffer = re.search(r"url\((https://[^)]+\.ttf)\)", css)
            if treffer:
                os.makedirs(GOOGLE_CACHE, exist_ok=True)
                with urllib.request.urlopen(treffer.group(1), timeout=15) as r, open(ziel, "wb") as f:
                    f.write(r.read())
                return ziel
    except Exception:
        pass
    _google_fehlversuche.add(name)
    return None


def finde_schriftdatei(name: str, schrift_datei: str = "", fett: bool = True):
    """Sucht die CI-Schrift und gibt (pfad, quelle) zurück. Reihenfolge:
    1. im Profil hochgeladene Schriftdatei (zuverlässigster Weg, auch für Hausschriften)
    2. Ordner fonts/ neben der App
    3. auf dem Rechner installierte Systemschriften
    4. automatischer Download von Google Fonts
    Wird nichts gefunden: (None, "ersatz")."""
    if schrift_datei and os.path.exists(schrift_datei):
        return schrift_datei, "Hochgeladene Schriftdatei"
    name = (name or "").strip()
    if not name:
        return None, "ersatz"
    basis = name.replace(" ", "")
    if os.path.isdir(SCHRIFT_VERZEICHNIS):
        treffer = sorted(set(glob.glob(os.path.join(SCHRIFT_VERZEICHNIS, f"{basis}*.[ot]tf"))
                             + glob.glob(os.path.join(SCHRIFT_VERZEICHNIS, f"{name}*.[ot]tf"))))
        if fett:
            treffer.sort(key=lambda p: 0 if "bold" in p.lower() else 1)
        if treffer:
            return treffer[0], "Ordner fonts/"
    kandidaten = ([f"{basis}-Bold.ttf", f"{name} Bold.ttf", f"{basis}Bold.ttf", f"{basis}-Bold.otf"] if fett else []) \
        + [f"{basis}-Regular.ttf", f"{basis}.ttf", f"{name}.ttf", f"{basis}.otf"]
    if basis.lower() == "arial":
        kandidaten = ["arialbd.ttf", "Arial Bold.ttf"] + kandidaten
    for k in kandidaten:
        try:
            ImageFont.truetype(k, 12)
            return k, "Systemschrift"
        except OSError:
            continue
    pfad = _google_font_laden(name)
    if pfad:
        return pfad, "Google Fonts (automatisch geladen)"
    return None, "ersatz"


def finde_schrift(name: str, groesse: int, fett: bool = True, schrift_datei: str = ""):
    pfad, _ = finde_schriftdatei(name, schrift_datei, fett)
    if pfad:
        try:
            return ImageFont.truetype(pfad, groesse)
        except OSError:
            pass
    for k in (["arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"] if fett
              else ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"]):
        try:
            return ImageFont.truetype(k, groesse)
        except OSError:
            continue
    return ImageFont.load_default(size=groesse)


def _umbrechen(draw, text, schrift, max_breite):
    zeilen, aktuell = [], ""
    for wort in re.split(r"[ \t\n]+", text.strip()):  # NICHT split(): das trennt auch am geschützten Leerzeichen
        probe = f"{aktuell} {wort}".strip()
        if draw.textlength(probe, font=schrift) <= max_breite:
            aktuell = probe
        else:
            if aktuell:
                zeilen.append(aktuell)
            aktuell = wort
    if aktuell:
        zeilen.append(aktuell)
    return zeilen


def stoerer_text(angebot: str):
    """Leitet aus dem Angebot den Kurztext für den Störer ab, z. B.
    '10 % Rabatt auf Brezeln' -> ('–10 %', 'RABATT'), '2 für 1,50 €' -> ('1,50 €', 'NUR').
    Gibt None zurück, wenn kein prägnanter Wert (Prozent/Preis) erkennbar ist."""
    angebot = (angebot or "").strip()
    if not angebot:
        return None
    prozent = re.search(r"(\d+(?:[.,]\d+)?)\s?%", angebot)
    if prozent:
        return f"–{prozent.group(1)} %", "RABATT"
    preis = re.search(r"(\d+(?:[.,]\d{1,2})?)\s?(€|euro|eur)(?!\w)", angebot, re.IGNORECASE)
    if preis:
        if re.search(r"rabatt|sparen|spare|günstiger|reduziert", angebot, re.IGNORECASE):
            return f"–{preis.group(1)} €", "RABATT"
        return f"{preis.group(1)} €", "NUR"
    if re.search(r"gratis|kostenlos|umsonst", angebot, re.IGNORECASE):
        return "GRATIS", ""
    if re.search(r"\bneu\b", angebot, re.IGNORECASE):
        return "NEU", ""
    return None


def _zeichne_stoerer(bild, wert: str, zusatz: str, fuell, textfarbe, schrift_name, schrift_datei, rand, oben=None):
    breite, hoehe = bild.size
    d = int(min(breite, hoehe) * 0.30)
    ebene = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    zeichner = ImageDraw.Draw(ebene)
    zeichner.ellipse([0, 0, d - 1, d - 1], fill=fuell + (255,))
    zeichner.ellipse([int(d * 0.05), int(d * 0.05), int(d * 0.95), int(d * 0.95)],
                     outline=textfarbe + (255,), width=max(3, d // 60))
    # Wert so groß wie möglich innerhalb des Kreises
    groesse = int(d * 0.36)
    while groesse > 12:
        schrift = finde_schrift(schrift_name, groesse, fett=True, schrift_datei=schrift_datei)
        tb = zeichner.textbbox((0, 0), wert, font=schrift)
        if tb[2] - tb[0] <= d * 0.78:
            break
        groesse = int(groesse * 0.9)
    tb = zeichner.textbbox((0, 0), wert, font=schrift)
    w_h = tb[3] - tb[1]
    zusatz_schrift = finde_schrift(schrift_name, int(d * 0.11), fett=True, schrift_datei=schrift_datei)
    zb = zeichner.textbbox((0, 0), zusatz, font=zusatz_schrift) if zusatz else (0, 0, 0, 0)
    gesamt = w_h + ((zb[3] - zb[1]) + int(d * 0.04) if zusatz else 0)
    y = (d - gesamt) // 2
    zusatz_oben = zusatz == "NUR"  # "NUR 1,50 €" liest sich von oben nach unten
    y_wert = y + ((zb[3] - zb[1]) + int(d * 0.04) if zusatz and zusatz_oben else 0)
    zeichner.text(((d - (tb[2] - tb[0])) // 2 - tb[0], y_wert - tb[1]), wert, font=schrift, fill=textfarbe)
    if zusatz:
        y_zusatz = y if zusatz_oben else y + w_h + int(d * 0.04)
        zeichner.text(((d - (zb[2] - zb[0])) // 2 - zb[0], y_zusatz - zb[1]),
                      zusatz, font=zusatz_schrift, fill=textfarbe)
    ebene = ebene.rotate(10, resample=Image.BICUBIC, expand=True)
    bild.paste(ebene, (rand, oben if oben is not None else rand), ebene)


def erstelle_werbebild(bild_bytes: bytes, headline: str = "", cta: str = "",
                       plattform_format: str = "ig_feed", ci_farbe: str = "#C9532E",
                       ci_farbe_sekundaer: str = "", schrift_name: str = "",
                       logo_pfad: str = "", schrift_datei: str = "", angebot: str = "") -> bytes:
    """Gibt das fertige Werbebild als JPEG-Bytes zurück.

    CI-Regeln (immer aus dem Unternehmensprofil):
      * Headline in der Primärfarbe, in der CI-Schrift
      * CTA-Button in der Primärfarbe, Button-Text in der Sekundärfarbe
        (nur wenn der Kontrast ausreicht, sonst Weiß/Schwarz für Lesbarkeit)
      * Hintergrund-Verlauf hinter dem Text wird automatisch hell oder dunkel
        gewählt, damit die CI-Textfarbe lesbar bleibt
    """
    primaer = _hex_zu_rgb(ci_farbe)
    sekundaer = _hex_zu_rgb(ci_farbe_sekundaer) if ci_farbe_sekundaer else None
    breite, hoehe = FORMATE.get(plattform_format, FORMATE["ig_feed"])
    basis = Image.open(io.BytesIO(bild_bytes))
    basis = ImageOps.exif_transpose(basis).convert("RGB")
    # Auf Zielformat zuschneiden (mittig, wie Instagram)
    bild = ImageOps.fit(basis, (breite, hoehe), method=Image.LANCZOS, centering=(0.5, 0.5))

    headline = (headline or "").strip()
    cta = (cta or "").strip()
    rand = int(min(breite, hoehe) * 0.07)
    max_textbreite = breite - 2 * rand

    if headline or cta:
        # Verlauf von unten (dunkel -> transparent) für Lesbarkeit auf jedem Motiv
        verlauf_hoehe = int(hoehe * (0.55 if hoehe > breite else 0.62))
        maske = Image.new("L", (1, verlauf_hoehe))
        for y in range(verlauf_hoehe):
            maske.putpixel((0, y), int(225 * min(1.0, (y / verlauf_hoehe) * 1.15) ** 1.4))
        maske = maske.resize((breite, verlauf_hoehe))
        # Dunkle CI-Farbe -> heller Verlauf, helle CI-Farbe -> dunkler Verlauf
        grund = (255, 255, 255) if not _ist_hell(primaer) else (0, 0, 0)
        flaeche = Image.new("RGB", (breite, verlauf_hoehe), grund)
        bild.paste(flaeche, (0, hoehe - verlauf_hoehe), maske)

    y_oben = max(rand, int(hoehe * OBERER_SICHERHEITSBEREICH.get(plattform_format, 0)))
    # Zahl und Einheit nie trennen ("1,50 €", "10 %")
    headline = re.sub(r"(\d)\s+(%|€|Euro|EUR)", "\\1\u00a0\\2", headline)
    draw = ImageDraw.Draw(bild)
    grund = (255, 255, 255) if not _ist_hell(primaer) else (0, 0, 0)
    # Stories: unten ~14 % freihalten (dort liegt Instagrams Antwortfeld)
    y_unten = hoehe - max(rand, int(hoehe * UNTERER_SICHERHEITSBEREICH.get(plattform_format, 0)))

    # --- Call-to-Action als Button in CI-Primärfarbe ------------------------
    if cta:
        cta_groesse = max(22, int(min(breite, hoehe) * 0.038))
        cta_schrift = finde_schrift(schrift_name, cta_groesse, fett=True, schrift_datei=schrift_datei)
        pad_x, pad_y = int(cta_groesse * 0.9), int(cta_groesse * 0.55)
        tb = draw.textbbox((0, 0), cta, font=cta_schrift)
        b_w, b_h = tb[2] - tb[0] + 2 * pad_x, tb[3] - tb[1] + 2 * pad_y
        x0, y0 = rand, y_unten - b_h
        draw.rounded_rectangle([x0, y0, x0 + b_w, y0 + b_h], radius=b_h // 2, fill=primaer)
        if sekundaer and kontrast(sekundaer, primaer) >= 3:
            textfarbe = sekundaer
        else:
            textfarbe = max([(255, 255, 255), (26, 24, 22)], key=lambda c: kontrast(c, primaer))
        draw.text((x0 + pad_x - tb[0], y0 + pad_y - tb[1]), cta, font=cta_schrift, fill=textfarbe)
        y_unten = y0 - int(cta_groesse * 0.9)

    # --- Headline: so groß wie möglich, max. 3 Zeilen ------------------------
    if headline:
        groesse = int(min(breite, hoehe) * 0.085)
        while True:
            schrift = finde_schrift(schrift_name, groesse, fett=True, schrift_datei=schrift_datei)
            zeilen = _umbrechen(draw, headline, schrift, max_textbreite)
            if len(zeilen) <= 3 or groesse <= 28:
                break
            groesse = int(groesse * 0.9)
        zeilen = zeilen[:3]
        zeilenhoehe = int(groesse * 1.15)
        y = y_unten - zeilenhoehe * len(zeilen)
        if sekundaer and kontrast(sekundaer, grund) >= 1.5:  # Akzentlinie in Sekundärfarbe
            draw.rectangle([rand, y - int(groesse * 0.45), rand + int(groesse * 1.6),
                            y - int(groesse * 0.45) + max(4, groesse // 12)], fill=sekundaer)
        for zeile in zeilen:
            draw.text((rand, y), zeile, font=schrift, fill=primaer)
            y += zeilenhoehe

    # --- Logo oben rechts (SVG kann Pillow nicht lesen -> wird übersprungen) --
    if logo_pfad and os.path.exists(logo_pfad) and not logo_pfad.lower().endswith(".svg"):
        try:
            logo = Image.open(logo_pfad).convert("RGBA")
            logo_max = int(min(breite, hoehe) * 0.16)
            logo.thumbnail((logo_max, logo_max))
            pad = int(logo_max * 0.12)
            kachel = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
            ImageDraw.Draw(kachel).rounded_rectangle(
                [0, 0, kachel.width - 1, kachel.height - 1], radius=pad, fill=(255, 255, 255, 235))
            kachel.alpha_composite(logo, (pad, pad))
            bild.paste(kachel, (breite - rand - kachel.width, y_oben), kachel)
        except Exception:
            pass

    # --- Störer mit dem Angebot (z. B. –10 % Rabatt) oben links ---------------
    # Kontrastreichste CI-Kombination wählen, damit die Aktion sofort ins Auge fällt.
    stoerer = stoerer_text(angebot)
    if stoerer:
        kombis = [(primaer, sekundaer)] if sekundaer else []
        kombis += [(sekundaer, primaer)] if sekundaer else []
        kombis += [(primaer, (255, 255, 255)), (primaer, (26, 24, 22))]
        fuell, schriftfarbe = max(kombis, key=lambda k: kontrast(k[0], k[1]))
        _zeichne_stoerer(bild, stoerer[0], stoerer[1], fuell, schriftfarbe, schrift_name, schrift_datei, rand,
                         oben=y_oben)

    out = io.BytesIO()
    bild.save(out, format="JPEG", quality=90)
    return out.getvalue()