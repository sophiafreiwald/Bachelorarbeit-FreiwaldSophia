"""Werbebild-Komposition: legt Headline und Logo auf ein Basisbild.

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
VERSION = 22  # 22: neutrale Headline ohne CI für Variante B; 21: Kennzeichnung „KI-generiert“ auf KI-Bildern; 19: Story mit CTA-Knopf

# Zielformate in Pixeln (Plattformtabelle der Arbeit): Feed/Karussell 4:5 = 1080 × 1350, Story 9:16 = 1080 × 1920.
FORMATE = {
    "ig_feed": (1080, 1350), "ig_karussell": (1080, 1350), "ig_story": (1080, 1920),
    "fb_feed": (1080, 1350), "fb_karussell": (1080, 1350), "fb_story": (1080, 1920),
}
# Feed und Karussell: 4:5 oder 1:1 nach Wahl des Unternehmens
VERHAELTNIS_MASSE = {"4:5": (1080, 1350), "1:1": (1080, 1080)}

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


def _wort_trennen(draw, wort, schrift, max_breite):
    """Notlösung für Wörter, die selbst in kleinster Schrift breiter als das Bild sind:
    in Stücke mit Trennstrich zerlegen, die jeweils in die Zeile passen."""
    teile, rest = [], wort
    while rest and draw.textlength(rest, font=schrift) > max_breite:
        n = len(rest) - 1
        while n > 2 and draw.textlength(rest[:n] + "-", font=schrift) > max_breite:
            n -= 1
        teile.append(rest[:n] + "-")
        rest = rest[n:]
    return teile + ([rest] if rest else [])


def _umbrechen(draw, text, schrift, max_breite, trennen=False):
    zeilen, aktuell = [], ""
    woerter = []
    for wort in re.split(r"[ \t\n]+", text.strip()):  # NICHT split(): das trennt auch am geschützten Leerzeichen
        woerter += _wort_trennen(draw, wort, schrift, max_breite) if trennen else [wort]
    for wort in woerter:
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


def _passende_zeilen(draw, text, schrift_name, schrift_datei, groesse, max_breite, max_zeilen=3):
    """Schriftgröße so lange verkleinern, bis der Text in höchstens max_zeilen Zeilen passt UND keine Zeile breiter
    als max_breite ist (auch bei langen Einzelwörtern wie „Lieblingshochzeitsblumen“). Reicht das bis zur
    Mindestgröße nicht, werden überlange Wörter mit Trennstrich umbrochen. Gibt (schrift, groesse, zeilen) zurück."""
    mindest = max(22, int(groesse * 0.45))
    while True:
        schrift = finde_schrift(schrift_name, groesse, fett=True, schrift_datei=schrift_datei)
        zeilen = _umbrechen(draw, text, schrift, max_breite)
        passt = len(zeilen) <= max_zeilen and all(draw.textlength(z, font=schrift) <= max_breite for z in zeilen)
        if passt or groesse <= mindest:
            break
        groesse = int(groesse * 0.92)
    if not passt:
        zeilen = _umbrechen(draw, text, schrift, max_breite, trennen=True)
    return schrift, groesse, zeilen[:max_zeilen]



# --- Headline auf dem Bild ------------------------------------------------------------------
# Gestaltung: Headline waagerecht und senkrecht zentriert (Dang et al., 2026), in der Primärfarbe und der
# CI-Schrift. CTA: Feed/Karussell in der Caption, Story (ohne Caption) als Knopf unter der Headline im Bild
#. Kein Störer, kein Schleier, keine Kontur, kein Schatten. Lesbarkeit bei Bedarf über den
# Haken „Farbfläche“ (textflaeche=True) je Beitrag.

def flaechen_textfarbe(primaer, sekundaer=None):
    """Schriftfarbe AUF der Textfläche in der Primärfarbe: Sekundärfarbe, wenn sie sich ausreichend (mind. 3:1,
    WCAG-Richtwert für große Schrift) von der Primärfarbe abhebt, sonst Weiß oder Dunkel. Hängt nur von den
    CI-Farben ab – dadurch bei allen Beiträgen eines Unternehmens gleich."""
    if sekundaer and kontrast(sekundaer, primaer) >= 3:
        return sekundaer
    return max([(255, 255, 255), (26, 24, 22)], key=lambda c: kontrast(c, primaer))


def _textflaeche(bild, zeilen_pos, schrift, groesse, primaer):
    """Abgerundetes Feld in der Primärfarbe eng um den Headline-Block."""
    draw = ImageDraw.Draw(bild)
    boxen = [draw.textbbox((x, y), z, font=schrift) for (x, y), z in zeilen_pos]
    pad_x, pad_y = int(groesse * 0.45), int(groesse * 0.3)
    x0 = min(b[0] for b in boxen) - pad_x
    y0 = min(b[1] for b in boxen) - pad_y
    x1 = max(b[2] for b in boxen) + pad_x
    y1 = max(b[3] for b in boxen) + pad_y
    draw.rounded_rectangle([x0, y0, x1, y1], radius=int(groesse * 0.25), fill=primaer)


def _cta_knopf(cta, schrift_name, schrift_datei, groesse):
    """Maße und Schrift des CTA-Knopfs: (schrift, textbox, breite, hoehe, pad_x, pad_y)."""
    schrift = finde_schrift(schrift_name, groesse, fett=True, schrift_datei=schrift_datei)
    tb = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), cta, font=schrift)
    pad_x, pad_y = int(groesse * 0.9), int(groesse * 0.55)
    return schrift, tb, tb[2] - tb[0] + 2 * pad_x, tb[3] - tb[1] + 2 * pad_y, pad_x, pad_y


def _headline_mittig(bild, headline, primaer, sekundaer, schrift_name, schrift_datei, textflaeche=False, cta=""):
    """Headline waagerecht und senkrecht zentriert, höchstens 3 Zeilen, so groß wie möglich. Mit cta (Story):
    darunter ein abgerundeter Knopf in der Primärfarbe; Headline und Knopf sind zusammen zentriert."""
    if not headline and not cta:
        return bild
    breite, hoehe = bild.size
    draw = ImageDraw.Draw(bild)
    zeilen, zeilenhoehe, schrift, groesse = [], 0, None, 0
    if headline:
        headline = re.sub(r"(\d)\s+(%|€|Euro|EUR)", "\\1\u00a0\\2", headline)  # Zahl und Einheit nie trennen
        schrift, groesse, zeilen = _passende_zeilen(draw, headline, schrift_name, schrift_datei,
                                                    int(min(breite, hoehe) * 0.095), int(breite * 0.78))
        zeilenhoehe = int(groesse * 1.15)
    knopf = _cta_knopf(cta, schrift_name, schrift_datei, max(22, int(min(breite, hoehe) * 0.045))) if cta else None
    abstand = int(groesse * 0.6) if (zeilen and knopf) else 0
    block_h = zeilenhoehe * len(zeilen) + abstand + (knopf[3] if knopf else 0)
    y = (hoehe - block_h) // 2
    zeilen_pos = []
    for zeile in zeilen:
        zeilen_pos.append((((breite - draw.textlength(zeile, font=schrift)) / 2, y), zeile))
        y += zeilenhoehe
    if zeilen:
        if textflaeche:
            _textflaeche(bild, zeilen_pos, schrift, groesse, primaer)
            textfarbe = flaechen_textfarbe(primaer, sekundaer)
        else:
            textfarbe = primaer
        draw = ImageDraw.Draw(bild)
        for (x, yz), zeile in zeilen_pos:
            draw.text((x, yz), zeile, font=schrift, fill=textfarbe)
    if knopf:  # CTA-Knopf: Primärfarbe, Text in der Sekundärfarbe (bei zu wenig Kontrast Weiß/Dunkel)
        k_schrift, tb, k_b, k_h, pad_x, pad_y = knopf
        x0, y0 = int((breite - k_b) / 2), y + abstand
        draw = ImageDraw.Draw(bild)
        draw.rounded_rectangle([x0, y0, x0 + k_b, y0 + k_h], radius=k_h // 2, fill=primaer)
        draw.text((x0 + pad_x - tb[0], y0 + pad_y - tb[1]), cta, font=k_schrift,
                  fill=flaechen_textfarbe(primaer, sekundaer))
    return bild


def _logo_einfuegen(bild, logo_pfad, logo_stil, rand):
    """Logo oben rechts. logo_stil: "kachel" = mit weißem Hintergrund, "frei" = ohne Hintergrund, "aus" = kein
    Logo. SVG kann Pillow nicht lesen – wird übersprungen."""
    if logo_stil == "aus" or not logo_pfad or not os.path.exists(logo_pfad) or logo_pfad.lower().endswith(".svg"):
        return
    breite, hoehe = bild.size
    try:
        logo = Image.open(logo_pfad).convert("RGBA")
        logo_max = int(min(breite, hoehe) * 0.16)
        logo.thumbnail((logo_max, logo_max))
        pad = int(logo_max * 0.12)
        kachel = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
        if logo_stil != "frei":
            ImageDraw.Draw(kachel).rounded_rectangle(
                [0, 0, kachel.width - 1, kachel.height - 1], radius=pad, fill=(255, 255, 255, 235))
        kachel.alpha_composite(logo, (pad, pad))
        bild.paste(kachel, (breite - rand - kachel.width, rand), kachel)
    except Exception:
        pass


# --- Kennzeichnung KI-generierter Bilder ------------------------------
# Transparenzpflicht nach Art. 50 Abs. 4 KI-Verordnung (EU) 2024/1689 für realistisch wirkende KI-Bilder:
# sichtbarer Hinweis direkt im Bild, damit er auch beim Teilen erhalten bleibt. Dezent unten rechts;
# bei Storys oberhalb des Antwortfelds von Instagram/Facebook.
KI_KENNZEICHNUNG = "KI-generiert"


def _ki_kennzeichnung(bild, schrift_name, schrift_datei, story=False):
    breite, hoehe = bild.size
    groesse = max(18, int(min(breite, hoehe) * 0.028))  # gut lesbar, aber zurückhaltend
    schrift = finde_schrift(schrift_name, groesse, fett=False, schrift_datei=schrift_datei)
    tb = ImageDraw.Draw(bild).textbbox((0, 0), KI_KENNZEICHNUNG, font=schrift)
    pad_x, pad_y = int(groesse * 0.6), int(groesse * 0.35)
    k_b, k_h = tb[2] - tb[0] + 2 * pad_x, tb[3] - tb[1] + 2 * pad_y
    rand = int(min(breite, hoehe) * 0.03)
    x0 = breite - rand - k_b
    y0 = hoehe - k_h - (int(hoehe * 0.15) if story else rand)
    ebene = Image.new("RGBA", bild.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ebene)
    d.rounded_rectangle([x0, y0, x0 + k_b, y0 + k_h], radius=k_h // 2, fill=(0, 0, 0, 110))
    d.text((x0 + pad_x - tb[0], y0 + pad_y - tb[1]), KI_KENNZEICHNUNG, font=schrift, fill=(255, 255, 255, 235))
    return Image.alpha_composite(bild.convert("RGBA"), ebene).convert("RGB")


def erstelle_werbebild(bild_bytes: bytes, headline: str = "", plattform_format: str = "ig_feed",
                       ci_farbe: str = "#C9532E", ci_farbe_sekundaer: str = "", schrift_name: str = "",
                       logo_pfad: str = "", schrift_datei: str = "", logo_stil: str = "kachel",
                       verhaeltnis: str = "", textflaeche: bool = False, cta: str = "",
                       ki_bild: bool = False) -> bytes:
    """Gibt das fertige Werbebild als JPEG-Bytes zurück: Bild auf das Zielformat zugeschnitten, Headline mittig
    in der Primärfarbe (mit textflaeche=True auf einem Feld in der Primärfarbe), Logo oben rechts.
    cta wird nur bei Storys gezeichnet (Knopf unter der Headline); Feed und Karussell tragen den CTA in der Caption.
    ki_bild=True (KI-generiertes Bild): immer dezenter Hinweis „KI-generiert“ unten rechts (Art. 50 Abs. 4 KI-VO)."""
    breite, hoehe = FORMATE.get(plattform_format, FORMATE["ig_feed"])
    if verhaeltnis in VERHAELTNIS_MASSE and not str(plattform_format).endswith("_story"):
        breite, hoehe = VERHAELTNIS_MASSE[verhaeltnis]
    basis = ImageOps.exif_transpose(Image.open(io.BytesIO(bild_bytes))).convert("RGB")
    bild = ImageOps.fit(basis, (breite, hoehe), method=Image.LANCZOS, centering=(0.5, 0.5))  # mittig, wie Instagram
    if ci_farbe == "neutral":  # ohne CI (Variante B): Schwarz oder Weiß, je nachdem, was sich vom Bildmittelteil abhebt
        ausschnitt = bild.crop((breite // 4, hoehe // 3, breite * 3 // 4, hoehe * 2 // 3))
        mitte = ausschnitt.resize((1, 1), Image.BOX).getpixel((0, 0))
        primaer = max([(255, 255, 255), (26, 24, 22)], key=lambda c: kontrast(c, mitte))
    else:
        primaer = _hex_zu_rgb(ci_farbe)
    sekundaer = _hex_zu_rgb(ci_farbe_sekundaer) if ci_farbe_sekundaer else None

    cta = (cta or "").strip() if str(plattform_format).endswith("_story") else ""
    bild = _headline_mittig(bild, (headline or "").strip(), primaer, sekundaer, schrift_name, schrift_datei,
                            textflaeche, cta)
    _logo_einfuegen(bild, logo_pfad, logo_stil, int(min(breite, hoehe) * 0.07))
    if ki_bild:
        bild = _ki_kennzeichnung(bild, "", "", str(plattform_format).endswith("_story"))  # neutrale Schrift

    out = io.BytesIO()
    bild.save(out, format="JPEG", quality=90)
    return out.getvalue()
