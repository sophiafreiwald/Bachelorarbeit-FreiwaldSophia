"""Automatische Hinweise auf CI-Abweichungen und veraltete Angaben.

Bewusst regelbasiert statt per KI: Die Hinweise erscheinen sofort beim Bearbeiten,
kosten nichts und sind nachvollziehbar. Grenze (für die Limitationen der Arbeit):
Erkannt wird nur, was sich als Regel formulieren lässt – inhaltliche Fehler wie
falsche Öffnungszeiten erkennt dieses Modul nicht.
"""

import datetime
import re

HASHTAG = re.compile(r"#\w+")
# Emoji-Bereiche (Symbole, Piktogramme, Smileys, Dingbats)
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")
DU_FORMEN = re.compile(r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner|euch|euer|eure|euren|eurem)\b", re.IGNORECASE)
# Du-Imperative ohne Pronomen („Komm vorbei“, „Hol dir“, „Entdecke …“) am Satzanfang oder nach „jetzt“ –
# häufige CTA-Verben; „Kommen Sie“, „Entdecken Sie“ werden dadurch NICHT erfasst.
DU_IMPERATIV = re.compile(
    r"(?:^|[.!?:]\s+|\bjetzt\s+)(komm|hol|entdeck|schau|besuch|sicher|lass|probier|gönn|bestell|buch|meld|folg|"
    r"teil|erfahr|sag|schreib|kauf|nutz|erleb|start|frag|ruf|triff|lies|sieh|gib|nimm|vergiss|freu|klick|abonnier)"
    r"e?\b(?!\s+sie\b)", re.IGNORECASE | re.MULTILINE)
# „Sie/Ihnen/Ihre…“ nicht am Satzanfang (dort könnte es auch „sie“ = 3. Person sein)
SIE_FORMEN = re.compile(r"(?:^|[^.!?]\s)(Sie|Ihnen|Ihre|Ihren|Ihrem|Ihrer)\b")
DATUM = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d)")
PROFIL_MAX_ALTER_TAGE = 90


def pflicht_hashtags(profil: dict) -> list:
    """Hashtags, die laut „Sonstige CI-Besonderheiten“ in jeden Beitrag gehören."""
    gefunden = HASHTAG.findall((profil or {}).get("ci_besonderheiten") or "")
    return list(dict.fromkeys(gefunden))  # Reihenfolge behalten, Duplikate entfernen


def _vergangene_daten(text: str, stichtag: datetime.date) -> list:
    treffer = []
    for m in DATUM.finditer(text or ""):
        tag, monat = int(m.group(1)), int(m.group(2))
        if not (1 <= tag <= 31 and 1 <= monat <= 12):
            continue
        jahr = int(m.group(3)) if m.group(3) else stichtag.year
        if jahr < 100:
            jahr += 2000
        try:
            datum = datetime.date(jahr, monat, tag)
        except ValueError:
            continue
        if datum < stichtag:
            treffer.append(m.group(0))
    return treffer


def pruefe_beitrag(profil: dict, caption: str = "", headline: str = "", stichtag: datetime.date = None) -> list:
    """Prüft einen Entwurf gegen CI-Vorgaben, Anrede und Aktualität.

    Gibt eine Liste von dicts zurück: {"text": str, "ergaenzen": "#Hashtag" | None}.
    "ergaenzen" ist gesetzt, wenn der Hinweis per Klick behoben werden kann.
    """
    profil = profil or {}
    stichtag = stichtag or datetime.date.today()
    text = f"{headline or ''}\n{caption or ''}"
    besonderheiten = profil.get("ci_besonderheiten") or ""
    hinweise = []

    vorhanden = {h.lower() for h in HASHTAG.findall(caption or "")}
    for tag in pflicht_hashtags(profil):
        if tag.lower() not in vorhanden:
            hinweise.append({"text": f"CI-Vorgabe: Der Hashtag {tag} fehlt.", "ergaenzen": tag})

    if re.search(r"(keine|nie|ohne)\s+emojis?", besonderheiten, re.IGNORECASE) and EMOJI.search(text):
        hinweise.append({"text": "CI-Vorgabe: Laut Profil sollen keine Emojis verwendet werden.", "ergaenzen": None})

    anrede = profil.get("anrede")
    if anrede == "Sie" and (DU_FORMEN.search(text) or DU_IMPERATIV.search(text)):
        hinweise.append({"text": "Anrede: Im Profil ist „Sie“ festgelegt, der Text duzt. Bitte prüfen.", "ergaenzen": None})
    elif anrede == "Du" and SIE_FORMEN.search(text):
        hinweise.append({"text": "Anrede: Im Profil ist „Du“ festgelegt, der Text siezt. Bitte prüfen.", "ergaenzen": None})

    for datum in _vergangene_daten(text, stichtag):
        hinweise.append({"text": f"Aktualität: Das Datum {datum} liegt zum Veröffentlichungstermin in der Vergangenheit.",
                         "ergaenzen": None})
    return hinweise


def anrede_verstoss(text: str, anrede: str) -> bool:
    """True, wenn der Text die im Profil festgelegte Ansprache verletzt (gleiche Regeln wie pruefe_beitrag)."""
    if anrede == "Du":
        return bool(SIE_FORMEN.search(text or ""))
    return bool(DU_FORMEN.search(text or "") or DU_IMPERATIV.search(text or ""))  # Standard im Profil ist „Sie“


def name_korrigieren(text: str, name: str) -> str:
    """Schreibt den Unternehmensnamen überall exakt wie im Profil (z. B. „blumen weiss“ → „Blumen Weiss“).
    Hashtags (#BlumenWeiss) bleiben unberührt – sie baut die App selbst nach der Hashtag-Regel."""
    name = (name or "").strip()
    teile = [re.escape(t) for t in re.split(r"[\s\-]+", name) if t]
    if not text or not teile:
        return text
    muster = re.compile(r"(?<![#\w])" + r"[\s\-]*".join(teile) + r"(?!\w)", re.IGNORECASE)
    return muster.sub(lambda m: name, text)


def emojis_verboten(profil: dict) -> bool:
    return bool(re.search(r"(keine|nie|ohne)\s+emojis?", (profil or {}).get("ci_besonderheiten") or "", re.IGNORECASE))


def emojis_entfernen(text: str) -> str:
    ohne = re.sub(r"[ \t]{2,}", " ", EMOJI.sub("", text or ""))
    return re.sub(r"[ \t]+([!?.,;:])", r"\1", ohne).strip()


def profil_hinweise(profil: dict, logo_vorhanden: bool, heute: datetime.date = None) -> list:
    """Hinweise zum Unternehmensprofil selbst (fehlende oder möglicherweise veraltete Angaben)."""
    profil = profil or {}
    heute = heute or datetime.date.today()
    hinweise = []
    import json
    try:
        zeiten = json.loads(profil.get("oeffnungszeiten") or "{}")
    except ValueError:
        zeiten = {}
    if not any((v or "").strip() for v in zeiten.values()):
        hinweise.append("Im Profil sind noch keine Öffnungszeiten hinterlegt. Sie können dann weder in Beiträge "
                        "einfließen noch im Faktencheck geprüft werden.")
    if not ((profil.get("strasse") or "").strip() and (profil.get("ort") or "").strip()):
        hinweise.append("Die Anschrift im Profil ist unvollständig (Straße und Ort).")
    if not (profil.get("produkte") or "").strip():
        hinweise.append("Im Profil sind noch keine Produkte oder Leistungen hinterlegt.")
    if not logo_vorhanden:
        hinweise.append("Es ist kein Logo hinterlegt. Werbebilder erscheinen ohne Logo.")
    stand = profil.get("profil_aktualisiert") or ""
    try:
        tage = (heute - datetime.date.fromisoformat(stand[:10])).days
        if tage > PROFIL_MAX_ALTER_TAGE:
            hinweise.append(f"Das Profil wurde zuletzt vor {tage} Tagen aktualisiert. "
                            "Sind Öffnungszeiten, Preise und Angebote noch aktuell?")
    except ValueError:
        pass  # noch nie gespeichert -> kein Altershinweis
    return hinweise


def hashtag_anhaengen(caption: str, tag: str, maximum: int = None, kuerzen=None) -> str:
    """Hängt einen Hashtag an; bleibt dabei im Zeichenlimit (kuerzen = hart_kuerzen aus app.py)."""
    neu = f"{(caption or '').rstrip()} {tag}".strip()
    if maximum and len(neu) > maximum and kuerzen:
        return f"{kuerzen(caption, maximum - len(tag) - 1)} {tag}"
    return neu


# --- Öffnungszeiten: Eingabe prüfen und vereinheitlichen ----------------------------

WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WOCHENTAGE_LANG = {"Mo": "Montag", "Di": "Dienstag", "Mi": "Mittwoch", "Do": "Donnerstag",
                   "Fr": "Freitag", "Sa": "Samstag", "So": "Sonntag"}
_ZEIT = r"(\d{1,2})(?:[:.](\d{2}))?"
_SPANNE = re.compile(rf"^\s*{_ZEIT}\s*(?:uhr)?\s*[-–—]\s*{_ZEIT}\s*(?:uhr)?\s*$", re.IGNORECASE)


def normalisiere_oeffnungszeit(text: str):
    """'6-12.30, 14-18' -> (True, '06:00–12:30, 14:00–18:00'); 'zu' -> (True, 'geschlossen');
    leer -> (True, ''); nicht lesbar -> (False, Originaltext)."""
    t = (text or "").strip()
    if not t:
        return True, ""
    if t.lower() in ("geschlossen", "zu", "ruhetag", "-", "–"):
        return True, "geschlossen"
    spannen = []
    for teil in re.split(r"\s*(?:,|;|/|und)\s*", t):
        m = _SPANNE.match(teil)
        if not m:
            return False, t
        h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
        if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and m1 < 60 and m2 < 60) or (h2, m2) <= (h1, m1):
            return False, t
        spannen.append(f"{h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}")
    return True, ", ".join(spannen)


# --- Hashtag-Regel für Beitrag (Feed) und Reel ------------------------------------------
# Vorgabe: genau 5 Hashtags – 1. Unternehmensname, 2. Ort (aus der Anschrift), 3.–5. thematisch.
# Die Hashtags stehen in einer eigenen Zeile und zählen NICHT zum Primärtext-Limit. Storys: keine Pflicht.

HASHTAG_ANZAHL = 5
_RECHTSFORMEN = re.compile(r"\b(gmbh|mbh|ug|ag|kg|ohg|gbr|e\.?\s?k\.?|e\.?\s?v\.?|co\.?|inh\.?|haftungsbeschränkt)\b",
                           re.IGNORECASE)


def hashtag_regel_gilt(plattform_format: str) -> bool:
    # Beitrag (Feed), Karussell und Reel: genau 5 Hashtags (redaktionelle Regel); Story: keine Pflicht
    return str(plattform_format or "").endswith(("_feed", "_karussell", "_reels"))


def als_hashtag(text: str) -> str:
    """'Blumen Müller GmbH & Co. KG' -> '#BlumenMüller'; 'Filderstadt-Bernhausen' -> '#FilderstadtBernhausen'."""
    text = _RECHTSFORMEN.sub(" ", text or "")
    woerter = re.findall(r"[^\W_]+", text)
    if not woerter:
        return ""
    return "#" + "".join(w[:1].upper() + w[1:] for w in woerter)


def feste_hashtags(profil: dict) -> list:
    """Die beiden festen Hashtags: Unternehmensname und Ort (Ort nach der PLZ; Fallback: alter Standort)."""
    profil = profil or {}
    tags = [als_hashtag(profil.get("unternehmensname", "")),
            als_hashtag(profil.get("ort") or profil.get("standort") or "")]
    return [t for t in tags if t]


def trenne_hashtags(caption: str):
    """-> (Text ohne Hashtags, [Hashtags in Reihenfolge, ohne Duplikate])."""
    tags = list(dict.fromkeys(HASHTAG.findall(caption or "")))
    text = HASHTAG.sub("", caption or "")
    text = re.sub(r"[ \t]+([.,!?;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    return text, tags


def hashtags_aufbauen(profil: dict, vorschlaege: list) -> list:
    """Genau 5 Hashtags: feste zuerst, dann CI-Pflicht-Hashtags, dann die thematischen Vorschläge.
    Reichen die Vorschläge nicht, wird mit der Branche aufgefüllt."""
    ergebnis, gesehen = [], set()

    def dazu(tag):
        tag = tag if str(tag).startswith("#") else als_hashtag(str(tag))
        if tag and tag.lower() not in gesehen and len(ergebnis) < HASHTAG_ANZAHL:
            ergebnis.append(tag)
            gesehen.add(tag.lower())

    for tag in feste_hashtags(profil) + pflicht_hashtags(profil) + list(vorschlaege or []):
        dazu(tag)
    if len(ergebnis) < HASHTAG_ANZAHL and (profil or {}).get("branche"):
        dazu(als_hashtag(profil["branche"]))
    return ergebnis


def caption_zusammensetzen(text: str, hashtags: list) -> str:
    text = (text or "").strip()
    zeile = " ".join(hashtags or [])
    return f"{text}\n\n{zeile}" if text and zeile else (text or zeile)


def pruefe_hashtag_regel(profil: dict, hashtags: list) -> list:
    """Hinweise, wenn die Hashtag-Zeile von der Vorgabe abweicht (nur Feed/Reel)."""
    hinweise = []
    tags = [t.lower() for t in hashtags or []]
    if len(tags) != HASHTAG_ANZAHL:
        hinweise.append({"text": f"Vorgabe: genau {HASHTAG_ANZAHL} Hashtags (aktuell {len(tags)}).", "ergaenzen": None})
    for pos, tag in enumerate(feste_hashtags(profil)):
        if pos >= len(tags) or tags[pos] != tag.lower():
            was = "Unternehmensname" if pos == 0 else "Ort"
            hinweise.append({"text": f"Vorgabe: Hashtag {pos + 1} ist der {was} ({tag}).", "ergaenzen": None})
    return hinweise



# --- Keine Wiederholung: Bildtext (Headline) darf NICHT in der Caption stehen ------------------


def _normalisiert(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", (text or "").lower()))


def caption_wiederholt_headline(headline: str, caption_text: str) -> bool:
    kern = _normalisiert(headline)
    return bool(kern) and len(kern) >= 6 and kern in _normalisiert(caption_text)


def headline_aus_caption_entfernen(caption_text: str, headline: str) -> str:
    """Entfernt die wörtlich wiederholte Headline (ohne Groß-/Kleinschreibung, samt Satzzeichen danach) aus der
    Caption. Abgewandelte Wiederholungen bleiben stehen und werden nur als Hinweis gemeldet."""
    kopf = (headline or "").strip().rstrip(".!?:")
    if len(kopf) < 6:
        return caption_text
    muster = re.compile(re.escape(kopf) + r"\s*[.!?:–-]*\s*", re.IGNORECASE)
    neu = muster.sub("", caption_text or "", count=1).strip()
    return neu[:1].upper() + neu[1:] if neu and neu != (caption_text or "").strip() else (caption_text or "")


def pruefe_bildtext_wiederholung(headline: str, caption_text: str) -> list:
    """Hinweis mit „entfernen“, wenn die Caption den Text auf dem Bild wiederholt."""
    if not caption_wiederholt_headline(headline, caption_text):
        return []
    return [{"text": f"Die Caption wiederholt den Text auf dem Bild („{headline.strip()}“). Bild und Caption sollen "
                     "sich ergänzen, nicht doppeln.", "ergaenzen": None, "entfernen": headline.strip()}]
