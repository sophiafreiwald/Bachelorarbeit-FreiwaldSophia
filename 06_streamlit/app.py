"""Content-Engine – Streamlit-Frontend.

Architektur:
  Login/Registrierung -> Chat-Dialog (Absichtserkennung via OpenAI) ->
  Vorschläge (3 Varianten, Bildquelle wählen, sofort veröffentlichen
  ODER für den Kalender einplanen) -> Kalender (Planungsübersicht,
  manuelles Veröffentlichen zum geplanten Zeitpunkt).

Persistenz: SQLite über db.py (Nutzer, Unternehmensprofil, geplante und veröffentlichte Beiträge).
Prompt-Architektur: Alle Anweisungen an die KI stehen in prompts.py (Backend).
KI-Aufrufe (OpenAI) und Website-Abruf laufen direkt aus Python.
"""

import base64
import calendar
import concurrent.futures
import datetime
import html
import io
import json
import os
import re

import requests
import streamlit as st
from PIL import Image

try:
    from dotenv import load_dotenv  # Paket „python-dotenv“ (pip install python-dotenv)
except ImportError:
    def load_dotenv(pfad: str = None):
        """Ersatz, falls python-dotenv nicht installiert ist: liest einfache KEY=WERT-Zeilen aus .env.
        Bereits gesetzte Umgebungsvariablen werden – wie bei python-dotenv – nicht überschrieben."""
        pfad = pfad or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if not os.path.exists(pfad):
            return False
        with open(pfad, encoding="utf-8") as datei:
            for zeile in datei:
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#") or "=" not in zeile:
                    continue
                schluessel, wert = zeile.split("=", 1)
                schluessel = schluessel.replace("export ", "", 1).strip()
                wert = wert.strip().strip('"').strip("'")
                os.environ.setdefault(schluessel, wert)
        return True

import db
import hinweise
import prompts
import werbebild

# .env liegt neben app.py – unabhängig davon, aus welchem Ordner Streamlit gestartet wird
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# --- Konfiguration -------------------------------------------------------

OPENAI_API_KEY = os.environ.get("API_CHATGPT")
# Modelle: fest eingestellt, damit alle Durchläufe – auch die Evaluation –
# mit denselben Modellen und derselben Bildqualität laufen (keine Änderung über die .env).
#   Text: gpt-6-luna · Bild: gpt-image-2.5-flare, Qualität „medium“
OPENAI_MODEL = "gpt-6-luna"
OPENAI_IMAGE_MODEL = "gpt-image-2.5-flare"
OPENAI_IMAGE_QUALITY = "medium"
GENERIERTE_BILDER_VERZEICHNIS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "uploads", "generated"
)

db.init_db()

# Prüft, ob app.py und werbebild.py vom selben Stand sind. Häufige Ursache für
# Fehler wie "unexpected keyword argument": eine alte werbebild.py im Ordner
# oder ein Streamlit-Server, der das alte Modul noch im Speicher hat.
WERBEBILD_VERSION_ERWARTET = 23
if getattr(werbebild, "VERSION", 0) != WERBEBILD_VERSION_ERWARTET:
    st.error(
        f"werbebild.py ist veraltet (Version {getattr(werbebild, 'VERSION', 'alt')}, erwartet "
        f"{WERBEBILD_VERSION_ERWARTET}). Bitte die neue werbebild.py in den Ordner "
        f"{os.path.dirname(os.path.abspath(werbebild.__file__))} legen und die App im Terminal "
        "mit Strg+C beenden und neu starten (streamlit run app.py)."
    )
    st.stop()

def _farbschema_sicherstellen():
    """Fällt die .streamlit/config.toml beim Start nicht an (z. B. anderer Startordner), übernimmt die App das
    Farbschema selbst. Sonst gilt das Standardschema von Streamlit – auf Handys im Dunkelmodus mit schwarzen
    Eingabefeldern."""
    from streamlit import config as st_config
    datei = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "config.toml")
    if st_config.get_option("theme.primaryColor") or not os.path.exists(datei):
        return
    try:
        import tomllib
        with open(datei, "rb") as f:
            einstellungen = tomllib.load(f)
    except Exception:
        return
    for bereich, werte in einstellungen.items():
        for schluessel, wert in (werte or {}).items():
            try:
                st_config.set_option(f"{bereich}.{schluessel}", wert, datei)
            except Exception:
                pass  # in älteren Streamlit-Versionen unbekannte Option
    if st_config.get_option("theme.primaryColor"):
        st.rerun()  # damit schon die erste Sitzung das Farbschema erhält


_farbschema_sicherstellen()

st.set_page_config(page_title="ContentEngine", layout="wide", initial_sidebar_state="expanded")

TONE_OPTIONEN = ["Professionell", "Locker", "Humorvoll", "Emotional", "Sachlich"]
ZIEL_OPTIONEN = prompts.ZIEL_OPTIONEN  # Meffert et al. (2019)
ANREDE_OPTIONEN = ["Sie", "Du"]
CONTENT_TYPE_LABELS = {
    "idee": "Nur Idee",
    "bild": "Nur Bild",
    "caption": "Nur Caption",
    "beides": "Bild + Caption",
}
PLATTFORMEN = {"instagram": "Instagram", "facebook": "Facebook"}
BEITRAGSARTEN = {"feed": "Feed", "karussell": "Karussell", "story": "Story"}
# Plattform-Spezifikationen. Quellen: Meta Ads Guide (Instagram Story/Feed/Karussell, Facebook Story/Bild/Karussell);
# Meta, Hashtags Best Practices 2026. primaertext = Caption, ueberschrift = Headline auf dem Bild, None = keine Vorgabe.
#   tabelle          = Anzeige unter „Vorgaben“
#   verhaeltnis      = Seitenverhältnis: Feed und Karussell 4:5 oder 1:1 (Wahl im Dialog), Storys 9:16
#   caption_empfehlung = Längenempfehlung je Plattform:
#     Instagram: 70–125 Zeichen für Reichweite, 150–300 Wörter für informativen Content
#     Facebook:  ca. 80 Zeichen für Engagement, 100–250 Wörter für Angebote
#   primaertext_max  = technisches Plattformlimit: Instagram max. 2.200 Zeichen (Hashtags zählen mit, daher
#                      Text höchstens 2.100); Facebook max. 63.206 Zeichen
#   hashtags_max     = 5 (Instagram „bis zu 5“, Facebook „3–5“); die App setzt redaktionell genau 5
#   Storys: keine Caption und keine Hashtags
# 2.100 = Instagram-Limit 2.200 Zeichen inkl. Hashtags abzüglich Reserve für die Hashtag-Zeile (5 Hashtags)
_CAPTION_IG = {"primaertext_min": None, "primaertext_max": 2100,
               "caption_empfehlung": "kurz 70–125 Zeichen für Reichweite; lang 150–300 Wörter für informativen Content"}
_CAPTION_FB = {"primaertext_min": None, "primaertext_max": 63206,
               "caption_empfehlung": "kurz ca. 80 Zeichen für Engagement; lang 100–250 Wörter für Angebote"}
VERHAELTNIS_OPTIONEN = {"4:5": "4:5 (Hochformat)", "1:1": "1:1 (quadratisch)"}
_STORY = {"primaertext_min": None, "primaertext_max": None, "caption_empfehlung": None, "hashtags_max": None}
PLATTFORM_SPEZIFIKA = {
    "ig_feed": {"plattform": "Instagram", "art": "Feed",
                "tabelle": {"Format": "4:5 oder 1:1", "Caption": "70–125 Zeichen (Reichweite) · 150–300 Wörter (informativ) · max. 2.200 Zeichen",
                            "Hashtags": "bis zu 5"},
                "verhaeltnis": "4:5", "safezone": "", **_CAPTION_IG, "ueberschrift_max": None, "hashtags_max": 5},
    "ig_karussell": {"plattform": "Instagram", "art": "Karussell",
                     "tabelle": {"Format": "4:5 oder 1:1",
                                 "Caption": "70–125 Zeichen (Reichweite) · 150–300 Wörter (informativ) · max. 2.200 Zeichen",
                                 "Hashtags": "bis zu 5"},
                     "verhaeltnis": "4:5", "safezone": "", **_CAPTION_IG, "ueberschrift_max": None, "hashtags_max": 5},
    "ig_story": {"plattform": "Instagram", "art": "Story",
                 "tabelle": {"Format": "9:16", "Caption": "keine"},
                 "verhaeltnis": "9:16", "safezone": "", **_STORY, "ueberschrift_max": None},
    "fb_feed": {"plattform": "Facebook", "art": "Feed",
                "tabelle": {"Format": "1:1 oder 4:5", "Caption": "ca. 80 Zeichen (Engagement) · 100–250 Wörter (Angebote) · max. 63.206 Zeichen",
                            "Hashtags": "3–5"},
                "verhaeltnis": "4:5", "safezone": "", **_CAPTION_FB, "ueberschrift_max": None, "hashtags_max": 5},
    "fb_karussell": {"plattform": "Facebook", "art": "Karussell",
                     "tabelle": {"Format": "1:1 oder 4:5",
                                 "Caption": "ca. 80 Zeichen (Engagement) · 100–250 Wörter (Angebote) · max. 63.206 Zeichen",
                                 "Hashtags": "3–5"},
                     "verhaeltnis": "4:5", "safezone": "", **_CAPTION_FB, "ueberschrift_max": None, "hashtags_max": 5},
    "fb_story": {"plattform": "Facebook", "art": "Story",
                 "tabelle": {"Format": "9:16", "Caption": "keine"},
                 "verhaeltnis": "9:16", "safezone": "", **_STORY, "ueberschrift_max": None},
}
# Overlay-Modus: „optional“ = Text aufs Bild nur bei kommunikativem Mehrwert
OVERLAY_MODUS = os.environ.get("OVERLAY_MODUS", "optional")
BILDSTIL = "realistisches, glaubwürdiges Foto"
KARUSSELL_MIN, KARUSSELL_MAX = 2, 10
LOGO_STILE = {"kachel": "Logo mit Hintergrund", "frei": "Logo ohne Hintergrund", "aus": "Kein Logo"}


def ist_karussell(plattform_format: str) -> bool:
    return str(plattform_format or "").endswith("_karussell")


def ohne_caption(plattform_format: str) -> bool:
    """Storys (Instagram und Facebook) werden ohne Caption veröffentlicht:
    keine Caption, keine Hashtags – die Botschaft tragen Bild und Headline."""
    return str(plattform_format or "").endswith("_story")


def sk(basis: str, i: int, j: int = 0) -> str:
    """Session-Schlüssel je Beitrag i und Karussell-Karte j (Karte 1 nutzt die bisherigen Schlüssel)."""
    return f"{basis}_{i}" if not j else f"{basis}_{i}_s{j}"


def karussell_slides(post: dict, ctx: dict) -> list:
    """Karten des Karussells (2–10). Fehlen sie in der KI-Antwort, wird mindestens die Titelkarte gebildet."""
    slides = [s for s in (post.get("slides") or []) if isinstance(s, dict)][:KARUSSELL_MAX]
    if not slides:
        slides = [{"headline": post.get("headline", ""), "bild_prompt": post.get("bild_prompt", "")}]
    return [{"headline": str(s.get("headline") or ""), "text": str(s.get("text") or ""),
             "bild_prompt": str(s.get("bild_prompt") or "")} for s in slides]


def karten_post(post: dict, slides: list, j: int) -> dict:
    """Beitrag aus Sicht von Karte j: eigene Bildidee + Hinweis auf die Bildserie (gleicher Stil wie Karte 1)."""
    if not slides:
        return post
    return {**post, "bild_prompt": slides[j]["bild_prompt"] or post.get("bild_prompt", ""),
            "aspekt": f"{post.get('aspekt', '')} – Karte {j + 1}",
            "serie": {"position": f"Karte {j + 1} von {len(slides)}",
                      "referenz": slides[0]["bild_prompt"] or post.get("bild_prompt", "")}}
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]
WOCHENTAGE_LANG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def datum_lang(d) -> str:
    """date oder 'JJJJ-MM-TT' -> 'Freitag, 23. Oktober 2026'"""
    if isinstance(d, str):
        try:
            d = datetime.date.fromisoformat(d)
        except ValueError:
            return d
    return f"{WOCHENTAGE_LANG[d.weekday()]}, {d.day}. {MONATE[d.month - 1]} {d.year}"


def vorgaben_zusammenfassung(spec: dict) -> list:
    """Plattformvorgaben als Textzeilen (beim Karussell zusätzlich die Kartenzahl)."""
    teile = [f"{zeile}: {wert}" for zeile, wert in spec.get("tabelle", {}).items()]
    if spec.get("art") == "Karussell":
        teile.append(f"{KARUSSELL_MIN}–{KARUSSELL_MAX} Karten")
    return teile


def pruefe_texte(spec: dict, caption: str, headline: str) -> list:
    """Prüft Caption/Headline gegen die Plattform-Vorgaben; gibt Hinweise zurück."""
    hinweise = []
    n = len(caption or "")
    if spec["primaertext_max"] and n > spec["primaertext_max"]:
        hinweise.append(f"Primärtext hat {n} Zeichen (max. {spec['primaertext_max']}).")
    if spec["primaertext_min"] and n < spec["primaertext_min"]:
        hinweise.append(f"Primärtext hat {n} Zeichen (mind. {spec['primaertext_min']}).")
    if spec["ueberschrift_max"] and headline and len(headline) > spec["ueberschrift_max"]:
        hinweise.append(f"Überschrift hat {len(headline)} Zeichen (max. {spec['ueberschrift_max']}).")
    if spec["hashtags_max"]:
        anzahl = len(re.findall(r"#\w+", caption or ""))
        if anzahl > spec["hashtags_max"]:
            hinweise.append(f"{anzahl} Hashtags (max. {spec['hashtags_max']}).")
    return hinweise


def format_label(key: str) -> str:
    spec = PLATTFORM_SPEZIFIKA.get(key)
    return f"{spec['plattform']} {spec['art']}" if spec else (key or "")


def verhaeltnis_waehlbar(key: str) -> bool:
    """Feed und Karussell: das Unternehmen wählt 4:5 oder 1:1."""
    return str(key or "").endswith(("_feed", "_karussell"))


def verhaeltnis_aus(ctx: dict) -> str:
    """Gewähltes Seitenverhältnis des Beitrags; Storys immer 9:16."""
    key = (ctx or {}).get("plattform_format") or "ig_feed"
    gewaehlt = (ctx or {}).get("seitenverhaeltnis")
    if verhaeltnis_waehlbar(key) and gewaehlt in VERHAELTNIS_OPTIONEN:
        return gewaehlt
    return PLATTFORM_SPEZIFIKA.get(key, PLATTFORM_SPEZIFIKA["ig_feed"])["verhaeltnis"]


def spec_fuer(key: str, verhaeltnis: str = "") -> dict:
    """Plattform-Spezifikation mit dem gewählten Seitenverhältnis (4:5 oder 1:1 bei Feed/Karussell)."""
    spec = dict(PLATTFORM_SPEZIFIKA.get(key, PLATTFORM_SPEZIFIKA["ig_feed"]))
    if verhaeltnis_waehlbar(key) and verhaeltnis in VERHAELTNIS_OPTIONEN:
        spec["verhaeltnis"] = verhaeltnis
        spec["tabelle"] = {**spec["tabelle"], "Format": verhaeltnis}
    return spec


def plattform_vorgaben(key: str, verhaeltnis: str = "") -> dict:
    """Die Plattform-Vorgaben, die in die Prompts eingehen (FORMAT_KONFIGURATION)."""
    spec = spec_fuer(key, verhaeltnis)
    return {k: spec.get(k) for k in ("plattform", "art", "verhaeltnis", "primaertext_min", "primaertext_max",
                                     "caption_empfehlung", "ueberschrift_max", "hashtags_max", "safezone")}


def bild_erforderlich(content_type: str) -> bool:
    return content_type in ("bild", "beides")
NAV_ITEMS = [("chat", "Beitrag erstellen"), ("vorschlaege", "Posting Vorschläge"), ("kalender", "Content-Kalender"), ("profil", "Profil")]
LOGO_VERZEICHNIS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "logos")

# --- Design-Tokens & globales CSS: ausgelagert in style.css --------------

STYLESHEET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css")
SCHRIFTEN_HTML = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">'
)


def stylesheet_laden() -> str:
    """Liest das App-Stylesheet (style.css) und gibt es mit den Schrift-Links als HTML zurück."""
    with open(STYLESHEET, encoding="utf-8") as datei:
        return SCHRIFTEN_HTML + "<style>\n" + datei.read() + "</style>"


st.html(stylesheet_laden())

# --- Session-Defaults ------------------------------------------------------

# A/B-Test: Evaluationsmodus nur über den versteckten Link-Parameter
# ?evaluation=1 – dann erzeugt EIN Klick beide Varianten aus exakt denselben Dialogangaben:
#   A = komplette Prompt-Architektur (drei Routen, Theorien, Qualitätssicherung)
#   B = naiver Prompt ohne Architektur, wie ihn ein:e Unternehmer:in ohne Vorwissen formulieren würde
try:
    _ev = str(st.query_params.get("evaluation", "")).lower()
except Exception:
    _ev = ""
if _ev in ("1", "true", "ja"):
    st.session_state.evaluation = True
elif _ev in ("0", "false", "nein"):
    st.session_state.evaluation = False


if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "page" not in st.session_state:
    st.session_state.page = "chat"
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "chat_context" not in st.session_state:
    st.session_state.chat_context = {}
if "vorschlaege" not in st.session_state:
    st.session_state.vorschlaege = None
if "kalender_monat" not in st.session_state:
    heute = datetime.date.today()
    st.session_state.kalender_monat = (heute.year, heute.month)


def initialen(name: str) -> str:
    teile = [t for t in name.strip().split() if t]
    if not teile:
        return "NU"
    if len(teile) == 1:
        return teile[0][:2].upper()
    return (teile[0][0] + teile[1][0]).upper()


def save_schrift(uploaded_file, user_id: int) -> str:
    """Speichert die CI-Schriftdatei (TTF/OTF) des Unternehmens."""
    verzeichnis = os.path.join(os.path.dirname(LOGO_VERZEICHNIS), "fonts")
    os.makedirs(verzeichnis, exist_ok=True)
    ext = os.path.splitext(uploaded_file.name)[1].lower() or ".ttf"
    pfad = os.path.join(verzeichnis, f"{user_id}{ext}")
    with open(pfad, "wb") as f:
        f.write(uploaded_file.getvalue())
    return pfad


def zeige_schrift_status(profile: dict):
    """Zeigt an, ob die CI-Schrift für die Werbebilder gefunden wurde."""
    if not (profile.get("ci_schrift") or profile.get("ci_schrift_datei")):
        st.caption("ℹ️ Keine CI-Schrift angegeben – Werbebilder nutzen eine Standardschrift.")
        return
    pfad, quelle = werbebild.finde_schriftdatei(profile.get("ci_schrift", ""), profile.get("ci_schrift_datei", ""))
    if pfad:
        st.caption(f"✓ CI-Schrift für Werbebilder gefunden ({quelle}).")
    else:
        st.warning(
            f"Die Schrift „{profile.get('ci_schrift')}“ wurde weder auf diesem Rechner noch bei Google Fonts "
            "gefunden – Werbebilder nutzen vorerst eine Ersatzschrift. Bitte unter „Profil“ die "
            "Schriftdatei (.ttf/.otf) hochladen."
        )


def save_logo(uploaded_file, user_id: int) -> str:
    os.makedirs(LOGO_VERZEICHNIS, exist_ok=True)
    ext = os.path.splitext(uploaded_file.name)[1] or ".png"
    pfad = os.path.join(LOGO_VERZEICHNIS, f"{user_id}{ext}")
    with open(pfad, "wb") as f:
        f.write(uploaded_file.getvalue())
    return pfad


def _slide_urls(eintrag: dict) -> list:
    """Kartenbilder eines gespeicherten Karussells (leer bei Einzelbild)."""
    try:
        return json.loads((eintrag or {}).get("slide_urls") or "[]")
    except (ValueError, TypeError):
        return []


def render_instagram_simulation(unternehmensname: str, caption: str, bild_bytes=None, image_url: str = "",
                                plattform_format: str = "", slides=None, verhaeltnis: str = ""):
    """Rein clientseitige Simulation der Instagram-Veröffentlichung.

    Es wird bewusst KEIN echter API-Call an die Meta Graph API gemacht –
    das würde ein verknüpftes Instagram Business Account je Testnutzer:in
    voraussetzen, was im Rahmen der Evaluation nicht praktikabel ist.
    """
    with st.container(border=True):
        name = format_label(plattform_format) or "Social-Media"
        st.caption(f"📱 Simulierte {name}-Vorschau – es wird kein echter Account verknüpft oder gepostet.")
        st.html(
            f"""
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
              <div class="ce-avatar-circle" style="width:32px; height:32px;">{initialen(unternehmensname or 'NU')}</div>
              <div style="font-weight:600; font-size:14px;">{unternehmensname or 'Dein Unternehmen'}</div>
            </div>
            """
        )
        karten = [bild_fuer_anzeige(b) if isinstance(b, str) else b for b in (slides or []) if b]
        karten = [b for b in karten if b]
        if len(karten) > 1:  # Karussell: Karten zum Wischen
            spec = spec_fuer(plattform_format, verhaeltnis)
            zeige_karussell_streifen(karten, -1, spec.get("verhaeltnis", "4:5"))
        elif bild_bytes:
            st.image(bild_bytes, width='stretch')
        elif bild_fuer_anzeige(image_url):
            st.image(bild_fuer_anzeige(image_url), width='stretch')
        else:
            st.html(
                "<div style='aspect-ratio:1/1;background:var(--accent-soft);border-radius:8px;"
                "display:flex;align-items:center;justify-content:center;color:var(--accent);"
                "font-size:13px;'>KI-Bild (wird im echten Betrieb von der KI generiert)</div>"
            )
        if caption:  # Storys haben keine Caption
            st.caption(caption)


# --- Hilfsfunktionen: Beitragserstellung ------------------

def _json_aus_text(raw: str, muster: str, ersatz):
    """Erst direkt als JSON lesen, sonst den ersten [..]- bzw. {..}-Block."""
    try:
        return json.loads(raw)
    except ValueError:
        treffer = re.search(muster, raw)
        return json.loads(treffer.group(0)) if treffer else ersatz


def _json_tolerant(raw: str):
    """Rettet fast-JSON wie `headline: "…", post_text: "…", hashtags: [...]` (Schlüssel ohne Anführungszeichen,
    fehlende geschweifte Klammern, Komma am Ende). Gibt ein dict zurück oder None."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    if not t.startswith("{"):
        t = "{" + t + "}"
    t = re.sub(r'(?m)(^|[{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', t)
    t = re.sub(r",\s*([}\]])", r"\1", t)
    try:
        p = json.loads(t)
    except ValueError:
        return None
    return p if isinstance(p, dict) and ("post_text" in p or "headline" in p) else None


def _post_formatieren(raw: str, aspekt: str, nummer: int) -> dict:
    """Bringt einen Beitrag aus der KI-Antwort in das Format der App; rettet fast-JSON (_json_tolerant),
    damit nie Rohtext wie `headline: "…", post_text: "…"` in der Caption landet."""
    try:
        p = _json_aus_text(raw, r"\{[\s\S]*\}", None)
    except ValueError:
        p = None
    if not isinstance(p, dict):
        p = _json_tolerant(raw) or {"post_text": raw}
    if not isinstance(p, dict):
        p = {}
    hashtags = [str(h).strip() for h in p["hashtags"] if str(h).strip() not in ("", "#")] if isinstance(p.get("hashtags"), list) else []
    text = str(p.get("post_text") or "").strip()
    slides = p.get("slides") if isinstance(p.get("slides"), list) else []
    return {
        "aspekt_nummer": nummer,
        "aspekt": aspekt,
        "headline": str(p.get("headline") or ""),
        "post_text": text,
        "hashtags": hashtags,
        "hashtags_string": " ".join(hashtags),
        "full_caption": text + "\n\n" + " ".join(hashtags) if hashtags else text,
        "cta": str(p.get("cta") or ""),
        "bild_prompt": str(p.get("bild_prompt") or ""),
        # Karussell: Karten (headline + bild_prompt), höchstens 10
        "slides": [{"headline": str((k or {}).get("headline") or "") if isinstance(k, dict) else "",
                    "text": str((k or {}).get("text") or "") if isinstance(k, dict) else "",
                    "bild_prompt": str((k or {}).get("bild_prompt") or "") if isinstance(k, dict) else ""}
                   for k in slides[:10]],
    }


def erzeuge_beitraege(payload: dict) -> dict:
    """Beitragserstellung mit EINEM Generierungsaufruf: Der Gesamtprompt
    (prompts.GESAMT_SYSTEM: Rahmenmodell, Werbebotschaft, Bildgestaltung) liefert alle drei Beiträge samt
    Unteraspekt, Texten und Bildidee. Danach folgen Bildmodell und Qualitätssicherung."""
    p = payload["prompts"]
    anzahl = int(payload.get("anzahl_posts") or prompts.ANZAHL_BEITRAEGE)
    client = _get_openai_client()
    # JSON-Modus: Die API liefert garantiert ein gültiges JSON-Objekt (verhindert Rohtext in der Caption)
    antwort = client.chat.completions.create(
        model=p["modell"], temperature=p["temperatur"], response_format={"type": "json_object"},
        messages=[{"role": "system", "content": p["system"]}, {"role": "user", "content": p["user"]}])
    roh = _json_aus_text(antwort.choices[0].message.content or "{}", r"\{[\s\S]*\}", {})
    beitraege = [b for b in (roh.get("beitraege") or roh.get("posts") or []) if isinstance(b, dict)] \
        if isinstance(roh, dict) else []
    posts = [_post_formatieren(json.dumps(b, ensure_ascii=False), str(b.get("unteraspekt") or ""), n + 1)
             for n, b in enumerate(beitraege[:anzahl])]
    if not bild_erforderlich(payload.get("content_type", "beides")):
        for post in posts:
            post["bild_prompt"] = ""

    return {
        "thema": payload.get("thema") or "",
        "kommunikationsziel": payload.get("kommunikationsziel") or "",
        "inhaltlicher_contenttyp": payload.get("inhaltlicher_contenttyp") or "",
        "contenttyp_quelle": payload.get("contenttyp_quelle") or "",
        "content_type": payload.get("content_type") or "",
        "plattform_format": payload.get("plattform_format") or "",
        "anzahl_posts": len(posts),
        "posts": posts,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def generiere_vorschlaege(profile: dict, context: dict, anzahl: int = 3, bisherige_aspekte=None) -> dict:
    import random

    payload = {
        "thema": context.get("thema", ""),
        "wirkungsebene": context.get("wirkungsebene", ""),
        "kommunikationsziel": context.get("kommunikationsziel", "Bekanntheit"),
        # bestätigter inhaltlicher Contenttyp (informativ | interaktional | transaktional)
        "inhaltlicher_contenttyp": context.get("inhaltlicher_contenttyp", ""),
        "contenttyp_quelle": context.get("contenttyp_quelle", ""),
        "klaerungen": context.get("klaerungen", []),  # Rückfragen und Antworten aus dem Klärungsschritt
        "content_type": context.get("content_type", "beides"),
        "plattform_format": context.get("plattform_format", "ig_feed"),
        "plattform_vorgaben": plattform_vorgaben(context.get("plattform_format", "ig_feed"), verhaeltnis_aus(context)),
        "angebot": context.get("angebot", ""),
        "personen_im_bild": context.get("personen_im_bild"),
        "anzahl_posts": anzahl,
        "bisherige_aspekte": bisherige_aspekte or [],
        "varianten_seed": random.randint(1, 1_000_000),
        "unternehmensname": profile.get("unternehmensname", ""),
        "standort": profile.get("ort") or profile.get("standort", ""),
        "branche": profile.get("branche", ""),
        "tone_of_voice": profile.get("tone_of_voice", "Professionell"),
        # Unternehmenswissen aus dem Profil
        "zielgruppe": profile.get("zielgruppe", ""),
        "anrede": profile.get("anrede", "Sie"),
        "strategie": profile.get("strategie", ""),
        "positionierung": profile.get("positionierung", ""),
        "fakten": prompts.profil_fakten_text(profile),  # Anschrift, Öffnungszeiten, Kontakt
        "stilbeispiel": profile.get("stilbeispiel", ""),
        "ci_besonderheiten": profile.get("ci_besonderheiten", ""),
        "pflicht_hashtags": hinweise.pflicht_hashtags(profile),
        # Parameter, die die App liefert (statt sie im Prompt fest vorzugeben)
        "hashtag_regel": hinweise.hashtag_regel_gilt(context.get("plattform_format", "ig_feed")),
        "hashtag_anzahl": hinweise.HASHTAG_ANZAHL,
        "automatische_hashtags": (hinweise.feste_hashtags(profile) + hinweise.pflicht_hashtags(profile)
                                  if hinweise.hashtag_regel_gilt(context.get("plattform_format", "ig_feed")) else []),
        "overlay_modus": OVERLAY_MODUS,
        "bildstil": BILDSTIL,
    }
    # Alle Prompts kommen aus prompts.py. Sie werden aus den ROHEN Profilfeldern gebaut (Anschrift,
    # Öffnungszeiten … einzeln), ergänzt um die Eckdaten der Anfrage.
    payload.update(prompts.post_generierung({**payload, **profile}))
    return erzeuge_beitraege(payload)


def setze_vorschlaege(data, kontext: dict = None):
    """Neue Vorschläge speichern und alte, KI-generierte Bilder der vorherigen
    Runde verwerfen – sonst hängt Bild 1 der alten Runde an Post 1 der neuen."""
    if isinstance(data, dict):
        # Momentaufnahme der Dialog-Entscheidungen (Format, Seitenverhältnis, Ziel …): Die Vorschläge zeigen immer
        # das Format, mit dem sie erzeugt wurden – auch wenn danach eine neue Anfrage gestartet wurde.
        data = {**data, "kontext": dict(kontext if kontext is not None else (st.session_state.get("chat_context") or {}))}
    st.session_state.vorschlaege = data
    # Auch Eingabefelder (Caption, Headline, CTA, Upload) der alten Runde verwerfen –
    # Streamlit-Widgets mit festem key würden sonst die ALTEN Texte weiter anzeigen.
    praefixe = ("ki_bild_", "ki_prompt_", "ki_fehler_", "ki_modell_", "caption_", "headline_", "cta_",
                "textbild_", "textflaeche_", "deckkraft_", "angebot_", "bild_", "bildquelle_", "aktion_", "datum_", "zeit_", "feedback_",
                "faktencheck_", "hashtags_", "bildwunsch_", "ueberarbeitet_", "slide_", "logo_", "kartentext_",
                "textexp_")
    for key in [k for k in st.session_state.keys() if str(k).startswith(praefixe)]:
        del st.session_state[key]


# --- OpenAI: Bildgenerierung ------------------------------------------------
#
# Die App erzeugt die Bilder selbst mit dem OpenAI-Key aus der .env.

# Bildgröße je Seitenverhältnis; werbebild.py schneidet exakt auf 1080×1350, 1080×1080 bzw. 1080×1920 zu.
BILDGROESSE = {"4:5": "1024x1536", "9:16": "1024x1536", "1:1": "1024x1024"}


def standard_headline(post: dict, context: dict = None) -> str:
    """KI-Headline > Angebot (bei Aktionen muss das Angebot sofort erkennbar sein) > Aspekt."""
    angebot = ((context or {}).get("angebot") or "").strip()
    return (post.get("headline") or angebot or post.get("aspekt") or "").strip()


def basis_daten(context: dict) -> dict:
    """Variante B: nur die Dialogangaben (kein Unternehmensprofil), Wirkung und Art so benannt wie im Dialog."""
    wirkung = WIRKUNGSEBENEN.get(context.get("wirkungsebene"))
    typ = CONTENTTYP_OPTIONEN.get(context.get("inhaltlicher_contenttyp"))
    return {**context, "wirkung_label": wirkung["label"] if wirkung else "",
            "contenttyp_label": typ[0] if typ else "",
            "plattform_vorgaben": plattform_vorgaben(context.get("plattform_format", "ig_feed"), verhaeltnis_aus(context))}


def neutrales_profil(profile: dict) -> dict:
    """Variante B: Bild ohne Unternehmensdaten – keine CI-Farben/-Schrift, kein Logo (Headline neutral)."""
    return {**profile, "ci_farbe": "neutral", "ci_farbe_sekundaer": "", "ci_schrift": "", "ci_schrift_datei": "",
            "logo_path": ""}


def baue_bild_prompt(profile: dict, context: dict, post: dict, aenderungswunsch: str = "") -> str:
    if post.get("ab_variante") == "B":  # Variante B: Bild-Prompt ohne Architektur und ohne Unternehmensdaten
        daten = basis_daten(context)
        karte = post.get("serie", {}).get("position", "") if isinstance(post.get("serie"), dict) else ""
        return prompts.basis_bild_prompt(daten, karte) + (f" {aenderungswunsch.strip()}" if aenderungswunsch.strip() else "")
    ctx = {**context, "plattform_vorgaben": plattform_vorgaben(context.get("plattform_format", "ig_feed"), verhaeltnis_aus(context)),
           "bildstil": BILDSTIL}
    return prompts.bild_prompt(profile, ctx, post, aenderungswunsch, OVERLAY_MODUS)


def generiere_ki_bild(prompt: str, plattform_format: str = "ig_feed", client=None, verhaeltnis: str = ""):
    """Erzeugt EIN Bild mit gpt-image-2.5-flare. Gibt (bild_bytes, modell) zurück.
    Bewusst Bytes statt URL: OpenAI-Bild-URLs laufen nach ca. 1 Stunde ab. Thread-sicher."""
    client = client or _get_openai_client()
    verhaeltnis = verhaeltnis or PLATTFORM_SPEZIFIKA.get(plattform_format, PLATTFORM_SPEZIFIKA["ig_feed"])["verhaeltnis"]
    result = client.images.generate(model=OPENAI_IMAGE_MODEL, prompt=prompt, n=1, quality=OPENAI_IMAGE_QUALITY,
                                    size=BILDGROESSE.get(verhaeltnis, BILDGROESSE["4:5"]))
    bild = result.data[0]
    if getattr(bild, "b64_json", None):
        return base64.b64decode(bild.b64_json), OPENAI_IMAGE_MODEL
    if getattr(bild, "url", None):
        r = requests.get(bild.url, timeout=60)
        r.raise_for_status()
        return r.content, OPENAI_IMAGE_MODEL
    raise RuntimeError("Die API hat weder Bilddaten noch eine URL zurückgegeben.")


def zeige_bildmodell(schluessel: str, bild_quelle: str):
    """Unter dem KI-Bild: mit welchem Modell es erstellt wurde (Transparenz, Dokumentation der Evaluation)."""
    info = st.session_state.get(schluessel)
    if bild_quelle != "ki" or not info:
        return
    st.caption(f"Erstellt mit {info['modell']}")


# --- Karussell: Karten durchklicken, Bilder für alle Karten -------------------------------------

def karussell_bilder_alle(profile: dict, ctx: dict, posts: list, plattform: str):
    """Erzeugt die fehlenden KI-Bilder ALLER Karten der übergebenen Karussell-Vorschläge (bis zu 3 × 10 Bilder).
    Einträge None werden übersprungen (der Index i bleibt erhalten). Höchstens 5 Anfragen gleichzeitig; bei
    Erreichen des Bild-Limits pro Minute wartet der OpenAI-Client automatisch und versucht es erneut."""
    jobs = {}
    for i, post in enumerate(posts):
        if post is None:
            continue
        slides = karussell_slides(post, ctx)
        for j in range(len(slides)):
            if (st.session_state.get(sk("bildquelle", i, j), "ki") == "ki"
                    and not st.session_state.get(sk("ki_bild", i, j))):
                jobs[(i, j)] = baue_bild_prompt(profile, ctx, karten_post(post, slides, j))
    if not jobs:
        return
    client = _get_openai_client().with_options(max_retries=6)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(jobs))) as pool:
        futures = {pool.submit(generiere_ki_bild, prompt, plattform, client, verhaeltnis_aus(ctx)): ij
                   for ij, prompt in jobs.items()}
        for f in concurrent.futures.as_completed(futures):
            i, j = futures[f]
            st.session_state[sk("ki_prompt", i, j)] = jobs[(i, j)]
            try:
                bild, modell = f.result()
                st.session_state[sk("ki_bild", i, j)] = bild
                st.session_state[sk("ki_modell", i, j)] = {"modell": modell}
                st.session_state.pop(sk("ki_fehler", i, j), None)
            except Exception as e:
                st.session_state[sk("ki_fehler", i, j)] = str(e)


def karussell_bilder_erzeugen(i: int, slides: list, profile: dict, ctx: dict, post: dict, plattform: str):
    """Fehlende Kartenbilder EINES Karussells nachträglich erzeugen (z. B. nach einem Fehler)."""
    karussell_bilder_alle(profile, ctx, [post if n == i else None for n in range(i + 1)], plattform)


def _karte_gewaehlt(i: int):
    wahl = st.session_state.get(f"slide_wahl_{i}")
    if wahl:
        st.session_state[f"slide_{i}"] = int(wahl) - 1
        st.session_state[f"karte_sprung_{i}"] = True  # Seite zum Karussell dieses Beitrags springen lassen


def karussell_steuerung(i: int, j: int, slides: list, profile: dict, ctx: dict, post: dict, plattform: str):
    """Unter dem Wisch-Streifen: welche Karte bearbeitet wird + Bilder für alle Karten erzeugen."""
    n = len(slides)
    st.session_state[f"slide_wahl_{i}"] = str(j + 1)
    st.segmented_control("Bild bearbeiten für Karte", [str(x + 1) for x in range(n)], key=f"slide_wahl_{i}",
                         on_change=_karte_gewaehlt, args=(i,), width="stretch",
                         help="Eigenes Foto, neues KI-Bild und Änderungswunsch gelten für die gewählte Karte. "
                              "Die Texte aller Karten bearbeitest du unter „Texte der Karten und Logo“.")
    if n < KARUSSELL_MIN:
        st.caption("Die KI hat nur eine Karte geliefert – ein Karussell braucht mindestens 2. "
                   "Bitte neue Vorschläge erstellen.")
    fehlend = sum(1 for x in range(n) if st.session_state.get(sk("bildquelle", i, x), "ki") == "ki"
                  and not st.session_state.get(sk("ki_bild", i, x)))
    if fehlend:
        if st.button(f"✨ KI-Bilder für alle Karten erzeugen ({fehlend} fehlen)", key=f"slides_erzeugen_{i}",
                     width="stretch"):
            with st.spinner(f"Erzeuge {fehlend} Bilder …"):
                karussell_bilder_erzeugen(i, slides, profile, ctx, post, plattform)
            st.rerun()


@st.cache_data(max_entries=300, show_spinner=False)
def _werbebild_cache(basis_bytes: bytes, headline: str, plattform: str, ci_farbe: str, ci_farbe2: str,
                     schrift_name: str, logo_pfad: str, schrift_datei: str, logo_stil: str,
                     version: int, verhaeltnis: str = "", textflaeche: bool = False, ki_bild: bool = False,
                     flaeche_deckkraft: int = 100) -> bytes:
    """Zwischenspeicher: jede Karte wird nur neu gezeichnet, wenn sich Bild oder Text ändern.
    version (werbebild.VERSION) gehört zum Cache-Schlüssel: neue Darstellung -> neu zeichnen."""
    return werbebild.erstelle_werbebild(
        basis_bytes, headline=headline, plattform_format=plattform, ci_farbe=ci_farbe,
        ci_farbe_sekundaer=ci_farbe2, schrift_name=schrift_name, logo_pfad=logo_pfad, schrift_datei=schrift_datei,
        logo_stil=logo_stil, verhaeltnis=verhaeltnis, textflaeche=textflaeche, ki_bild=ki_bild,
        flaeche_deckkraft=flaeche_deckkraft)


def textflaeche_haken(i: int):
    """Haken im Bereich „Text und Logo auf dem Bild“: Headline mit Fläche in der Primärfarbe
    hinterlegen, falls die Schrift auf dem Bild schlecht lesbar ist. Standard: aus (Schrift in der Primärfarbe)."""
    if st.session_state.get(f"textbild_{i}", True):
        st.checkbox("Headline mit Farbfläche hinterlegen (bessere Lesbarkeit)", key=f"textflaeche_{i}",
                    help="Legt hinter die Headline ein Feld in der Primärfarbe aus dem Profil; die Schrift steht "
                         "darauf in der Sekundärfarbe.")
        if st.session_state.get(f"textflaeche_{i}", False):
            st.slider("Deckkraft der Farbfläche", 0, 100, 100, step=5, format="%d %%", key=f"deckkraft_{i}",
                      help="100 % = Fläche deckend, 0 % = Fläche unsichtbar (nur die Schrift bleibt).")


def werbebild_fuer(i: int, j: int, basis_bytes: bytes, headline: str, plattform: str, profile: dict,
                   verhaeltnis: str = "") -> bytes:
    """Fertiges Bild einer Karte mit denselben Einstellungen wie in der Vorschlagskarte."""
    text = st.session_state.get(f"textbild_{i}", True)
    return _werbebild_cache(
        basis_bytes, headline if text else "", plattform,
        profile.get("ci_farbe") or "#C9532E", profile.get("ci_farbe_sekundaer") or "",
        profile.get("ci_schrift") or "", profile.get("logo_path") or "", profile.get("ci_schrift_datei") or "",
        st.session_state.get(f"logo_{i}", "kachel"), werbebild.VERSION, verhaeltnis,
        st.session_state.get(f"textflaeche_{i}", False),
        st.session_state.get(sk("bildquelle", i, j), "ki") == "ki",  # Kennzeichnung „KI-generiert“ nur bei KI-Bildern
        st.session_state.get(f"deckkraft_{i}", 100))  # Deckkraft der Farbfläche hinter der Headline


@st.cache_data(max_entries=300, show_spinner=False)
def _vorschau_base64(bild: bytes, breite: int = 540) -> str:
    im = Image.open(io.BytesIO(bild)).convert("RGB")
    im.thumbnail((breite, breite * 2))
    puffer = io.BytesIO()
    im.save(puffer, format="JPEG", quality=82)
    return base64.b64encode(puffer.getvalue()).decode()


def zeige_karussell_streifen(bilder: list, aktiv: int = -1, verhaeltnis: str = "4:5", springen: bool = False):
    """Karussell zum Wischen: alle Karten nebeneinander, seitlich scrollbar (am Handy wischen,
    am Rechner mit Trackpad/Shift+Mausrad oder der Scrollleiste). Fehlende Bilder als Platzhalter."""
    w, h = (int(x) for x in verhaeltnis.split(":")) if ":" in verhaeltnis else (4, 5)
    karten = []
    for n, bild in enumerate(bilder):
        rahmen = "2px solid var(--accent)" if n == aktiv else "1px solid rgba(0,0,0,.08)"
        if isinstance(bild, (bytes, bytearray)):
            inhalt = f"<img src='data:image/jpeg;base64,{_vorschau_base64(bytes(bild))}' style='width:100%;display:block'>"
        elif isinstance(bild, str) and bild and os.path.exists(bild):  # lokal gespeichertes Kartenbild
            with open(bild, "rb") as datei:
                inhalt = (f"<img src='data:image/jpeg;base64,{_vorschau_base64(datei.read())}' "
                          "style='width:100%;display:block'>")
        elif isinstance(bild, str) and bild:
            inhalt = f"<img src='{html.escape(bild)}' style='width:100%;display:block'>"
        else:
            inhalt = (f"<div style='aspect-ratio:{w}/{h};display:flex;align-items:center;justify-content:center;"
                      "background:var(--accent-soft);color:var(--accent);font-size:13px'>noch kein Bild</div>")
        karten.append(
            f"<div style='flex:0 0 86%;scroll-snap-align:center;border-radius:10px;overflow:hidden;border:{rahmen};"
            f"position:relative'>{inhalt}<div style='position:absolute;top:8px;right:8px;background:rgba(0,0,0,.55);"
            f"color:#fff;font-size:12px;padding:2px 8px;border-radius:10px'>{n + 1}/{len(bilder)}</div></div>")
    import uuid
    streifen_id = f"ce-karussell-{uuid.uuid4().hex[:10]}"  # eindeutig, auch bei mehreren Karussells auf der Seite
    sprung = ""
    if aktiv >= 0:
        # Gewählte Karte im Streifen anzeigen: dorthin scrollen (Skript, weil CSS die Scrollposition nicht setzen kann).
        sprung = (
            "<script>(function(){var n=0;function los(){var s=document.getElementById('" + streifen_id + "');"
            "if(!s){if(n++>40)return;setTimeout(los,50);return;}var k=s.children[" + str(aktiv) + "];if(!k)return;"
            "s.scrollTo({left:k.offsetLeft-(s.clientWidth-k.clientWidth)/2,behavior:'smooth'});"
            + ("s.scrollIntoView({behavior:'smooth',block:'center'});" if springen else "") + "}"
            "setTimeout(los,60);})();</script>")
    st.html("<div class='ce-karussell' id='" + streifen_id + "' style='display:flex;gap:10px;overflow-x:auto;"
            "scroll-snap-type:x mandatory;padding-bottom:6px;-webkit-overflow-scrolling:touch;position:relative'>"
            + "".join(karten) + "</div>"
            "<div style='font-size:12px;opacity:.7;text-align:center;margin-top:2px'>← zur Seite wischen →</div>")
    if sprung:  # eigenes Element, damit das Skript bei jeder Auswahl neu ausgeführt wird
        st.html(sprung, unsafe_allow_javascript=True)


def karussell_fertige_bilder(i: int, slides: list, plattform: str, profile: dict, ctx: dict = None) -> list:
    """Alle Karten als fertige Bilder (None, wenn einer Karte noch das Bild fehlt)."""
    bilder = []
    for j, slide in enumerate(slides):
        if st.session_state.get(sk("bildquelle", i, j), "ki") == "upload":
            datei = st.session_state.get(sk("bild", i, j))
            basis = datei.getvalue() if datei else None
        else:
            basis = st.session_state.get(sk("ki_bild", i, j))
        if not basis:
            bilder.append(None)
            continue
        headline = hart_kuerzen(st.session_state.get(sk("headline", i, j), slide["headline"]),
                                PLATTFORM_SPEZIFIKA.get(plattform, {}).get("ueberschrift_max"))
        bilder.append(werbebild_fuer(i, j, basis, headline, plattform, profile,
                                     verhaeltnis=verhaeltnis_aus(ctx or {"plattform_format": plattform})))
    return bilder


def generiere_bilder_parallel(profile: dict, context: dict, posts: list) -> None:
    """Erzeugt die KI-Bilder für ALLE Posts gleichzeitig (eine Anfrage pro Post,
    parallel in Threads). Die Wartezeit entspricht damit etwa EINEM Bild statt drei.
    Ergebnisse landen in st.session_state (nur im Haupt-Thread geschrieben)."""
    client = _get_openai_client()
    plattform = context.get("plattform_format", "ig_feed")
    # Karussell: zunächst nur die Titelkarte; die übrigen Karten per Knopf (Kosten, Bild-Limit pro Minute)
    bild_prompts = {i: baue_bild_prompt(profile, context, {**p, "bild_prompt": p.get("bild_prompt") or (
        (p.get("slides") or [{}])[0].get("bild_prompt", "") if isinstance((p.get("slides") or [{}])[0], dict) else "")})
        for i, p in enumerate(posts)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(posts))) as pool:
        futures = {pool.submit(generiere_ki_bild, bild_prompts[i], plattform, client,
                               verhaeltnis_aus(context)): i for i in bild_prompts}
        for f in concurrent.futures.as_completed(futures):
            i = futures[f]
            st.session_state[f"ki_prompt_{i}"] = bild_prompts[i]
            try:
                bild, modell = f.result()
                st.session_state[f"ki_bild_{i}"] = bild
                st.session_state[f"ki_modell_{i}"] = {"modell": modell}
                st.session_state.pop(f"ki_fehler_{i}", None)
            except Exception as e:
                st.session_state[f"ki_fehler_{i}"] = str(e)


# --- Zeichenlimits der Plattformen DURCHSETZEN -------------------------------
#
# Die KI bekommt die Limits zwar mitgeschickt, Sprachmodelle zählen Zeichen aber
# unzuverlässig. Deshalb wird hier jeder Text verbindlich geprüft:
#   1. überzählige Hashtags entfernen,
#   2. zu lange/zu kurze Texte von GPT passend umformulieren lassen (max. 2 Versuche),
#   3. falls es danach immer noch nicht passt: harte Kürzung an einer Wortgrenze.
# Ergebnis: Der angezeigte Text überschreitet das Limit NIE.

def begrenze_hashtags(text: str, maximum) -> str:
    if not maximum:
        return text
    gesehen = 0

    def ersetze(m):
        nonlocal gesehen
        gesehen += 1
        return m.group(0) if gesehen <= maximum else ""
    return re.sub(r"\s?#\w+", ersetze, text).strip()


def hart_kuerzen(text: str, maximum) -> str:
    """Letzte Sicherung: kürzt an einer Wortgrenze und hängt '…' an (zählt mit)."""
    text = (text or "").strip()
    if not maximum or len(text) <= maximum:
        return text
    schnitt = text[: maximum - 1]
    if " " in schnitt[int(maximum * 0.5):]:
        schnitt = schnitt[: schnitt.rfind(" ")]
    schnitt = re.sub(r"[\s#,;:–-]+$", "", schnitt)
    return (schnitt + "…")[:maximum]


def _passt(text: str, minimum, maximum) -> bool:
    return (not maximum or len(text) <= maximum) and (not minimum or len(text) >= minimum)


def text_auf_limit_bringen(client, text: str, minimum, maximum, art: str, hashtags_max=None,
                           headline: str = "") -> str:
    """Bringt einen Text in die Zeichenvorgabe. Thread-sicher (kein st.session_state)."""
    text = begrenze_hashtags((text or "").strip(), hashtags_max)
    if _passt(text, minimum, maximum) or client is None:
        return hart_kuerzen(text, maximum)
    bereich = (f"zwischen {minimum} und {maximum}" if minimum and maximum else
               f"höchstens {maximum}" if maximum else f"mindestens {minimum}")
    anweisung = prompts.kuerzen_anweisung(text, art, bereich, hashtags_max, headline=headline)
    messages = [{"role": "system", "content": prompts.KUERZEN_SYSTEM},
                {"role": "user", "content": anweisung}]
    kandidat = text
    for _ in range(2):
        try:
            antwort = client.chat.completions.create(model=OPENAI_MODEL, messages=messages, temperature=0.4)
            kandidat = begrenze_hashtags((antwort.choices[0].message.content or "").strip().strip('"„“'),
                                         hashtags_max)
        except Exception:
            break
        if _passt(kandidat, minimum, maximum):
            return kandidat
        messages += [{"role": "assistant", "content": kandidat},
                     {"role": "user", "content": prompts.kuerzen_korrektur(len(kandidat), bereich)}]
    return hart_kuerzen(kandidat if len(kandidat) >= len(text) * 0.3 else text, maximum)


def format_name(plattform_format: str) -> str:
    return "Story" if ohne_caption(plattform_format) else "Karussell" if ist_karussell(plattform_format) else "Feed"


def textelemente(post: dict, plattform_format: str) -> dict:
    """Textelemente eines Beitrags – für Selbstprüfung und Überarbeitung in ALLEN Formaten gleich aufgebaut: Feed = Headline + Caption, Karussell = Karten (Headline + Text) + Caption,
    Story = Headline + CTA (keine Caption)."""
    if ohne_caption(plattform_format):
        return {"headline": post.get("headline", ""), "cta": post.get("cta", "")}
    caption = (post.get("post_text") or hinweise.trenne_hashtags(post.get("full_caption", ""))[0]).strip()
    if ist_karussell(plattform_format):
        return {"karten": [{"headline": k["headline"], "text": k["text"]} for k in karussell_slides(post, {})],
                "caption": caption}
    return {"headline": post.get("headline", ""), "caption": caption}


def textelemente_uebernehmen(post: dict, neu: dict) -> dict:
    """Schreibt überarbeitete Textelemente in den Beitrag zurück (Bildideen der Karten bleiben erhalten)."""
    post = dict(post)
    for feld in ("headline", "cta"):
        if feld in neu:
            post[feld] = neu[feld]
    if "caption" in neu:
        tags = post.get("hashtags") or []
        post["post_text"] = neu["caption"]
        post["full_caption"] = neu["caption"] + "\n\n" + " ".join(tags) if tags else neu["caption"]
    if "karten" in neu:
        post["slides"] = [{**alt, "headline": n["headline"], "text": n["text"]}
                          for alt, n in zip(karussell_slides(post, {}), neu["karten"])]
    return post


def textelemente_ueberarbeiten(client, elemente: dict, feedback: dict, profile: dict, context: dict,
                               maximum=None, headline_max=None) -> dict:
    """Überarbeitet ALLE Textelemente gemeinsam nach dem Feedback (UEBERARBEITEN_SYSTEM) – genutzt von der
    automatischen Selbstprüfung und vom Knopf „Text mit dem Feedback überarbeiten“. Fehlt ein Element in der
    Antwort, bleibt der bisherige Wortlaut erhalten."""
    fakten = " ".join(x for x in (prompts.profil_fakten_text(profile),
                                  f"Angebot: {context['angebot']}." if context.get("angebot") else "") if x)
    antwort = client.chat.completions.create(
        model=OPENAI_MODEL, temperature=0.5, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompts.UEBERARBEITEN_SYSTEM},
                  {"role": "user", "content": prompts.ueberarbeiten_anweisung(
                      elemente, feedback, maximum, True, fakten, profile, headline_max)}])
    roh = json.loads(antwort.choices[0].message.content or "{}")
    neu = {}
    for feld, alt in elemente.items():
        if feld == "karten":
            karten = roh.get("karten") if isinstance(roh.get("karten"), list) else []
            if len(karten) != len(alt) or not all(isinstance(k, dict) for k in karten):
                karten = alt  # Anzahl/Reihenfolge der Karten bleibt gleich – sonst unverändert lassen
            neu["karten"] = [{"headline": str(k.get("headline") or a["headline"]).strip(),
                              "text": str(k.get("text") or a["text"]).strip()} for k, a in zip(karten, alt)]
        else:
            neu[feld] = str(roh.get(feld) or "").strip().strip('"„“') or alt
    if "caption" in neu:
        neu["caption"] = hinweise.trenne_hashtags(neu["caption"])[0] or elemente["caption"]
    return neu


def selbstpruefung(client, post: dict, context: dict, profile: dict) -> dict:
    """Die Content-Engine prüft ihren eigenen Entwurf konstruktiv-kritisch (FEEDBACK_SYSTEM) und überarbeitet
    danach ALLE Textelemente gemeinsam (textelemente_ueberarbeiten) – bevor der Beitrag angezeigt wird. Gleiches
    Vorgehen für Feed, Karussell und Story.
    Das Feedback wird NICHT angezeigt; es bleibt nur zur Dokumentation in
    post["selbstpruefung"]. Bei einem Fehler bleibt der ursprüngliche Entwurf unverändert."""
    if client is None:
        return post
    plattform = context.get("plattform_format")
    elemente = textelemente(post, plattform)
    if not any(elemente.values()):
        return post
    try:
        feedback = hole_feedback(profile, context, {
            "format": format_name(plattform), "unteraspekt": post.get("aspekt", ""), **elemente,
            "angebot": context.get("angebot", ""), "bild": post.get("bild_prompt", "")}, client=client)
        if not feedback.get("verbesserungen"):
            return {**post, "selbstpruefung": feedback}
        spec = spec_fuer(plattform, verhaeltnis_aus(context))
        neu = textelemente_ueberarbeiten(client, elemente, feedback, profile, context,
                                         headline_max=spec.get("ueberschrift_max"))
        return {**textelemente_uebernehmen(post, neu), "selbstpruefung": {**feedback, "vorher": elemente}}
    except Exception:
        return post


def passe_post_an_vorgaben(client, post: dict, spec: dict, context: dict, profile: dict = None) -> dict:
    """Gibt Caption und Headline zurück, die garantiert in die Plattformvorgaben passen.
    Vorher prüft die Content-Engine den Entwurf selbst und arbeitet ihr Feedback ein (selbstpruefung).
    Beitrag (Feed) und Reel: Text und Hashtags getrennt – der Text hält das Limit, die Hashtags
    (genau 5: Unternehmensname, Ort, 3 thematisch) stehen in einer eigenen Zeile."""
    if ist_karussell(context.get("plattform_format")):  # Karten zuerst sicherstellen, dann gemeinsam prüfen
        post = {**post, "slides": karten_sicherstellen(client, post, context, profile or {})}
    post = selbstpruefung(client, post, context, profile or {})  # Feed, Karussell und Story gleich
    if ohne_caption(context.get("plattform_format")):  # Story: keine Caption, keine Hashtags
        ergebnis = {"full_caption": "", "post_text": "", "hashtags": [], "hashtags_string": "", "original_text": "",
                    "headline": text_auf_limit_bringen(client, standard_headline(post, context), None,
                                                       spec.get("ueberschrift_max"), "Überschrift")}
        if post.get("selbstpruefung"):
            ergebnis["selbstpruefung"] = post["selbstpruefung"]
        return profilvorgaben_durchsetzen(client, ergebnis, post, profile, spec)
    regel = hinweise.hashtag_regel_gilt(context.get("plattform_format"))
    if regel:
        roh_text, tags_im_text = hinweise.trenne_hashtags(post.get("post_text") or post.get("full_caption", ""))
        vorschlaege = [t if str(t).startswith("#") else f"#{t}" for t in (post.get("hashtags") or [])] + tags_im_text
        text = text_auf_limit_bringen(client, roh_text, spec.get("primaertext_min"), spec.get("primaertext_max"),
                                      "Primärtext (Caption) ohne Hashtags", None,
                                      headline=standard_headline(post, context))
        text = hinweise.trenne_hashtags(text)[0]  # falls beim Umformulieren Hashtags hineingeraten
        ergebnis = {"full_caption": text, "hashtags_liste": hinweise.hashtags_aufbauen(profile or {}, vorschlaege),
                    "original_text": roh_text}
    else:
        ergebnis = {"full_caption": text_auf_limit_bringen(
            client, post.get("full_caption", ""), spec.get("primaertext_min"), spec.get("primaertext_max"),
            "Primärtext (Caption)", spec.get("hashtags_max"), headline=standard_headline(post, context)),
            "original_text": post.get("full_caption", "")}
    ergebnis["headline"] = text_auf_limit_bringen(
        client, standard_headline(post, context), None, spec.get("ueberschrift_max"), "Überschrift",
    )
    # Keine Wiederholung: wörtlich wiederholte Headline aus der Caption nehmen
    ergebnis["full_caption"] = hinweise.headline_aus_caption_entfernen(ergebnis["full_caption"], ergebnis["headline"])
    if ist_karussell(context.get("plattform_format")):
        ergebnis["slides"] = post.get("slides") or []
    if post.get("selbstpruefung"):
        ergebnis["selbstpruefung"] = post["selbstpruefung"]
        ergebnis["post_text"] = post.get("post_text", "")
    return profilvorgaben_durchsetzen(client, ergebnis, post, profile, spec)


def _anrede_korrigieren(client, text: str, anrede: str) -> str:
    """Nur bei festgestelltem Verstoß: KI schreibt die Ansprache um; danach erneute Regelprüfung."""
    if not text or client is None or not hinweise.anrede_verstoss(text, anrede):
        return text
    try:
        antwort = client.chat.completions.create(
            model=OPENAI_MODEL, temperature=0.2,
            messages=[{"role": "system", "content": prompts.anrede_korrektur_system(anrede)},
                      {"role": "user", "content": text}])
        neu = (antwort.choices[0].message.content or "").strip().strip('"„“')
        return neu or text
    except Exception:
        return text


def profilvorgaben_durchsetzen(client, ergebnis: dict, post: dict, profile: dict, spec: dict) -> dict:
    """Profilvorgaben IMMER exakt einhalten – nach jeder Generierung:
    1. Unternehmensname in exakter Schreibweise (regelbasiert, hinweise.name_korrigieren)
    2. Ansprache Du/Sie: Regelprüfung, bei Verstoß KI-Korrektur nur dieses Feldes (_anrede_korrigieren)
    3. CI „keine Emojis“: Emojis entfernen
    Gilt für Caption, Headline, CTA und alle Karussell-Karten. Thread-sicher (kein st.session_state)."""
    profile = profile or {}
    name, anrede = profile.get("unternehmensname") or "", profile.get("anrede") or "Sie"
    ohne_emojis = hinweise.emojis_verboten(profile)

    def sauber(text: str, maximum=None) -> str:
        if not text:
            return text
        neu = _anrede_korrigieren(client, hinweise.name_korrigieren(text, name), anrede)
        neu = hinweise.name_korrigieren(neu, name)
        if ohne_emojis:
            neu = hinweise.emojis_entfernen(neu)
        if maximum and len(neu) > maximum:  # Korrektur darf das Zeichenlimit nicht sprengen
            neu = text_auf_limit_bringen(client, neu, None, maximum, "Primärtext (Caption)", None)
        return neu

    ergebnis = dict(ergebnis)
    if ergebnis.get("full_caption"):  # nur den Textteil korrigieren, angehängte Hashtags bleiben unverändert
        text, tags = hinweise.trenne_hashtags(ergebnis["full_caption"])
        neu = sauber(text, spec.get("primaertext_max"))
        ergebnis["full_caption"] = hinweise.caption_zusammensetzen(neu, tags) if tags else neu
    if ergebnis.get("post_text"):
        ergebnis["post_text"] = sauber(ergebnis["post_text"])
    if ergebnis.get("headline"):
        ergebnis["headline"] = sauber(ergebnis["headline"])
    if post.get("cta"):
        ergebnis["cta"] = sauber(post["cta"])
    if ergebnis.get("slides"):
        ergebnis["slides"] = [{**k, "headline": sauber(k.get("headline", "")), "text": sauber(k.get("text", ""))}
                              for k in ergebnis["slides"]]
    return ergebnis


def karten_sicherstellen(client, post: dict, context: dict, profile: dict) -> list:
    """Karussell: mindestens 2 Karten. Fehlen sie in der KI-Antwort, werden sie gezielt nachgefragt."""
    karten = [k for k in (post.get("slides") or []) if isinstance(k, dict) and (k.get("headline") or k.get("bild_prompt"))]
    if len(karten) >= KARUSSELL_MIN or client is None:
        return karten[:KARUSSELL_MAX]
    p = prompts.karussell_karten_prompt(post, {**profile, **context,
                                               "plattform_vorgaben": plattform_vorgaben(context.get("plattform_format", "ig_feed"), verhaeltnis_aus(context))})
    try:
        antwort = client.chat.completions.create(
            model=OPENAI_MODEL, temperature=0.6, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": p["system"]}, {"role": "user", "content": p["user"]}])
        roh = json.loads(antwort.choices[0].message.content or "{}")
        neu = [k for k in (roh.get("slides") or []) if isinstance(k, dict)]
        return neu[:KARUSSELL_MAX] if len(neu) >= KARUSSELL_MIN else karten
    except Exception:
        return karten


def generiere_ideen(profile: dict, context: dict, bisherige=None) -> dict:
    """Inhaltsart „Nur Idee“: nur Umsetzungsideen, keine fertigen Beiträge."""
    daten = {**profile, **context, "plattform_vorgaben": plattform_vorgaben(context.get("plattform_format", "ig_feed"), verhaeltnis_aus(context)),
             "bisherige_ideen": [b for b in (bisherige or []) if b]}
    p = prompts.ideen_prompts(daten)
    roh = _ki_json(p["system"], p["user"], temperatur=0.8)
    ideen = [x for x in (roh.get("ideen") or []) if isinstance(x, dict) and x.get("titel")]
    return {"ideen": ideen[:3], "thema": context.get("thema", "")}


def ideen_direkt_erzeugen(ctx: dict):
    """„Nur Idee“: nach der Formatwahl sofort Ideen erzeugen und zur Vorschlagsseite wechseln."""
    profile = db.get_profile(st.session_state.user_id)
    vorherige = st.session_state.get("vorschlaege")
    bisherige = [x.get("titel", "") for x in (vorherige or {}).get("ideen", [])] if isinstance(vorherige, dict) else []
    try:
        erzeuge_vorschlaege_komplett(profile, ctx, bisherige)
        navigiere("vorschlaege")
    except Exception as e:
        st.session_state.chat_history.append(
            {"role": "assistant", "content": f"⚠️ Ideen konnten nicht erzeugt werden: {e}"})


def generiere_vorschlaege_basis(profile: dict, context: dict) -> dict:
    """Variante B des A/B-Tests: EIN Prompt ohne Prompt-Architektur und ohne Unternehmensdaten
    (siehe prompts.basis_text_prompt)."""
    daten = basis_daten(context)
    client = _get_openai_client()
    antwort = client.chat.completions.create(
        model=OPENAI_MODEL, temperature=prompts.BASIS_TEMPERATUR, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompts.basis_text_prompt(daten)}])
    roh = json.loads(antwort.choices[0].message.content or "{}")
    posts = [p for p in (roh.get("posts") or []) if isinstance(p, dict)][:3]
    return {"thema": context.get("thema", ""), "plattform_format": context.get("plattform_format", ""),
            "content_type": context.get("content_type", ""), "ab_variante": "B",
            "posts": [_post_formatieren(json.dumps(p, ensure_ascii=False), "", n + 1) for n, p in enumerate(posts)],
            "anzahl_posts": len(posts), "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def basis_aufbereiten(client, post: dict, spec: dict, context: dict, profile: dict = None) -> dict:
    """Variante B: keine Selbstprüfung, keine Hashtag-Regel, keine Profilkorrektur – nur technische Kürzung auf das
    Plattformlimit und Darstellung der Hashtags, wie die KI sie geliefert hat."""
    tags = [t if str(t).startswith("#") else f"#{t}" for t in (post.get("hashtags") or [])]
    text = hart_kuerzen(post.get("post_text") or "", spec.get("primaertext_max"))
    return {"full_caption": text, "original_text": post.get("post_text") or "", "hashtags_liste": tags,
            "headline": hart_kuerzen(standard_headline(post, context), spec.get("ueberschrift_max"))}


def erzeuge_vorschlaege_komplett(profile: dict, context: dict, bisherige_aspekte=None) -> None:
    """Texte erzeugen (erzeuge_beitraege), dann GLEICHZEITIG: Texte auf die Plattformlimits bringen und
    alle Bilder erzeugen – die Vorschläge erscheinen erst vollständig."""
    if context.get("content_type") == "idee":
        with st.spinner("Sammle Ideen …"):
            setze_vorschlaege(generiere_ideen(profile, context, bisherige_aspekte), context)
        return
    with st.spinner("Erstelle Beitragstexte …"):
        data = generiere_vorschlaege(profile, context, anzahl=3, bisherige_aspekte=bisherige_aspekte)
        st.session_state.pop("eval_fehler", None)
        if st.session_state.get("evaluation") and isinstance(data, dict):
            # Evaluationsmodus: Variante B aus EXAKT denselben Dialogangaben (gleicher Kontext, ein Klick)
            for p in data.get("posts", []):
                p["ab_variante"] = "A"
            try:
                daten_b = generiere_vorschlaege_basis(profile, context)
            except Exception as e:
                daten_b = {"posts": []}
                st.session_state["eval_fehler"] = str(e)
            for p in daten_b.get("posts", []):
                p["ab_variante"] = "B"
            data = {**data, "posts": data.get("posts", []) + daten_b.get("posts", []), "evaluation": True}
    setze_vorschlaege(data, context)
    posts = data.get("posts", []) if isinstance(data, dict) else []
    if not posts:
        return
    spec = spec_fuer(context.get("plattform_format"), verhaeltnis_aus(context))
    client = _get_openai_client() if OPENAI_API_KEY else None
    mit_bildern = bild_erforderlich(context.get("content_type")) and client is not None
    text = f"Passe Texte an die Vorgaben an{' und erstelle ' + str(len(posts)) + ' Bilder' if mit_bildern else ''} …"
    with st.spinner(text):
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(posts))) as pool:
            text_jobs = {pool.submit(basis_aufbereiten if p.get("ab_variante") == "B" else passe_post_an_vorgaben,
                                     client, p, spec, context, profile): i
                         for i, p in enumerate(posts)}
            if mit_bildern and not ist_karussell(context.get("plattform_format")):  # parallel zu den Text-Jobs
                try:
                    generiere_bilder_parallel(profile, context, posts)
                except Exception as e:  # Bildfehler dürfen die Text-Aufbereitung (Hashtags!) nicht abbrechen
                    for n in range(len(posts)):
                        st.session_state[f"ki_fehler_{n}"] = str(e)
            for f in concurrent.futures.as_completed(text_jobs):
                post = posts[text_jobs[f]]
                try:
                    angepasst = f.result()
                except Exception:
                    roh, tags = hinweise.trenne_hashtags(post.get("full_caption", ""))
                    regel = hinweise.hashtag_regel_gilt(context.get("plattform_format"))
                    angepasst = {"full_caption": hart_kuerzen(roh if regel else post.get("full_caption", ""),
                                                              spec["primaertext_max"]),
                                 "original_text": roh if regel else post.get("full_caption", ""),
                                 "headline": hart_kuerzen(standard_headline(post, context), spec["ueberschrift_max"])}
                    if regel:
                        angepasst["hashtags_liste"] = hinweise.hashtags_aufbauen(profile, tags)
                # „Original“ nur zeigen, wenn der TEXT gekürzt/umformuliert wurde (abgetrennte Hashtags zählen nicht)
                if angepasst["full_caption"].strip() != (angepasst.pop("original_text", "") or "").strip():
                    post["original_caption"] = post.get("full_caption", "")
                post.update(angepasst)
                if ohne_caption(context.get("plattform_format")):  # auch im Fehlerfall: Story ohne Caption
                    post.update({"full_caption": "", "post_text": "", "hashtags": [], "hashtags_string": ""})
                    post.pop("original_caption", None)
    # Karussell: Erst wenn alle Karten feststehen (inkl. Absicherung), entstehen die Bilder ALLER Karten –
    # thematisch abgestimmt als Bildserie.
    if mit_bildern and ist_karussell(context.get("plattform_format")):
        anzahl = sum(len(karussell_slides(p, context)) for p in posts)
        with st.spinner(f"Erstelle {anzahl} Bilder für {len(posts)} Karussells – das dauert etwa "
                        f"{max(1, round(anzahl / 12))}–{max(2, round(anzahl / 8))} Minuten …"):
            try:
                karussell_bilder_alle(profile, context, posts, context.get("plattform_format"))
            except Exception as e:
                for n in range(len(posts)):
                    st.session_state[f"ki_fehler_{n}"] = str(e)


def speichere_generiertes_bild(bild_bytes: bytes, user_id: int, endung: str = "jpg") -> str:
    """Speichert ein fertiges Werbebild für den Kalender im Arbeitsverzeichnis (uploads/generated)
    und gibt den Dateipfad zurück."""
    dateiname = f"post_{user_id}_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{endung}"
    os.makedirs(GENERIERTE_BILDER_VERZEICHNIS, exist_ok=True)
    pfad = os.path.join(GENERIERTE_BILDER_VERZEICHNIS, dateiname)
    with open(pfad, "wb") as f:
        f.write(bild_bytes)
    return pfad


def bild_fuer_anzeige(image_url: str):
    """Pfad eines gespeicherten Beitragsbildes oder None, wenn die Datei fehlt. Ältere Einträge mit
    Online-Adresse werden über den Dateinamen auf die lokale Kopie in uploads/generated abgebildet."""
    if not image_url:
        return None
    if os.path.exists(image_url):
        return image_url
    lokal = os.path.join(GENERIERTE_BILDER_VERZEICHNIS, os.path.basename(image_url).split("_", 1)[-1])
    return lokal if image_url.startswith("http") and os.path.exists(lokal) else None


def formatiere_chat_text(text: str) -> str:
    """Escaped Chat-Text für st.html und übernimmt einfache Markdown-Auszeichnung
    (**fett**, Zeilenumbrüche), die GPT gerne verwendet."""
    sicher = html.escape(text or "")
    sicher = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", sicher)
    return sicher.replace("\n", "<br>")


# --- Konstruktiv-kritisches Feedback zu einem Entwurf -------------------------
# Zwei Wege:
#   * KI-Entwürfe: Die Content-Engine prüft sich selbst und arbeitet das Feedback ein (selbstpruefung),
#     ohne es anzuzeigen.
#   * Eigene Texte der Nutzer:innen: Feedback nur auf Wunsch („Feedback zu meinem Text“), konstruktiv-kritisch.

FEEDBACK_ANWEISUNG = prompts.FEEDBACK_SYSTEM


def feld_ids(elemente: dict) -> list:
    """IDs der Textelemente in Anzeigereihenfolge: headline, karte_1 …, cta, caption."""
    ids = ["headline"] if "headline" in elemente else []
    ids += [f"karte_{x + 1}" for x in range(len(elemente.get("karten") or []))]
    return ids + [f for f in ("cta", "caption") if f in elemente]


def feld_wert(elemente: dict, feld: str):
    if feld.startswith("karte_"):
        return elemente["karten"][int(feld.split("_")[1]) - 1]
    return elemente.get(feld)


def hole_feedback(profile: dict, context: dict, entwurf: dict, client=None) -> dict:
    """Gibt {staerken: [{felder, text}], verbesserungen: [{felder, text}], fazit: str} zurück.
    „felder“ ordnet jeden Punkt den Textelementen zu (feld_ids). Thread-sicher (kein st.session_state)."""
    client = client or _get_openai_client()
    gueltig = feld_ids(entwurf)
    spec = spec_fuer(context.get("plattform_format"), verhaeltnis_aus(context))
    daten = {
        "unternehmen": {**{k: profile.get(k, "") for k in ("unternehmensname", "branche", "strategie",
                                                           "positionierung", "zielgruppe",
                                                           "tone_of_voice", "anrede", "ci_besonderheiten")},
                        "fakten": prompts.profil_fakten(profile)},
        "anfrage": {**{k: context.get(k, "") for k in ("thema", "wirkungsebene", "kommunikationsziel", "inhaltlicher_contenttyp")},
                    "rueckfragen": context.get("klaerungen", [])},
        "plattform": f"{spec['plattform']} {spec['art']}",
        # Formatkonfiguration und Overlay-Konzept mitgeben
        "formatkonfiguration": vorgaben_zusammenfassung(spec) + [f"Overlay-Modus: {OVERLAY_MODUS}"],
        "felder": gueltig,
        "entwurf": entwurf,
    }
    antwort = client.chat.completions.create(
        model=OPENAI_MODEL, temperature=0.3, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": FEEDBACK_ANWEISUNG},
                  {"role": "user", "content": json.dumps(daten, ensure_ascii=False)}],
    )
    roh = json.loads(antwort.choices[0].message.content or "{}")

    def liste(x):
        eintraege = []
        for v in (x if isinstance(x, list) else [])[:3]:
            felder = [f for f in (v.get("felder") or []) if f in gueltig] if isinstance(v, dict) else []
            text = str((v.get("text") or "") if isinstance(v, dict) else v).strip()
            if text:
                eintraege.append({"felder": felder, "text": text})
        return eintraege
    return {"staerken": liste(roh.get("staerken")), "verbesserungen": liste(roh.get("verbesserungen")),
            "fazit": str(roh.get("fazit") or "")}


def textelemente_standard(post: dict, plattform: str, ctx: dict, spec: dict, caption: str = None) -> dict:
    """Textelemente so, wie die App sie in den Eingabefeldern vorbelegt (Headlines/Caption auf das Limit gekürzt)."""
    gen = textelemente(post, plattform)
    if "headline" in gen:
        gen["headline"] = hart_kuerzen(standard_headline(post, ctx), spec.get("ueberschrift_max"))
    for karte in gen.get("karten", []):
        karte["headline"] = hart_kuerzen(karte["headline"], spec.get("ueberschrift_max"))
    if "caption" in gen:
        gen["caption"] = hart_kuerzen(gen["caption"] if caption is None else caption, spec.get("primaertext_max"))
    return gen


def textelemente_aus_eingaben(i: int, gen: dict) -> dict:
    """Textelemente mit den aktuellen (ggf. selbst geänderten) Eingaben; gen = textelemente_standard(...)."""
    wert = lambda key, standard: st.session_state.get(key, standard)  # noqa: E731
    if "karten" in gen:
        return {"karten": [{"headline": wert(sk("headline", i, x), k["headline"]),
                            "text": wert(sk("kartentext", i, x), k["text"])} for x, k in enumerate(gen["karten"])],
                "caption": wert(f"caption_{i}", gen["caption"])}
    eingaben = {"headline": wert(f"headline_{i}", gen["headline"])}
    if "cta" in gen:
        eingaben["cta"] = wert(f"cta_{i}", gen["cta"])
    if "caption" in gen:
        eingaben["caption"] = wert(f"caption_{i}", gen["caption"])
    return eingaben


def _text_ueberarbeiten(i: int, standard: dict, ctx: dict, feedback: dict, maximum, minimum, headline_max):
    """Callback „Text mit dem Feedback überarbeiten“ (läuft vor dem Neuzeichnen, darf daher die Textfelder ändern).
    Überarbeitet wie die Selbstprüfung ALLE Textelemente gemeinsam (textelemente_ueberarbeiten)."""
    try:
        client = _get_openai_client()
        profile = db.get_profile(st.session_state.user_id)
        neu = textelemente_ueberarbeiten(client, textelemente_aus_eingaben(i, standard), feedback, profile,
                                         ctx, maximum, headline_max)
        if "caption" in neu:
            st.session_state[f"caption_{i}"] = text_auf_limit_bringen(client, neu["caption"], minimum, maximum,
                                                                      "Primärtext (Caption)", None)
        if "headline" in neu:
            st.session_state[f"headline_{i}"] = hart_kuerzen(neu["headline"], headline_max)
        if "cta" in neu:
            st.session_state[f"cta_{i}"] = neu["cta"]
        for x, karte in enumerate(neu.get("karten", [])):
            st.session_state[sk("headline", i, x)] = hart_kuerzen(karte["headline"], headline_max)
            st.session_state[sk("kartentext", i, x)] = karte["text"]
        st.session_state[f"ueberarbeitet_{i}"] = "Text wurde anhand des Feedbacks überarbeitet."
    except Exception as e:
        st.session_state[f"ueberarbeitet_{i}"] = f"⚠️ Überarbeitung fehlgeschlagen: {e}"


def zeige_ueberarbeiten(i: int, profile: dict, ctx: dict, post: dict, spec: dict, feedback: dict,
                        mit_bild: bool, bild_quelle: str, plattform: str, standard: dict = None):
    """Beitrag überarbeiten (Human-in-the-loop): Bild per Änderungswunsch neu erzeugen; liegt Feedback zum
    eigenen Text vor, kann der Text damit überarbeitet werden."""
    if not feedback and not (mit_bild and bild_quelle == "ki") and not st.session_state.get(f"ueberarbeitet_{i}"):
        return
    with st.container(border=True, key=f"ueberarbeiten_{i}"):
        st.markdown("**Beitrag überarbeiten**")
        meldung = st.session_state.pop(f"ueberarbeitet_{i}", None)
        if meldung:
            (st.warning if meldung.startswith("⚠️") else st.success)(meldung)
        if feedback:
            st.button("✍️ Text mit dem Feedback überarbeiten", key=f"text_ueberarbeiten_{i}", width="stretch",
                      on_click=_text_ueberarbeiten,
                      args=(i, standard or textelemente_standard(post, plattform, ctx, spec), ctx, feedback,
                            spec.get("primaertext_max"), spec.get("primaertext_min"), spec.get("ueberschrift_max")))
        if mit_bild and bild_quelle == "ki":
            wunsch = st.text_input("Was soll am Bild anders sein?", key=f"bildwunsch_{i}",
                                   placeholder="z. B. heller, Rosen in Nahaufnahme, herbstliche Dekoration")
            if ist_karussell(plattform):
                st.caption(f"Gilt für die angezeigte Karte {int(st.session_state.get(f'slide_{i}', 0)) + 1}.")
            if st.button("🖼️ Bild mit Änderungswunsch neu erzeugen", key=f"bild_ueberarbeiten_{i}",
                         width="stretch", disabled=not wunsch.strip()):
                j = int(st.session_state.get(f"slide_{i}", 0)) if ist_karussell(plattform) else 0
                if ist_karussell(plattform):  # Änderungswunsch gilt für die gerade angezeigte Karte
                    karten = karussell_slides(post, ctx)
                    j = min(j, len(karten) - 1)
                    post = karten_post(post, karten, j)
                prompt = baue_bild_prompt(profile, ctx, post, wunsch)
                with st.spinner("Bild wird neu erzeugt …"):
                    try:
                        bild_bytes, modell = generiere_ki_bild(prompt, plattform, verhaeltnis=verhaeltnis_aus(ctx))
                        st.session_state[sk("ki_bild", i, j)] = bild_bytes
                        st.session_state[sk("ki_prompt", i, j)] = prompt
                        st.session_state[sk("ki_modell", i, j)] = {"modell": modell}
                        st.rerun()
                    except Exception as e:
                        st.error(f"Bild konnte nicht erzeugt werden: {e}")


def zeige_feedback_feld(staerken: list, verbesserungen: list, fazit: str = ""):
    """Feedback direkt unter einem Textfeld: Stärken (✓) und Änderungsvorschläge (→)."""
    if not (staerken or verbesserungen or fazit):
        return
    with st.container(border=True):
        st.caption("FEEDBACK ZU DEINEM TEXT")
        zeilen = [f"- ✓ {x}" for x in staerken] + [f"- → {x}" for x in verbesserungen]
        if zeilen:
            st.markdown("\n".join(zeilen))
        if fazit:
            st.markdown(f"*{fazit}*")


def feedback_unter_feldern(i: int, generiert: dict, eingaben: dict, plaetze: dict, profile: dict, ctx: dict,
                           plattform: str, bild_konzept: str):
    """Feedback-Knopf unter dem zuletzt geänderten Textfeld; das Feedback steht unter dem Feld, das es betrifft
    (Punkte zu mehreren Feldern unter jedem davon). Felder im Bereich „Text und Logo auf dem Bild“
    öffnen diesen Bereich automatisch."""
    fb_key = f"feedback_{i}"
    reihenfolge = feld_ids(generiert)
    geaendert = [f for f in reihenfolge if feld_wert(eingaben, f) != feld_wert(generiert, f)]
    if not geaendert:
        st.session_state.pop(fb_key, None)
        return None
    knopf_feld = next((f for f in reversed(geaendert) if f in plaetze), None)
    knopf_platz = plaetze[knopf_feld] if knopf_feld else st.container()

    feedback = st.session_state.get(fb_key)
    if feedback:
        je_feld = {f: ([], []) for f in reihenfolge}
        ohne_platz = ([], [])
        for nr, art in enumerate(("staerken", "verbesserungen")):
            for eintrag in feedback.get(art, []):
                if not isinstance(eintrag, dict):  # ältere Form ohne Feldzuordnung
                    eintrag = {"felder": [], "text": str(eintrag)}
                ziele = [f for f in eintrag["felder"] if f in plaetze]
                for f in ziele:
                    je_feld[f][nr].append(eintrag["text"])
                if not ziele:
                    ohne_platz[nr].append(eintrag["text"])
        for f in reihenfolge:
            if f in plaetze and f != knopf_feld:
                with plaetze[f]:
                    zeige_feedback_feld(*je_feld[f])
        with knopf_platz:
            s, v = je_feld.get(knopf_feld, ([], [])) if knopf_feld else ([], [])
            zeige_feedback_feld(s + ohne_platz[0], v + ohne_platz[1], feedback.get("fazit", ""))

    with knopf_platz:
        if st.button("💬 Feedback zu meinem Text aktualisieren" if feedback else "💬 Feedback zu meinem Text",
                     key=f"feedback_btn_{i}", width="stretch",
                     help="Du hast den Text selbst geändert – die Content-Engine gibt dir ehrliches, "
                          "konstruktiv-kritisches Feedback dazu, direkt unter dem jeweiligen Feld."):
            with st.spinner("Feedback wird erstellt …"):
                try:
                    neu = hole_feedback(profile, ctx, {
                        "format": format_name(plattform), **eingaben,
                        "angebot": ctx.get("angebot", ""), "bild": bild_konzept})
                    st.session_state[fb_key] = neu
                    if any(f != "caption" for art in ("staerken", "verbesserungen")
                           for e in neu[art] for f in e["felder"]):
                        st.session_state[f"textexp_oeffnen_{i}"] = True
                except Exception as e:
                    st.error(f"Feedback konnte nicht erstellt werden: {e}")
                else:
                    st.rerun()
    return feedback


# --- Website: Tonalität & Faktencheck ----------------------------------------------
# Die Website wird nur abgerufen, um die TONALITÄT der Unternehmenstexte grob auszulesen
# (website_text_abrufen -> prompts.WEBSITE_TONALITAET_SYSTEM -> Profil).
# Der Faktencheck vor jeder Freigabe prüft ausschließlich gegen das Profil (Human-in-the-loop).

_HTML_ENTITAETEN = {"&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&lt;": "<", "&gt;": ">",
                    "&auml;": "ä", "&ouml;": "ö", "&uuml;": "ü", "&Auml;": "Ä", "&Ouml;": "Ö", "&Uuml;": "Ü",
                    "&szlig;": "ß", "&euro;": "€", "&ndash;": "–", "&mdash;": "—"}


def _entitaet(treffer) -> str:
    e = treffer.group(0)
    if e in _HTML_ENTITAETEN:
        return _HTML_ENTITAETEN[e]
    if e.startswith("&#"):
        try:
            return chr(int(e[3:-1], 16)) if e[2:3] in ("x", "X") else chr(int(e[2:-1]))
        except (ValueError, OverflowError):
            return " "
    return " "


def html_zu_text(html_text: str) -> str:
    """Technische Umwandlung HTML -> Text (keine KI).
    Footer/Kontakt bleiben erhalten; höchstens 20.000 Zeichen."""
    t = re.sub(r"<(script|style|noscript|svg|iframe|template)[\s\S]*?</\1>", " ", html_text or "", flags=re.I)
    t = re.sub(r"<!--[\s\S]*?-->", " ", t)
    t = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr|/section|/footer|/header)[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&[a-zA-Z#0-9]+;", _entitaet, t)
    zeilen = (re.sub(r"\s+", " ", z).strip() for z in t.split("\n"))
    return "\n".join(z for z in zeilen if z)[:20000]


def website_text_abrufen(url: str) -> str:
    """Ruft die Website ab (Browser-Kennung, deutsche Sprache, max. 5 Weiterleitungen, 20 s Zeitlimit) und
    gibt den reinen Text zurück. Bei Fehlern leerer Text."""
    sitzung = requests.Session()
    sitzung.max_redirects = 5
    try:
        antwort = sitzung.get(url, timeout=20, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ContentEngine-Faktencheck/1.0)",
            "Accept-Language": "de-DE,de;q=0.9"})
        antwort.raise_for_status()
    except requests.exceptions.RequestException:
        return ""
    if "charset" not in (antwort.headers.get("Content-Type") or "").lower():
        try:  # ohne Zeichensatz-Angabe zuerst UTF-8, sonst automatisch erkennen
            antwort.content.decode("utf-8")
            antwort.encoding = "utf-8"
        except UnicodeDecodeError:
            antwort.encoding = antwort.apparent_encoding or "utf-8"
    return html_zu_text(antwort.text)


def tonalitaet_ermitteln(url: str) -> str:
    """1) Die Website (Startseite) wird abgerufen und in reinen Text umgewandelt (website_text_abrufen).
    2) Die KI beschreibt daraus grob die Tonalität (prompts.WEBSITE_TONALITAET_SYSTEM) – KEINE Fakten.
    Speichert nichts; wird nur per Knopf am Website-Feld ausgelöst."""
    url = (url or "").strip()
    if not url:
        raise RuntimeError("Bitte zuerst eine Website eintragen.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    seitentext = website_text_abrufen(url).strip()
    if len(seitentext) < 80:
        raise RuntimeError("Die Website lieferte kaum Text (evtl. wird sie per JavaScript geladen).")

    client = _get_openai_client()
    ergebnis = client.chat.completions.create(
        model=OPENAI_MODEL, temperature=0,
        messages=[{"role": "system", "content": prompts.WEBSITE_TONALITAET_SYSTEM},
                  {"role": "user", "content": f"Website: {url}\n\n{seitentext[:15000]}"}],
    )
    tonalitaet = (ergebnis.choices[0].message.content or "").strip()
    if not tonalitaet:
        raise RuntimeError("Aus der Website konnte keine Tonalität gelesen werden.")
    return tonalitaet


def pruefe_fakten(profile: dict, entwurf: dict) -> list:
    """KI-Faktencheck des Entwurfs. Gibt eine Liste von Befunden zurück
    ({typ, textstelle, hinweis, quelle}); leere Liste = nichts gefunden."""
    client = _get_openai_client()
    daten = {
        "PROFIL": {"Unternehmensname": profile.get("unternehmensname", ""),
                   # Angebot/Leistungen stehen seit dem Profil-Umbau in der Positionierung („Angebot: …“)
                   "Positionierung": profile.get("positionierung", ""), **prompts.profil_fakten(profile)},
        "HEUTE": datetime.date.today().isoformat(),
        "BEITRAG": entwurf,
    }
    antwort = client.chat.completions.create(
        model=OPENAI_MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompts.FAKTENCHECK_SYSTEM},
                  {"role": "user", "content": json.dumps(daten, ensure_ascii=False)}],
    )
    roh = json.loads(antwort.choices[0].message.content or "{}")
    befunde = []
    for b in roh.get("befunde") or []:
        if isinstance(b, dict) and b.get("hinweis"):
            befunde.append({"typ": str(b.get("typ") or "widerspruch"), "textstelle": str(b.get("textstelle") or ""),
                            "hinweis": str(b["hinweis"]), "quelle": str(b.get("quelle") or "")})
    return befunde


FAKTENCHECK_TITEL = {"widerspruch": "Widerspruch", "unbelegt": "Nicht belegt"}


def zeige_faktencheck(befunde: list):
    for b in befunde:
        zitat = f" – „{b['textstelle']}“" if b["textstelle"] else ""
        quelle = f" *(Quelle: {b['quelle']})*" if b["quelle"] and b["quelle"] != "keine" else ""
        text = f"**{FAKTENCHECK_TITEL.get(b['typ'], 'Hinweis')}**{zitat}: {b['hinweis']}{quelle}"
        if b["typ"] == "quelle":
            st.info(text, icon="🌐")
        else:
            st.warning(text, icon="🔎")


def faktencheck_vor_freigabe(user_id: int, entwurf: dict) -> dict:
    """Prüft den Entwurf gegen das Profil (nicht gegen die Website).
    blockierend=True heißt: erst nach bewusster Bestätigung freigeben."""
    profile = db.get_profile(user_id)
    ergebnis = {"befunde": [], "blockierend": False, "fehler": ""}
    try:
        ergebnis["befunde"] = pruefe_fakten(profile, entwurf)
    except Exception as e:
        ergebnis["fehler"] = str(e)
        ergebnis["blockierend"] = True
        return ergebnis
    ergebnis["blockierend"] = any(b["typ"] in ("widerspruch", "unbelegt") for b in ergebnis["befunde"])
    return ergebnis


def zeige_faktencheck_ergebnis(fc: dict):
    with st.container(border=True):
        st.markdown("**🔎 Faktencheck vor der Freigabe**")
        if fc.get("fehler"):
            st.error(f"Der Faktencheck konnte nicht durchgeführt werden: {fc['fehler']}")
        elif fc["befunde"]:
            zeige_faktencheck(fc["befunde"])
        else:
            st.success("Keine Widersprüche zu deinem Profil gefunden.")


# --- Automatische Hinweise (CI, Anrede, Aktualität) ------------------------------

def _hashtag_ergaenzen(widget_key: str, tag: str, maximum):
    """Callback für „Ergänzen“: läuft VOR dem nächsten Rendern, darf daher das Textfeld ändern."""
    if widget_key.startswith("hashtags_"):
        # Hashtag-Zeile (Feed/Reel): an Position 3 einsetzen (nach Name und Ort), höchstens 5 behalten
        tags = [t for t in hinweise.trenne_hashtags(st.session_state.get(widget_key, ""))[1]
                if t.lower() != tag.lower()]
        tags.insert(min(2, len(tags)), tag)
        st.session_state[widget_key] = " ".join(tags[:hinweise.HASHTAG_ANZAHL])
        return
    st.session_state[widget_key] = hinweise.hashtag_anhaengen(
        st.session_state.get(widget_key, ""), tag, maximum, hart_kuerzen)


def _text_entfernen(text_key: str, text: str):
    """Callback für „Entfernen“: wiederholten Bildtext aus der Caption nehmen."""
    st.session_state[text_key] = hinweise.headline_aus_caption_entfernen(st.session_state.get(text_key, ""), text)


def zeige_ci_hinweise(profile: dict, caption: str, headline: str, stichtag, widget_key: str, maximum,
                      hashtag_regel: bool = False, bildtext: str = "", text_key: str = ""):
    """Hinweise auf CI-Abweichungen, Anrede und veraltete Daten – mit „Ergänzen“ für fehlende Hashtags.
    hashtag_regel=True (Feed/Reel): zusätzlich Prüfung „genau 5 Hashtags, 1. Name, 2. Ort“."""
    liste = hinweise.pruefe_beitrag(profile, caption, headline, stichtag)
    if hashtag_regel:
        liste += hinweise.pruefe_hashtag_regel(profile, hinweise.trenne_hashtags(caption)[1])
    if bildtext:  # Text auf dem Bild darf sich in der Caption NICHT wiederholen
        liste += hinweise.pruefe_bildtext_wiederholung(bildtext, hinweise.trenne_hashtags(caption)[0])
    for n, h in enumerate(liste):
        if h.get("entfernen"):
            c_text, c_knopf = st.columns([4, 1], vertical_alignment="center")
            with c_text:
                st.info(h["text"], icon="ℹ️")
            with c_knopf:
                st.button("Entfernen", key=f"entfernen_{widget_key}_{n}", on_click=_text_entfernen,
                          args=(text_key or widget_key, h["entfernen"]), width="stretch")
            continue
        if h["ergaenzen"]:
            c_text, c_knopf = st.columns([4, 1], vertical_alignment="center")
            with c_text:
                st.info(h["text"], icon="ℹ️")
            with c_knopf:
                st.button("Ergänzen", key=f"ergaenzen_{widget_key}_{n}", on_click=_hashtag_ergaenzen,
                          args=(widget_key, h["ergaenzen"], maximum), width="stretch")
        else:
            st.info(h["text"], icon="ℹ️")


def logo_vorhanden(profile: dict) -> bool:
    return bool(profile.get("logo_path") and os.path.exists(profile["logo_path"]))


def zeige_kontext_hinweise(profile: dict):
    """Über Dialog und Vorschlägen: Wunschtermin aus dem Kalender + Hinweise zum Profil."""
    wunsch = st.session_state.get("wunsch_datum")
    if wunsch:
        c_text, c_knopf = st.columns([5, 1], vertical_alignment="center")
        with c_text:
            st.info(f"📅 Geplanter Termin: **{datum_lang(wunsch)}** – bei „Für Kalender einplanen“ schon eingetragen.")
        with c_knopf:
            if st.button("Termin entfernen", key=f"wunsch_weg_{st.session_state.page}", width="stretch"):
                st.session_state.wunsch_datum = None
                st.rerun()
    liste = hinweise.profil_hinweise(profile, logo_vorhanden(profile))
    if liste:
        with st.expander(f"⚑ Hinweise zu deinem Unternehmensprofil ({len(liste)})"):
            st.markdown("\n".join(f"- {h}" for h in liste))
            if st.button("Profil öffnen", key=f"profil_oeffnen_{st.session_state.page}"):
                navigiere("profil")
                st.rerun()


# --- Geführter Dialog: eine Frage pro Schritt ---------------------------------------
# Reihenfolge der Schritte legt naechster_schritt fest. Die KI formuliert nur die Themenfrage und die Rückfragen,
# alles andere ist feste Auswahl per Knopf. Die Zielgruppe wird nicht erfragt – sie kommt aus der Registrierung (Profilfeld „Zielgruppe“).
KI_SCHRITTE = ("thema",)  # nur das Thema formuliert die KI; Wirkung, Ziel und Contenttyp = feste Auswahl
WIRKUNGSEBENEN = prompts.WIRKUNGSEBENEN  # Bruhn (2014); hierarchisch zu den Zielen nach Meffert et al. (2019)
WIRKUNG_FRAGE = {"Du": "Was soll dein Beitrag erreichen?",
                 "Sie": "Was soll Ihr Beitrag erreichen?"}


def _anrede_dialog() -> str:
    """Ansprache im Dialog = Anrede aus dem Profil (wie die Begrüßung)."""
    try:
        return "Du" if db.get_profile(st.session_state.user_id).get("anrede") == "Du" else "Sie"
    except Exception:
        return "Du"


def _wirkung_text(e: dict) -> str:
    return e["text"] if _anrede_dialog() == "Du" else e.get("text_sie", e["text"])


def _wirkung_label(e: dict) -> str:
    return e["label"] if _anrede_dialog() == "Du" else e.get("label_sie", e["label"])

# Inhaltlicher Contenttyp („inhaltlicher_contenttyp“) – NICHT zu verwechseln mit „content_type“ (Ausgabeart
# idee/bild/caption/beides). Angezeigt in Alltagssprache mit Beispielen; an die KI geht die fachliche Bezeichnung
# informativ / interaktional / transaktional (Shahbaznezhad et al., 2021).
CONTENTTYP_OPTIONEN = {
    "informativ": ("Tipps & Infos", "z. B. Wissenswertes, Anleitungen oder Produktdetails"),
    "interaktional": ("Einblicke & Austausch", "z. B. hinter die Kulissen, dein Team oder eine Frage an deine Community"),
    "transaktional": ("Angebot & Aktion", "z. B. ein Rabatt, ein neues Produkt oder ein Termin"),
}
CONTENTTYP_TEXT_SIE = {"interaktional": "z. B. hinter die Kulissen, Ihr Team oder eine Frage an Ihre Community"}
CONTENTTYP_FRAGE = {"Du": "Was für ein Beitrag soll es werden?",
                    "Sie": "Was für ein Beitrag soll es werden?"}

# Fallback, falls die KI nicht erreichbar ist – der Dialog bleibt bedienbar
_FRAGE_FALLBACK = {
    "thema": {"frage": "Worum soll es in dem Beitrag gehen – gibt es einen Anlass?", "vorschlaege": [],
              "empfehlung": None, "begruendung": ""},
}


def _get_openai_client():
    from openai import OpenAI

    if not OPENAI_API_KEY:
        raise RuntimeError("API_CHATGPT ist nicht gesetzt (siehe .env).")
    client = OpenAI(api_key=OPENAI_API_KEY)
    _robuste_chat_aufrufe(client)
    return client


def _robuste_chat_aufrufe(client):
    """Schutz beim Modellwechsel: Lehnt das Modell einen Parameter ab (z. B. „temperature“ oder
    „response_format“), wird der Aufruf einmal ohne diesen Parameter wiederholt, statt abzubrechen."""
    original = client.chat.completions.create

    def create(**kwargs):
        try:
            return original(**kwargs)
        except Exception as e:
            text = str(e).lower()
            entfernt = [k for k in ("temperature", "response_format") if k in kwargs and k in text]
            if not entfernt:
                raise
            return original(**{k: v for k, v in kwargs.items() if k not in entfernt})

    client.chat.completions.create = create


def _ki_json(system: str, user: str, temperatur: float = 0.4) -> dict:
    antwort = _get_openai_client().chat.completions.create(
        model=OPENAI_MODEL, temperature=temperatur, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return json.loads(antwort.choices[0].message.content or "{}")


def naechster_schritt(ctx: dict) -> str:
    """Dialogreihenfolge:
    0. Fertiger Beitrag oder nur Ideen? – „Nur Ideen“: Format wählen, dann direkt Ideen (ohne weitere Fragen).
    Beitrag: Thema → 1. Wirkungsebene (Bruhn, 2014) → Ziel (Meffert et al., 2019; nur die zur Wirkung passenden)
    → Art des Beitrags (Shahbaznezhad et al., 2021) → 2. Zielgruppe (aus dem
    Profil, in der Zusammenfassung sichtbar) → 3. Plattform/Format → Personen im Bild → Klärung offener Punkte durch die KI (White et al., 2023) → Zusammenfassung → 5. Kalender.
    Erzeugt wird erst nach bestätigter Zusammenfassung."""
    if ctx.get("content_type") == "idee":
        if not ctx.get("plattform_format"):
            return "plattform_format"
        return "ideen"  # „Nur Ideen“: keine weiteren Fragen, die Ideen werden direkt erzeugt
    if ctx.get("modus") != "beitrag":
        return "modus"
    if not ctx.get("thema"):
        return "thema"
    # Wirkungsebene (Bruhn) → Ziel (Meffert), hierarchisch
    if ctx.get("kommunikationsziel") and not ctx.get("wirkungsebene"):
        ctx["wirkungsebene"] = prompts.wirkungsebene_zu_ziel(ctx["kommunikationsziel"])
    if ctx.get("wirkungsebene") not in WIRKUNGSEBENEN:
        return "wirkungsebene"
    # Einheitlicher Ablauf: nach der Wirkung folgt bei ALLEN Wirkungen direkt die Art
    # des Beitrags. Das Ziel nach Meffert wird automatisch übernommen – bei „Informieren“ beide kognitiven Ziele.
    ziel = prompts.ziel_zur_wirkung(ctx["wirkungsebene"])
    if ctx.get("kommunikationsziel") not in WIRKUNGSEBENEN[ctx["wirkungsebene"]]["ziele"] + [ziel]:
        ctx["kommunikationsziel"] = ziel
    if ctx.get("inhaltlicher_contenttyp") not in CONTENTTYP_OPTIONEN:
        return "inhaltlicher_contenttyp"
    if not ctx.get("plattform_format"):
        return "plattform_format"
    if ohne_caption(ctx.get("plattform_format")):
        # Story (Instagram und Facebook): ohne Caption – die Frage Bild/Caption entfällt; Inhaltsart „Nur Bild“
        ctx["content_type"] = "bild"
    if not ctx.get("content_type"):
        # Keine Auswahl Bild/Caption mehr: ein Beitrag besteht immer aus Bild und Caption (Story: nur Bild, s. o.)
        ctx["content_type"] = "beides"
    if bild_erforderlich(ctx.get("content_type")) and ctx.get("personen_im_bild") is None:
        return "personen_im_bild"
    if not ctx.get("klaerung_fertig"):
        return "klaerung"
    return "zusammenfassung"


def _wiederholt(vorschlaege: list, bisherige: list) -> bool:
    """True, wenn mindestens ein Vorschlag einer bereits gezeigten Idee (fast) gleicht."""
    import difflib
    norm = lambda t: re.sub(r"[^a-zäöüß0-9 ]", "", str(t).lower()).strip()  # noqa: E731
    alt = [norm(b) for b in bisherige if b]
    return any(difflib.SequenceMatcher(None, norm(v), b).ratio() >= 0.8 for v in vorschlaege for b in alt)


def frage_stellen(schritt: str, neue_ideen: bool = False):
    """Lässt die KI die Frage zum Schritt formulieren und hängt sie EINMAL an den Verlauf an."""
    ctx = st.session_state.chat_context
    fragen = ctx.setdefault("fragen", {})
    if schritt in fragen and not neue_ideen:
        return
    ausschliessen = fragen.get(schritt, {}).get("vorschlaege", []) if neue_ideen else []
    ausschliessen = list(ctx.get("bisherige_ideen", [])) + list(ausschliessen)
    profile = db.get_profile(st.session_state.user_id)
    try:
        p = prompts.dialog_frage_prompts(schritt, profile, ctx, ausschliessen,
                                         ideen_modus=neue_ideen and schritt == "thema")
        roh = _ki_json(p["system"], p["user"], temperatur=0.8 if schritt == "thema" else 0.3)
        vorschlaege = [str(v).strip() for v in (roh.get("vorschlaege") or []) if str(v).strip()][:4]
        # „Andere Ideen“: Kommt eine bereits gezeigte Idee (oder fast dieselbe) wieder, wird bis zu zweimal neu
        # angefragt – mit höherer Temperatur und dem ausdrücklichen Hinweis auf andere Themenbereiche.
        versuch = 0
        while schritt == "thema" and neue_ideen and versuch < 2 and _wiederholt(vorschlaege, ausschliessen):
            versuch += 1
            roh = _ki_json(p["system"] + "\n\nDeine letzten Vorschläge wiederholten bereits gezeigte Ideen. Wähle "
                           "jetzt bewusst ANDERE Themenbereiche (z. B. andere Produkte, Anlässe, Blickwinkel).",
                           p["user"], temperatur=1.0)
            vorschlaege = [str(v).strip() for v in (roh.get("vorschlaege") or []) if str(v).strip()][:4]
        frage = {"frage": str(roh.get("frage") or _FRAGE_FALLBACK[schritt]["frage"]),
                 "vorschlaege": vorschlaege, "empfehlung": None, "begruendung": ""}
    except Exception as e:
        frage = dict(_FRAGE_FALLBACK[schritt])
        frage["frage"] += f" (Hinweis: KI nicht erreichbar – {e})"
    if schritt == "thema":
        ctx["bisherige_ideen"] = list(dict.fromkeys(ausschliessen + frage["vorschlaege"]))
    fragen[schritt] = frage
    st.session_state.chat_history.append({"role": "assistant", "content": frage["frage"]})


def dialog_antwort_waehlen(schritt: str, wert: str):
    """Antwort per Knopf: wird ohne KI-Aufruf übernommen."""
    ctx = st.session_state.chat_context
    ctx[schritt] = wert
    st.session_state.chat_history.append({"role": "user", "content": wert})


def feste_frage_stellen(schritt: str):
    """Wirkung (Bruhn): feste Frage EINMAL in den Verlauf, ohne KI und ohne Empfehlung."""
    fragen = st.session_state.chat_context.setdefault("fragen", {})
    if schritt in fragen:
        return
    text = WIRKUNG_FRAGE[_anrede_dialog()]
    fragen[schritt] = {"frage": text}
    st.session_state.chat_history.append({"role": "assistant", "content": text})


def wirkung_waehlen(ebene: str):
    ctx = st.session_state.chat_context
    ctx["wirkungsebene"] = ebene
    e = WIRKUNGSEBENEN[ebene]
    ctx["kommunikationsziel"] = prompts.ziel_zur_wirkung(ebene)  # Ziel aus der Wirkung abgeleitet
    chip_antwort(f"Wirkung: {_wirkung_label(e)}.")


def contenttyp_frage_stellen():
    """Dialogschritt nach der Wirkung: Frage EINMAL in den Verlauf (feste Frage, keine KI-Kosten)."""
    fragen = st.session_state.chat_context.setdefault("fragen", {})
    if "inhaltlicher_contenttyp" in fragen:
        return
    frage = CONTENTTYP_FRAGE[_anrede_dialog()]
    fragen["inhaltlicher_contenttyp"] = {"frage": frage}
    st.session_state.chat_history.append({"role": "assistant", "content": frage})


def klaerung_fragen():
    """Klärungsschritt (White et al., 2023): Die KI prüft die Angaben und stellt ggf. EINE gezielte Rückfrage.
    Höchstens prompts.KLAERUNG_MAX_FRAGEN Rückfragen; danach – oder wenn alles klar ist – weiter zur Zusammenfassung."""
    ctx = st.session_state.chat_context
    if ctx.get("klaerung_offen") or ctx.get("klaerung_fertig"):
        return
    if len(ctx.get("klaerungen", [])) >= prompts.KLAERUNG_MAX_FRAGEN:
        ctx["klaerung_fertig"] = True
        return
    profile = db.get_profile(st.session_state.user_id)
    try:
        p = prompts.klaerung_prompts(profile, ctx)
        roh = _ki_json(p["system"], p["user"], temperatur=0.2)
    except Exception:  # KI nicht erreichbar -> ohne Rückfrage weiter (Dialog bleibt bedienbar)
        ctx["klaerung_fertig"] = True
        return
    frage = str(roh.get("frage") or "").strip()
    if roh.get("klar") is True or not frage:
        ctx["klaerung_fertig"] = True
        if not ctx.get("klaerungen"):
            return
        st.session_state.chat_history.append(
            {"role": "assistant", "content": "Danke, jetzt ist alles klar. Bitte prüfe die Zusammenfassung."})
        return
    vorschlaege = [str(v).strip() for v in (roh.get("vorschlaege") or []) if str(v).strip()][:3]
    ctx["klaerung_offen"] = {"frage": frage, "vorschlaege": vorschlaege}
    st.session_state.chat_history.append({"role": "assistant", "content": frage})


def klaerung_beantworten(antwort: str, mit_auswertung: bool = True):
    """Speichert die Antwort zur offenen Rückfrage; erkennbare Angaben (z. B. Angebot) werden zusätzlich übernommen."""
    ctx = st.session_state.chat_context
    offen = ctx.pop("klaerung_offen", None) or {"frage": ""}
    st.session_state.chat_history.append({"role": "user", "content": antwort})
    ctx.setdefault("klaerungen", []).append({"frage": offen["frage"], "antwort": antwort})
    if not mit_auswertung:
        return
    try:
        p = prompts.dialog_auswertung_prompts("klaerung", antwort, db.get_profile(st.session_state.user_id), ctx)
        roh = _ki_json(p["system"], p["user"], temperatur=0.2)
        if isinstance(roh.get("angebot"), str) and roh["angebot"].strip():
            ctx["angebot"] = roh["angebot"].strip()
    except Exception:
        pass  # die Antwort ist trotzdem gespeichert und geht an die Generierung


def contenttyp_festlegen(typ: str, quelle: str):
    """Speichert den bestätigten bzw. gewählten Contenttyp im Dialogkontext.
    quelle: immer „manuell“ (keine KI-Empfehlung)."""
    ctx = st.session_state.chat_context
    ctx["inhaltlicher_contenttyp"] = typ
    ctx["contenttyp_quelle"] = quelle
    chip_antwort(f"Art des Beitrags: {CONTENTTYP_OPTIONEN[typ][0]}.")


def dialog_freitext(text: str):
    """Freie Antwort: KI übernimmt alle erkennbaren Angaben (auch zu späteren Schritten)."""
    text = text.strip()
    if not text:
        return
    ctx = st.session_state.chat_context
    schritt = naechster_schritt(ctx)
    if schritt == "klaerung" and ctx.get("klaerung_offen"):
        klaerung_beantworten(text)
        return
    st.session_state.chat_history.append({"role": "user", "content": text})
    profile = db.get_profile(st.session_state.user_id)
    try:
        p = prompts.dialog_auswertung_prompts(schritt, text, profile, ctx)
        roh = _ki_json(p["system"], p["user"], temperatur=0.2)
    except Exception as e:
        st.session_state.chat_history.append(
            {"role": "assistant", "content": f"⚠️ Die Anfrage an die KI ist fehlgeschlagen: {e}"})
        return
    for feld, erlaubt in (("thema", None), ("angebot", None),
                          ("kommunikationsziel", ZIEL_OPTIONEN),
                          ("inhaltlicher_contenttyp", list(CONTENTTYP_OPTIONEN))):
        wert = roh.get(feld)
        if isinstance(wert, str) and wert.strip() and (erlaubt is None or wert in erlaubt):
            ctx[feld] = wert.strip()
            if feld == "inhaltlicher_contenttyp":
                ctx["contenttyp_quelle"] = "manuell"
    if ctx.get("kommunikationsziel") in ZIEL_OPTIONEN:
        ctx["wirkungsebene"] = prompts.wirkungsebene_zu_ziel(ctx["kommunikationsziel"])
    if schritt == "modus":  # freie Antwort auf „Was möchtest du erstellen?“
        if roh.get("ideen_gewuenscht") and not ctx.get("thema"):
            ctx["content_type"] = "idee"
        elif any(roh.get(f) for f in ("thema", "angebot", "kommunikationsziel", "inhaltlicher_contenttyp")):
            ctx["modus"] = "beitrag"
    ideen = bool(roh.get("ideen_gewuenscht")) and naechster_schritt(ctx) == "thema"
    # Wünscht sich die Person Ideen (oder weiß das Thema noch nicht), kommt KEINE Rückfrage, sondern direkt
    # konkrete Ideen der KI.
    if roh.get("rueckmeldung") and not ideen:
        st.session_state.chat_history.append({"role": "assistant", "content": str(roh["rueckmeldung"])})
    if ideen:
        frage_stellen("thema", neue_ideen=True)


def chip_antwort(user_text: str, assistant_text: str = ""):
    """Für reine Knopf-Auswahlen (Inhaltsart, Format, Personen): keine KI-Kosten."""
    st.session_state.chat_history.append({"role": "user", "content": user_text})
    if assistant_text:
        st.session_state.chat_history.append({"role": "assistant", "content": assistant_text})


def angabe_aendern(schritt: str):
    """Aus der Zusammenfassung: Angabe zurücksetzen – die Frage wird erneut gestellt."""
    ctx = st.session_state.chat_context
    ctx.pop(schritt, None)
    ctx.get("fragen", {}).pop(schritt, None)
    if schritt == "wirkungsebene":  # neue Wirkung = Ziel neu wählen
        ctx.pop("kommunikationsziel", None)
        ctx.get("fragen", {}).pop("kommunikationsziel", None)
    if schritt == "kommunikationsziel":  # Ziel ändern = bei der Wirkung beginnen
        ctx.pop("wirkungsebene", None)
        ctx.get("fragen", {}).pop("wirkungsebene", None)
    if schritt == "plattform_format":  # Story ↔ Feed: Inhalt (Bild bzw. Bild + Caption) neu ableiten
        ctx.pop("content_type", None)
    if schritt == "inhaltlicher_contenttyp":
        ctx.pop("contenttyp_quelle", None)


def pruefe_und_generiere(erzwingen: bool = False):
    """Löst die Vorschlags-Generierung aus, sobald alle nötigen Angaben vorliegen:
    Dialog (Thema, Wirkung, Art des Beitrags), content_type, plattform_format
    und – falls ein Bild gebraucht wird – personen_im_bild.
    erzwingen=True: manueller Start per Button, auch wenn das Modell
    'bereit_zur_generierung' nicht gesetzt hat."""
    ctx = st.session_state.chat_context
    # Erzeugt wird nur nach Bestätigung der Zusammenfassung (erzwingen=True) und mit allen Angaben
    if not erzwingen or naechster_schritt(ctx) != "zusammenfassung":
        return False

    profile = db.get_profile(st.session_state.user_id)
    bisherige_aspekte = []
    vorherige = st.session_state.get("vorschlaege")
    if vorherige and isinstance(vorherige, dict):
        bisherige_aspekte = ([p.get("aspekt", "") for p in vorherige.get("posts", []) if p.get("aspekt")]
                             + [x.get("titel", "") for x in vorherige.get("ideen", []) if x.get("titel")])

    try:
        erzeuge_vorschlaege_komplett(profile, ctx, bisherige_aspekte)
        navigiere("vorschlaege")
    except Exception as e:  # alle Fehler abfangen (auch JSON-Fehler), damit die App bedienbar bleibt
        st.session_state.chat_history.append(
            {"role": "assistant", "content": f"⚠️ Generierung fehlgeschlagen: {e}"}
        )
        return False
    return True


# --- Login / Registrierung -------------------------------------------------

# --- Unternehmensprofil: gemeinsame Formularfelder ------------------------------
# Werden bei der Registrierung, im Onboarding (ältere Konten) und auf der Profilseite
# verwendet – so bleibt das Profil an allen Stellen identisch.

# Leitfragen für Positionierung und Zielgruppe: (Schlüssel, Etikett im gespeicherten Text).
# Gespeichert wird ein Text wie „Wer: …; Warum: …“ im bestehenden Profilfeld – mit Etiketten,
# damit das Sprachmodell die Angaben einordnen und die App die Felder wieder befüllen kann.
POSITIONIERUNG_FELDER = [("angebot", "Angebot"), ("fuer_wen", "Für wen"), ("besonderheit", "Besonderheit")]
ZIELGRUPPE_FELDER = [("alter", "Altersgruppen"), ("wer", "Wer"), ("warum", "Warum"),
                     ("nutzung", "Nutzung"), ("zeitpunkt", "Zeitpunkt")]
ALTER_KEINE = "Keine bestimmte Altersgruppe"
ALTERSGRUPPEN = ["13–17", "18–29", "30–49", "50–64", "65+", ALTER_KEINE]
ALTER_ALT = {"13–17-Jährige": "13–17"}  # frühere Bezeichnungen in bereits gespeicherten Profilen


def leitfragen_text(teile: dict, felder: list) -> str:
    """Setzt die Antworten zu „Etikett: Wert; …“ zusammen (leere Felder entfallen, „;“ → „,“,
    Zeilenumbrüche → Leerzeichen)."""
    return "; ".join(f"{etikett}: {' '.join(teile[k].replace(';', ',').split())}"
                     for k, etikett in felder if (teile.get(k) or "").strip())


def leitfragen_aufteilen(text: str, felder: list, freitext_feld: str) -> dict:
    """Gegenstück zu leitfragen_text(). Alter Freitext ohne Etiketten landet im Feld freitext_feld."""
    nach_etikett = {etikett: k for k, etikett in felder}
    teile = {}
    for stueck in (text or "").split(";"):
        etikett, _, wert = stueck.partition(":")
        if etikett.strip() in nach_etikett and wert.strip():
            teile[nach_etikett[etikett.strip()]] = wert.strip()
    if not teile and (text or "").strip():
        teile[freitext_feld] = text.strip()
    return teile


def profil_felder(profile: dict, logo_label: str = "Logo (optional)", mit_konten: bool = False,
                  mit_logo_feld: bool = True) -> dict:
    """Zeichnet alle Profilfelder (innerhalb eines st.form des Aufrufers) und gibt die Werte zurück."""
    w = {}
    st.markdown("##### Unternehmen")
    if mit_logo_feld:  # Logo direkt unter „Unternehmen“: anzeigen und hinzufügen bzw. ändern
        hat_logo = bool(profile.get("logo_path") and os.path.exists(profile["logo_path"]))
        # dezente Darstellung über style.css (.st-key-logo_feld_…); Knopftext je nachdem, ob schon ein Logo da ist
        with st.container(key="logo_feld_aendern" if hat_logo else "logo_feld_neu"):
            l1, l2 = st.columns([1, 5], vertical_alignment="center")
            with l1:
                if hat_logo:
                    st.image(profile["logo_path"], width=96)
                else:
                    st.caption("Noch kein Logo")
            with l2:
                w["logo_upload"] = st.file_uploader("Logo ändern" if hat_logo else logo_label,
                                                    type=["png", "jpg", "jpeg", "svg"],
                                                    label_visibility="collapsed")
    w["unternehmensname"] = st.text_input("Unternehmensname *", profile.get("unternehmensname", ""),
                                          placeholder="z. B. Bäckerei Freiwald")
    w["branche"] = st.text_input("Branche", profile.get("branche", ""), placeholder="z. B. Bäckerei, Fitnessstudio")
    w["strategie"] = st.text_area(
        "Social-Media-Strategie (Ziele)", profile.get("strategie", ""),
        placeholder="z. B. mehr Laufkundschaft am Vormittag, Stammkunden binden, Auszubildende gewinnen",
        help="Was soll Social Media für das Unternehmen erreichen?",
    )
    # Positionierung über drei Leitfragen (Was tun wir? Für wen? Wie?); gespeichert als ein Text
    # im bisherigen Feld „positionierung“, Teile mit „;“ getrennt – die Prompts bleiben unverändert.
    st.markdown("##### Positionierung")
    st.caption("Wofür steht ihr – im Unterschied zur Konkurrenz?")
    pos = leitfragen_aufteilen(profile.get("positionierung", ""), POSITIONIERUNG_FELDER, "besonderheit")
    # Größere Felder, damit die Angaben ausführlicher werden (ersetzt auch „Produkte & Leistungen“)
    p1, p2, p3 = st.columns(3, vertical_alignment="bottom")
    with p1:
        pos["angebot"] = st.text_area("Was bietet ihr an? *", pos.get("angebot", ""), height=140,
                                      placeholder="z. B. Brot und Brötchen aus eigener Backstube, "
                                                  "Hochzeitstorten auf Bestellung, Frühstück im Café")
    with p2:
        pos["fuer_wen"] = st.text_area("Für wen ist euer Angebot?", pos.get("fuer_wen", ""), height=140,
                                       placeholder="z. B. Genießer:innen und Familien aus der Region, "
                                                   "die Wert auf frische Backwaren legen")
    with p3:
        pos["besonderheit"] = st.text_area("Was macht euch besonders?", pos.get("besonderheit", ""), height=140,
                                           placeholder="z. B. traditionelles Handwerk mit regionalen Zutaten – "
                                                       "im Unterschied zu Backshop-Ketten")
    w["positionierung"] = leitfragen_text(pos, POSITIONIERUNG_FELDER)
    w["_positionierung_teile"] = pos

    st.markdown("##### Anschrift & Kontakt")
    w["strasse"] = st.text_input("Straße und Hausnummer", profile.get("strasse", ""), placeholder="z. B. Hauptstraße 12")
    a1, a2 = st.columns([1, 3])
    with a1:
        w["plz"] = st.text_input("PLZ", profile.get("plz", ""), max_chars=5, placeholder="70794")
    with a2:
        # Bestehende Konten: der frühere „Standort“ wird als Ort vorbelegt
        w["ort"] = st.text_input("Ort", profile.get("ort") or profile.get("standort", ""), placeholder="z. B. Filderstadt")
    k1, k2 = st.columns(2)
    with k1:
        w["telefon"] = st.text_input("Telefon", profile.get("telefon", ""), placeholder="z. B. 0711 123456")
    with k2:
        w["kontakt_email"] = st.text_input("E-Mail für Kund:innen", profile.get("kontakt_email", ""),
                                           placeholder="z. B. info@baeckerei-muster.de")

    st.markdown("##### Öffnungszeiten")
    st.caption("Je Tag z. B. „6–18“, „6:00–12:30, 14:00–18:00“ oder „geschlossen“. Leer = keine Angabe. "
               "Die Angaben werden vereinheitlicht und im Faktencheck verwendet.")
    try:
        zeiten = json.loads(profile.get("oeffnungszeiten") or "{}")
    except ValueError:
        zeiten = {}
    w["oeffnungszeiten"] = {}
    z1, z2 = st.columns(2)
    for n, tag in enumerate(hinweise.WOCHENTAGE):
        with (z1 if n < 4 else z2):
            w["oeffnungszeiten"][tag] = st.text_input(hinweise.WOCHENTAGE_LANG[tag], zeiten.get(tag, ""),
                                                      key=f"oeffnung_{tag}")

    st.markdown("##### Unternehmenswissen")
    st.caption("Diese Angaben nutzt die Content-Engine für Texte, Ideen und Bilder. "
               "Je konkreter, desto passender die Beiträge.")
    w["stilbeispiel"] = st.text_area(
        "Stilbeispiel (ein typischer Beitrag von euch)", profile.get("stilbeispiel", ""),
        placeholder="Einen bisherigen Instagram- oder Facebook-Text einfügen, der eure Sprache gut trifft.",
    )
    w["website"] = st.text_input(
        "Website (optional)", profile.get("website", ""), placeholder="z. B. www.baeckerei-muster.de",
        help="Die Content-Engine liest nur grob die Tonalität eurer Website-Texte aus (Anrede, Wortwahl, Satzbau), "
             "damit die Beiträge ähnlich klingen. Fakten wie Öffnungszeiten oder Anschrift kommen ausschließlich "
             "aus deinem Profil.",
    )
    website_tonalitaet_feld(profile, w["website"])

    # Zielgruppe über Leitfragen (Wer? Was? Warum? Wie?); gespeichert als ein Text im bisherigen
    # Feld „zielgruppe“, Teile mit „;“ getrennt – die Prompts bleiben unverändert.
    st.markdown("##### Zielgruppe & Ansprache")
    st.caption("Vier kurze Fragen – ein Stichpunkt pro Feld reicht.")
    zg = leitfragen_aufteilen(profile.get("zielgruppe", ""), ZIELGRUPPE_FELDER, "wer")
    alter_vorher = [ALTER_ALT.get(a.strip(), a.strip()) for a in zg.get("alter", "").split(",")]
    alter_vorher = [a for a in alter_vorher if a in ALTERSGRUPPEN]
    alter = st.pills("Welche Altersgruppen sollen angesprochen werden? * (mehrere möglich)", ALTERSGRUPPEN,
                     selection_mode="multi", default=alter_vorher)
    zg["alter"] = ", ".join(alter or [])
    # Je Zeile eigene Spalten, unten ausgerichtet – so stehen die Felder auch bei umbrechenden Labels auf einer Linie
    z1, z2 = st.columns(2, vertical_alignment="bottom")
    with z1:
        zg["wer"] = st.text_input("Wer sind die Personen, die angesprochen werden sollen? *", zg.get("wer", ""),
                                  placeholder="z. B. Berufstätige und Familien aus der Region")
    with z2:
        zg["warum"] = st.text_input("Warum kommen diese Personen zu euch? *", zg.get("warum", ""),
                                    placeholder="z. B. Qualität, persönliche Beratung, Nähe")
    z3, z4 = st.columns(2, vertical_alignment="bottom")
    with z3:
        zg["nutzung"] = st.text_input("Wie nutzen diese Personen euer Angebot?", zg.get("nutzung", ""),
                                      placeholder="z. B. regelmäßig, mit Termin, mit der Familie")
    with z4:
        zg["zeitpunkt"] = st.text_input("Wann sehen diese Personen eure Beiträge?", zg.get("zeitpunkt", ""),
                                        placeholder="z. B. abends am Handy")
    w["zielgruppe"] = leitfragen_text(zg, ZIELGRUPPE_FELDER)
    w["_zielgruppe_teile"] = {**zg, "alter_liste": list(alter or [])}
    w["anrede"] = st.radio(
        "Ansprache der Zielgruppe", ANREDE_OPTIONEN, horizontal=True,
        index=ANREDE_OPTIONEN.index(profile.get("anrede", "Sie")) if profile.get("anrede") in ANREDE_OPTIONEN else 0,
    )
    w["tone_of_voice"] = st.selectbox(
        "Tone of Voice", TONE_OPTIONEN,
        index=TONE_OPTIONEN.index(profile.get("tone_of_voice", "Professionell"))
        if profile.get("tone_of_voice") in TONE_OPTIONEN else 0,
    )

    st.markdown("##### CI-Guide")
    c1, c2 = st.columns(2)
    with c1:
        w["ci_farbe"] = st.color_picker("Primärfarbe (Headline & Button)", profile.get("ci_farbe") or "#000000")
    with c2:
        w["ci_farbe_sekundaer"] = st.color_picker("Sekundärfarbe (Button-Text & Akzent)",
                                                  profile.get("ci_farbe_sekundaer") or "#FFFFFF")
    w["ci_schrift"] = st.text_input("Schriftart (z. B. Montserrat, Arial)", profile.get("ci_schrift", ""),
                                    help="Google-Fonts-Schriften werden automatisch geladen. Für Hausschriften "
                                         "bitte zusätzlich die Schriftdatei hochladen.")
    w["schrift_upload"] = st.file_uploader("Schriftdatei (optional, .ttf/.otf – am besten der fette Schnitt)",
                                           type=["ttf", "otf"])
    w["ci_besonderheiten"] = st.text_area(
        "Sonstige CI-Besonderheiten", profile.get("ci_besonderheiten", ""),
        placeholder="z. B. natürliche Bildgestaltung, warme Farbtöne, harmonisches Zusammenspiel – "
                    "oder: immer mit Hashtag #, nie Emojis …",
        help="Wird auch für die Bilderstellung verwendet (fließt in die Bildbeschreibung ein).",
    )
    st.caption("Hier könnt ihr auch Vorgaben angeben, die beispielsweise zur Bilderstellung verwendet werden "
               "sollen, z. B. natürliche Bildgestaltung, warme Farbtöne, harmonisches Zusammenspiel etc.")

    if mit_konten:
        st.markdown("##### Konten für die Veröffentlichung")
        st.caption("Optional, für eine spätere echte Anbindung an Instagram und Facebook. Im Prototyp wird "
                   "die Veröffentlichung simuliert.")
        k1, k2 = st.columns(2)
        with k1:
            w["instagram_account_id"] = st.text_input("Instagram Business Account ID",
                                                      profile.get("instagram_account_id", ""))
        with k2:
            w["facebook_page_id"] = st.text_input("Facebook-Seiten-ID", profile.get("facebook_page_id", ""))
    return w


def profil_fehler(w: dict) -> list:
    """Prüft die Eingaben aus profil_felder(); vereinheitlicht dabei die Öffnungszeiten (ändert w)."""
    fehler = []
    if not w["unternehmensname"].strip():
        fehler.append("Bitte den Unternehmensnamen angeben.")
    plz = w.get("plz", "").strip()
    if plz and not (plz.isdigit() and len(plz) == 5):
        fehler.append("Die PLZ muss aus 5 Ziffern bestehen.")
    mail = w.get("kontakt_email", "").strip()
    if mail and ("@" not in mail or "." not in mail.split("@")[-1]):
        fehler.append("Die Kontakt-E-Mail ist ungültig.")
    pos = w.get("_positionierung_teile", {})
    if not pos.get("angebot", "").strip():
        fehler.append("Positionierung: Bitte angeben, was ihr anbietet.")
    zg = w.get("_zielgruppe_teile", {})
    alter = zg.get("alter_liste", [])
    if not alter:
        fehler.append("Zielgruppe: Bitte mindestens eine Altersgruppe auswählen.")
    elif ALTER_KEINE in alter and len(alter) > 1:
        fehler.append(f"Zielgruppe: Bitte entweder bestimmte Altersgruppen oder „{ALTER_KEINE}“ auswählen.")
    if not zg.get("wer", "").strip():
        fehler.append("Zielgruppe: Bitte beschreiben, wer angesprochen werden soll.")
    if not zg.get("warum", "").strip():
        fehler.append("Zielgruppe: Bitte angeben, warum diese Personen zu euch kommen.")
    for tag, text in w.get("oeffnungszeiten", {}).items():
        ok, normiert = hinweise.normalisiere_oeffnungszeit(text)
        if ok:
            w["oeffnungszeiten"][tag] = normiert
        else:
            fehler.append(f"Öffnungszeit {hinweise.WOCHENTAGE_LANG[tag]} „{text}“ nicht lesbar – "
                          "bitte z. B. „6–18“, „6:00–12:30, 14:00–18:00“ oder „geschlossen“.")
    return fehler


def profil_speichern(user_id: int, altes_profil: dict, w: dict, **extra):
    """Speichert die Werte aus profil_felder() inkl. Logo/Schrift und liest ggf. die Website aus."""
    logo_path = altes_profil.get("logo_path", "")
    if w.get("logo_upload") is not None:
        logo_path = save_logo(w["logo_upload"], user_id)
    schrift_datei = altes_profil.get("ci_schrift_datei", "")
    if w.get("schrift_upload") is not None:
        schrift_datei = save_schrift(w["schrift_upload"], user_id)
    werte = {k: (v.strip() if isinstance(v, str) else v) for k, v in w.items()
             if k not in ("logo_upload", "schrift_upload", "oeffnungszeiten")}
    werte["oeffnungszeiten"] = json.dumps(w.get("oeffnungszeiten", {}), ensure_ascii=False)
    werte["standort"] = werte.get("ort", "")  # „Standort“ wird aus dem Ort übernommen (Altfeld)
    db.save_profile(user_id, logo_path=logo_path, ci_schrift_datei=schrift_datei,
                    profil_aktualisiert=datetime.datetime.now().isoformat(timespec="seconds"),
                    **werte, **extra)
    website_nach_speichern(user_id, altes_profil, w.get("website", ""))


# Hinweis: st.html entfernt SVG-Grafiken – Logo-Zeichen und Häkchen sind deshalb reine CSS-Formen.
LOGO_HTML = """
<div class="ce-login-logo"><div class="ce-plakette"></div><span>ContentEngine</span></div>
"""

MARKENFLAECHE_HTML = f"""
<div class="ce-login-brand">
  {LOGO_HTML}
  <div>
    <p class="ce-login-eyebrow">KI-Content-Engine für kleine und mittlere Unternehmen</p>
    <h1>Social-Media-Content, der zu deinem Unternehmen passt.</h1>
    <p class="ce-login-lead">Im Dialog Ideen entwickeln, Bild und Text erstellen lassen und auf Instagram
      und Facebook veröffentlichen – mit deinem Unternehmensprofil als Grundlage.</p>
    <div class="ce-login-features">
      <div class="ce-login-feature"><div class="ce-icon"></div>
        <div><b>Ideen im Dialog</b><small>Die Content-Engine fragt nach, statt ein leeres Formular vorzulegen.</small></div></div>
      <div class="ce-login-feature"><div class="ce-icon"></div>
        <div><b>Eigenes Foto oder KI-Bild</b><small>Werbebild mit Headline, Logo und deinen CI-Farben.</small></div></div>
      <div class="ce-login-feature"><div class="ce-icon"></div>
        <div><b>Prüfen, freigeben, planen</b><small>Hinweise und Faktencheck vor jeder Freigabe, Übersicht im Kalender.</small></div></div>
    </div>
  </div>
  <div class="ce-login-fuss">© 2026 ContentEngine · Prototyp im Rahmen einer Bachelorarbeit</div>
</div>
"""


def _login_modus() -> str:
    """Umschalter Anmelden/Registrieren. Ein erneuter Klick auf den aktiven Eintrag hebt die Auswahl
    auf (Wert None) – dann gilt der zuletzt gewählte Modus weiter."""
    wahl = st.segmented_control("Modus", ["Anmelden", "Registrieren"], key="login_modus",
                                default="Anmelden", label_visibility="collapsed", width="stretch")
    if wahl:
        st.session_state.login_modus_letzt = wahl
    return wahl or st.session_state.get("login_modus_letzt", "Anmelden")


def render_login():
    modus = st.session_state.get("login_modus") or st.session_state.get("login_modus_letzt", "Anmelden")
    with st.container(key="login_seite"):
        if modus == "Registrieren":
            # Langes Formular: zentrierte, breite Karte statt geteilter Ansicht
            st.html(f'<div class="ce-reg-kopf">{LOGO_HTML}<small>Prototyp im Rahmen einer Bachelorarbeit</small></div>')
            with st.container(key="login_card_breit"):
                if _login_modus() == "Registrieren":
                    render_registrieren()
                else:
                    render_anmelden()
        else:
            col_brand, col_form = st.columns([1.15, 1], gap="large", vertical_alignment="center")
            with col_brand:
                st.html(MARKENFLAECHE_HTML)
            with col_form:
                with st.container(key="login_card"):
                    if _login_modus() == "Registrieren":
                        render_registrieren()
                    else:
                        render_anmelden()


def render_anmelden():
    st.markdown("#### Willkommen zurück")
    st.caption("Melde dich mit deinen Zugangsdaten an.")
    email = st.text_input("E-Mail", placeholder="name@unternehmen.de")
    passwort = st.text_input("Passwort", type="password", placeholder="••••••••")
    if st.button("Anmelden", type="primary", width='stretch'):
        if not email or not passwort:
            st.error("Bitte E-Mail und Passwort angeben.")
            return
        user_id = db.verify_login(email, passwort)
        if user_id is None:
            st.error("E-Mail oder Passwort ist falsch.")
            return
        st.session_state.user_id = user_id
        # Ältere Konten ohne vollständiges Profil holen das Onboarding nach
        if not db.get_profile(user_id).get("onboarding_done"):
            st.session_state.page = "onboarding"
        st.rerun()


def render_registrieren():
    """Registrierung UND Unternehmensprofil in einem Schritt (ein Formular, ein Knopf)."""
    st.markdown("#### Konto erstellen")
    st.caption("Lege dein Konto und dein Unternehmensprofil an. Die Angaben werden bei jeder "
               "Content-Generierung automatisch berücksichtigt und lassen sich später unter „Profil“ ändern. "
               "Pflichtfelder sind mit * markiert.")
    with st.form("registrierung"):
        st.markdown("##### Zugangsdaten")
        email = st.text_input("E-Mail *", placeholder="name@unternehmen.de")
        p1, p2 = st.columns(2)
        with p1:
            passwort = st.text_input("Passwort *", type="password", placeholder="mind. 8 Zeichen")
        with p2:
            passwort2 = st.text_input("Passwort wiederholen *", type="password")
        werte = profil_felder({})
        if st.form_submit_button("Konto erstellen & loslegen", type="primary", width="stretch"):
            fehler = profil_fehler(werte)
            if not email.strip() or "@" not in email:
                fehler.append("Bitte eine gültige E-Mail-Adresse angeben.")
            elif db.email_exists(email):
                fehler.append("Für diese E-Mail existiert bereits ein Konto.")
            if len(passwort) < 8:
                fehler.append("Das Passwort muss mindestens 8 Zeichen haben.")
            elif passwort != passwort2:
                fehler.append("Die Passwörter stimmen nicht überein.")
            if fehler:
                for f in fehler:
                    st.error(f)
                return
            user_id = db.create_user(email, passwort)
            profil_speichern(user_id, {}, werte, onboarding_done=1)
            st.session_state.user_id = user_id
            navigiere("chat")
            st.rerun()


# --- Onboarding: für ältere Konten ohne vollständiges Profil ----------------------

def render_onboarding_page():
    st.html('<div style="height:32px"></div>')
    _, mid, _ = st.columns([1, 3, 1])
    with mid:
        st.markdown("### Willkommen! Lass uns dein Unternehmensprofil vervollständigen")
        st.caption(
            "Diese Angaben werden einmalig erfasst und danach bei jeder Content-Generierung "
            "automatisch berücksichtigt – du musst sie im Dialog nicht erneut angeben. Du kannst "
            "sie später jederzeit unter „Profil“ anpassen."
        )
        profile = db.get_profile(st.session_state.user_id)
        with st.form("onboarding_formular"):
            werte = profil_felder(profile)
            if st.form_submit_button("Profil speichern & los geht's", type="primary", width='stretch'):
                fehler = profil_fehler(werte)
                if fehler:
                    for f in fehler:
                        st.error(f)
                    return
                profil_speichern(st.session_state.user_id, profile, werte, onboarding_done=1)
                navigiere("chat")
                st.rerun()


# --- Sidebar / Navigation ---------------------------------------------------

def render_sidebar():
    profile = db.get_profile(st.session_state.user_id)
    name = profile.get("unternehmensname") or "Unternehmensprofil"

    with st.sidebar:
        st.html(
            """
            <div class="ce-logo">
              <div class="ce-logo-badge"></div>
              <span>ContentEngine</span>
            </div>
            """
        )

        for key, label in NAV_ITEMS:
            aktiv = st.session_state.page == key
            if st.button(label, key=f"nav_{key}", type="primary" if aktiv else "secondary",
                         width='stretch'):
                navigiere(key)
                st.rerun()

        st.html(
            f"""
            <div class="ce-avatar-row">
              <div class="ce-avatar-circle">{initialen(name)}</div>
              <div>
                <div class="ce-avatar-name">{name}</div>
                <div class="ce-avatar-sub">Profil &amp; Einstellungen</div>
              </div>
            </div>
            """
        )
        if st.session_state.get("evaluation"):  # nur über ?evaluation=1 erreichbar
            st.caption("Evaluationsmodus aktiv")
        with st.container(key="sidebar_logout"):
            st.button("Abmelden", key="nav_logout", width='content', on_click=abmelden)


def abmelden():
    st.session_state.user_id = None
    st.session_state.chat_history = []
    st.session_state.chat_context = {}
    st.session_state.vorschlaege = None
    st.session_state.page = "chat"


def navigiere(ziel: str):
    st.session_state.page = ziel


def _mobil_navigieren(ziel: str):
    navigiere(ziel)
    st.session_state["mobilnav_menue"] = False  # Menü nach der Auswahl schließen


def mobil_menue():
    """Navigation auf dem Handy: Burgermenü oben rechts (am Desktop ausgeblendet, dort gilt die Sidebar)."""
    with st.popover(":material/menu:", key="mobilnav_menue", on_change="rerun", help="Menü"):
        for key, label in NAV_ITEMS:
            st.button(label, key=f"mnav_{key}", width="stretch", on_click=_mobil_navigieren, args=(key,),
                      type="primary" if st.session_state.page == key else "secondary")
        if st.session_state.get("evaluation"):
            st.caption("Evaluationsmodus aktiv")
        st.button("Abmelden", key="mnav_logout", width="stretch", type="tertiary", on_click=abmelden)


def topbar(title: str, subtitle: str = ""):
    with st.container(key="topbar"):
        c_titel, c_menue = st.columns([9, 1], vertical_alignment="center")
        with c_titel:
            st.html(f"<h1>{title}</h1>")
            if subtitle:
                st.html(f"<p>{subtitle}</p>")
        with c_menue:
            with st.container(key="mobilnav"):
                mobil_menue()


# --- Seite: Chat-Dialog ------------------------------------------------------

def zeige_zusammenfassung(ctx: dict):
    """Letzter Dialogschritt: alle Angaben prüfen, einzeln ändern, dann erzeugen (Human-in-the-loop)."""
    personen = {True: "mit Personen", False: "ohne Personen"}.get(ctx.get("personen_im_bild"), "–")
    zielgruppe = (db.get_profile(st.session_state.user_id).get("zielgruppe") or "").strip()
    zeilen = [("Thema", ctx.get("thema", "–"), "thema"),
              ("Wirkung", (_wirkung_label(WIRKUNGSEBENEN[ctx["wirkungsebene"]]) if ctx.get("wirkungsebene") in WIRKUNGSEBENEN else "–")
               + (f" ({ctx['wirkungsebene']})" if ctx.get("wirkungsebene") else ""), "wirkungsebene"),
              ("Ziel", ctx.get("kommunikationsziel", "–"), None),  # ergibt sich aus der Wirkung
              ("Art des Beitrags", CONTENTTYP_OPTIONEN.get(ctx.get("inhaltlicher_contenttyp"), ("–",))[0],
               "inhaltlicher_contenttyp"),
              # Zielgruppe (Eiring, 2026, Schritt 2) kommt aus der Registrierung; ändern im Profil
              ("Zielgruppe", (zielgruppe or "nicht angegeben") + " (aus deinem Profil)", None),
              ("Format", format_label(ctx.get("plattform_format")) + (f" · {verhaeltnis_aus(ctx)}" if ctx.get("plattform_format") else ""), "plattform_format"),
              ("Inhalt", CONTENT_TYPE_LABELS.get(ctx.get("content_type"), "–"), None)]  # fest: Bild + Caption
    if bild_erforderlich(ctx.get("content_type")):
        zeilen.append(("Personen im Bild", personen, "personen_im_bild"))
    for k in ctx.get("klaerungen", []):
        zeilen.append(("Rückfrage", f"{k['frage']} → {k['antwort']}", None))
    with st.container(border=True, key="dialog_zusammenfassung"):
        st.markdown("**Zusammenfassung – bitte prüfen**")
        for label, wert, feld in zeilen:
            c1, c2, c3 = st.columns([2, 6, 1], vertical_alignment="center")
            c1.caption(label)
            c2.markdown(html.escape(str(wert)))
            if feld and c3.button("Ändern", key=f"aendern_{feld}", type="tertiary"):
                angabe_aendern(feld)
                st.session_state.chat_history.append(
                    {"role": "user", "content": f"{label} ändern."})
                st.rerun()
        angebot = st.text_input("Angebot / Aktion (optional)", ctx.get("angebot", ""), key="zs_angebot",
                                placeholder="z. B. 10 % Rabatt auf Rosen, 15.–25.10.2026")
        if st.button("Vorschläge erstellen →", key="dialog_erzeugen", type="primary", width="stretch"):
            ctx["angebot"] = angebot.strip()
            if pruefe_und_generiere(erzwingen=True):
                st.rerun()
            st.rerun()


def render_chat_page():
    topbar("Dialog", "Erzähl der Content-Engine, was du heute brauchst.")

    with st.container(key="pagebody"):
        if not OPENAI_API_KEY:
            st.warning("API_CHATGPT ist nicht gesetzt – der Dialog kann keine Anfragen an die KI schicken.")
        zeige_kontext_hinweise(db.get_profile(st.session_state.user_id))

        if st.session_state.chat_context:
            if st.button("🔄 Neue Anfrage starten", key="reset_chat"):
                st.session_state.chat_context = {}
                st.session_state.chat_history = []
                st.rerun()

        profile = db.get_profile(st.session_state.user_id)
        if not st.session_state.chat_history:
            sie = profile.get("anrede") != "Du"
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": ("Hallo! Ich führe Sie in wenigen Schritten zum fertigen Beitrag. Was möchten Sie erstellen?"
                            if sie else
                            "Hallo! Ich führe dich in wenigen Schritten zum fertigen Beitrag. Was möchtest du erstellen?"),
            })

        # Nächste Frage der KI VOR dem Verlauf erzeugen, damit sie sofort sichtbar ist
        schritt = naechster_schritt(st.session_state.chat_context)
        if schritt in KI_SCHRITTE and schritt not in st.session_state.chat_context.get("fragen", {}):
            with st.spinner("Die Content-Engine formuliert die nächste Frage …"):
                frage_stellen(schritt)
        elif schritt == "wirkungsebene":
            feste_frage_stellen(schritt)
        elif schritt == "inhaltlicher_contenttyp":
            contenttyp_frage_stellen()
        elif schritt == "klaerung":
            with st.spinner("Die Content-Engine prüft, ob noch etwas unklar ist …"):
                klaerung_fragen()
            schritt = naechster_schritt(st.session_state.chat_context)

        profile = db.get_profile(st.session_state.user_id)
        user_initialen = initialen(profile.get("unternehmensname") or "Nutzer")

        chat_col, _ = st.columns([3, 1])
        with chat_col:
            for m in st.session_state.chat_history:
                rolle = "user" if m["role"] == "user" else "assistant"
                avatar_kuerzel = "→" if rolle == "assistant" else user_initialen
                # Inline-Styles statt nur CSS-Klassen: st.html-Blöcke werden teils
                # isoliert gerendert, dann greift das globale CSS (row-reverse) nicht
                # zuverlässig und Nutzer-/Bot-Nachrichten sehen gleich aus.
                ausrichtung = "flex-direction:row-reverse;" if rolle == "user" else ""
                bubble_style = (
                    "background:#E8DBCD;border-top-right-radius:4px;max-width:420px;"
                    if rolle == "user" else
                    "background:#fff;border:1px solid #E4DDD2;border-top-left-radius:4px;max-width:560px;"
                )
                avatar_bg = "#4B4E49" if rolle == "user" else "#6E6845"
                st.html(
                    f"""
                    <div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;{ausrichtung}">
                      <div style="width:30px;height:30px;border-radius:50%;flex:0 0 auto;display:flex;
                                  align-items:center;justify-content:center;font-size:12px;font-weight:700;
                                  color:#fff;background:{avatar_bg};">{html.escape(avatar_kuerzel)}</div>
                      <div style="padding:14px 18px;font-size:14px;line-height:1.6;border-radius:14px;
                                  font-family:'Work Sans',sans-serif;color:#181818;{bubble_style}">
                        {formatiere_chat_text(m['content'])}
                      </div>
                    </div>
                    """
                )

        ctx = st.session_state.chat_context

        if schritt == "modus":
            with st.container(key="chat_chips"):
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Beitrag erstellen", key="chip_modus_beitrag", width="stretch"):
                        ctx["modus"] = "beitrag"
                        chip_antwort("Ich möchte einen Beitrag erstellen.")
                        st.rerun()
                with c2:
                    if st.button(CONTENT_TYPE_LABELS["idee"], key="chip_ct_idee", width="stretch"):
                        ctx["content_type"] = "idee"
                        chip_antwort(f"Ich möchte: {CONTENT_TYPE_LABELS['idee']}.")
                        st.rerun()

        elif schritt == "plattform_format":
            with st.container(key="chat_chips"):
                st.caption("Wo und in welcher Form soll der Beitrag erscheinen?")
                c_pf, c_art, c_vh, c_ok = st.columns([2, 2, 2, 1], vertical_alignment="bottom")
                with c_pf:
                    plattform = st.selectbox("Plattform", list(PLATTFORMEN), format_func=PLATTFORMEN.get,
                                             key="auswahl_plattform")
                with c_art:
                    art = st.selectbox("Art des Beitrags", list(BEITRAGSARTEN), format_func=BEITRAGSARTEN.get,
                                       key="auswahl_beitragsart")
                schluessel = f"{'ig' if plattform == 'instagram' else 'fb'}_{art}"
                with c_vh:
                    # Feed und Karussell: das Unternehmen wählt 4:5 oder 1:1
                    if verhaeltnis_waehlbar(schluessel):
                        verhaeltnis = st.selectbox("Seitenverhältnis", list(VERHAELTNIS_OPTIONEN),
                                                   format_func=VERHAELTNIS_OPTIONEN.get, key="auswahl_verhaeltnis")
                    else:
                        verhaeltnis = "9:16"
                        st.selectbox("Seitenverhältnis", ["9:16"], disabled=True, key="auswahl_verhaeltnis_story")
                spec = spec_fuer(schluessel, verhaeltnis)
                with c_ok:
                    if st.button("Übernehmen", key="format_uebernehmen", type="primary", width='stretch'):
                        ctx["plattform_format"] = schluessel
                        ctx["seitenverhaeltnis"] = verhaeltnis if verhaeltnis_waehlbar(schluessel) else ""
                        chip_antwort(f"Format: {format_label(schluessel)} · {spec['verhaeltnis']}")
                        if ctx.get("content_type") == "idee":
                            # „Nur Idee“: keine Fragen nach Thema und Ziel – direkt verschiedene Contentvorschläge
                            ideen_direkt_erzeugen(ctx)
                        st.rerun()
                st.caption(" · ".join(vorgaben_zusammenfassung(spec)))

        elif schritt == "personen_im_bild":
            with st.container(key="chat_chips"):
                st.caption("Sollen im Bild Personen zu sehen sein?")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Mit Personen", key="chip_personen_ja", width='stretch'):
                        ctx["personen_im_bild"] = True
                        chip_antwort("Mit Personen im Bild.")
                        st.rerun()
                with c2:
                    if st.button("Ohne Personen", key="chip_personen_nein", width='stretch'):
                        ctx["personen_im_bild"] = False
                        chip_antwort("Ohne Personen im Bild.")
                        st.rerun()

        elif schritt in KI_SCHRITTE:
            frage = ctx.get("fragen", {}).get(schritt, {})
            vorschlaege = frage.get("vorschlaege") or []
            with st.container(key="chat_chips"):
                if vorschlaege:
                    spalten = st.columns(len(vorschlaege) + (1 if schritt == "thema" else 0))
                    for n, (col, wert) in enumerate(zip(spalten, vorschlaege)):
                        empfohlen = wert == frage.get("empfehlung")
                        with col:
                            if st.button(f"★ {wert}" if empfohlen else wert, key=f"antwort_{schritt}_{n}",
                                         type="primary" if empfohlen else "secondary", width="stretch",
                                         help="Empfehlung der Content-Engine" if empfohlen else None):
                                dialog_antwort_waehlen(schritt, wert)
                                st.rerun()
                    if schritt == "thema":
                        with spalten[-1]:
                            if st.button("💡 Andere Ideen", key="thema_neue_ideen", width="stretch"):
                                with st.spinner("Neue Ideen …"):
                                    frage_stellen("thema", neue_ideen=True)
                                st.rerun()
                st.caption("Antwort antippen oder unten eigene Antwort eingeben.")

        elif schritt == "wirkungsebene":
            with st.container(key="chat_chips"):
                spalten = st.columns(len(WIRKUNGSEBENEN))
                for col, (ebene, e) in zip(spalten, WIRKUNGSEBENEN.items()):
                    with col:
                        if st.button(_wirkung_label(e), key=f"wirkung_{ebene}", width="stretch", help=e["bedeutung"]):
                            wirkung_waehlen(ebene)
                            st.rerun()
                        st.caption(_wirkung_text(e))

        elif schritt == "kommunikationsziel":
            ziele = WIRKUNGSEBENEN.get(ctx.get("wirkungsebene"), {}).get("ziele") or ZIEL_OPTIONEN
            with st.container(key="chat_chips"):
                spalten = st.columns(len(ziele))
                for n, (col, ziel) in enumerate(zip(spalten, ziele)):
                    with col:
                        if st.button(ziel, key=f"antwort_kommunikationsziel_{n}", width="stretch"):
                            dialog_antwort_waehlen("kommunikationsziel", ziel)
                            st.rerun()
                st.caption("Ziele nach Meffert et al. (2019), passend zur gewählten Wirkung.")

        elif schritt == "inhaltlicher_contenttyp":
            with st.container(key="chat_chips"):
                spalten = st.columns(len(CONTENTTYP_OPTIONEN))
                for col, (typ, (label, erklaerung)) in zip(spalten, CONTENTTYP_OPTIONEN.items()):
                    erklaerung = CONTENTTYP_TEXT_SIE.get(typ, erklaerung) if _anrede_dialog() == "Sie" else erklaerung
                    with col:
                        if st.button(label, key=f"contenttyp_wahl_{typ}", width="stretch"):
                            contenttyp_festlegen(typ, "manuell")
                            st.rerun()
                        st.caption(erklaerung)

        elif schritt == "klaerung":
            offen = ctx.get("klaerung_offen") or {}
            with st.container(key="chat_chips"):
                vorschlaege = offen.get("vorschlaege") or []
                spalten = st.columns(len(vorschlaege) + 1)
                for n, (col, wert) in enumerate(zip(spalten, vorschlaege)):
                    with col:
                        if st.button(wert, key=f"klaerung_{len(ctx.get('klaerungen', []))}_{n}", width="stretch"):
                            klaerung_beantworten(wert)
                            st.rerun()
                with spalten[-1]:
                    if st.button("Weiter zur Zusammenfassung", key="klaerung_ueberspringen", width="stretch"):
                        ctx.pop("klaerung_offen", None)
                        ctx["klaerung_fertig"] = True
                        st.rerun()
                st.caption("Antwort antippen oder unten eigene Antwort eingeben.")

        elif schritt == "zusammenfassung":
            zeige_zusammenfassung(ctx)

        elif schritt == "ideen":
            with st.container(key="chat_chips"):
                c_zu, c_neu = st.columns(2)
                with c_zu:
                    if st.button("Zu den Ideen →", key="ideen_ansehen", type="primary", width="stretch",
                                 disabled=not (st.session_state.get("vorschlaege") or {}).get("ideen")):
                        navigiere("vorschlaege")
                        st.rerun()
                with c_neu:
                    if st.button("↻ Andere Ideen", key="ideen_dialog_neu", width="stretch"):
                        ideen_direkt_erzeugen(ctx)
                        st.rerun()
                st.caption("Du kannst unten auch ein Thema nennen – dann kommen Ideen genau dazu.")

        nutzer_text = st.chat_input("Eigene Antwort schreiben …")
        if nutzer_text:
            if schritt == "ideen":  # „Nur Idee“: Text = Themenwunsch für neue Ideen
                st.session_state.chat_history.append({"role": "user", "content": nutzer_text})
                ctx["thema"] = nutzer_text.strip()
                ideen_direkt_erzeugen(ctx)
            else:
                with st.spinner("Antwort wird ausgewertet …"):
                    dialog_freitext(nutzer_text)
            st.rerun()


# --- Seite: Vorschläge -------------------------------------------------------

def _bild_platzhalter(text: str, score=None):
    badge = f'<div class="ce-score-badge">● {html.escape(str(score))} Markenpassung</div>' if score is not None else ""
    st.html(
        f"""
        <div class="ce-card-image">
          <div class="ce-bild-symbol"></div>
          {badge}
        </div>
        <div style="text-align:center; font-size:11px; color:var(--ink-soft); margin-top:-6px;">{html.escape(text)}</div>
        """
    )




def render_ideen(data: dict, ctx: dict):
    """„Nur Idee“: Ideen-Karten statt fertiger Beiträge (keine Caption, kein Bild, keine Freigabe)."""
    ideen = data.get("ideen") or []
    st.caption(f"Ideen für {format_label(ctx.get('plattform_format'))} – das sind nur Vorschläge, was du "
               "umsetzen könntest, noch keine fertigen Beiträge.")
    if not ideen:
        st.warning("Es wurden keine Ideen geliefert. Bitte erneut versuchen.")
    cols = st.columns(max(1, min(3, len(ideen))))
    for n, idee in enumerate(ideen):
        with cols[n % len(cols)]:
            with st.container(border=True):
                typ = (CONTENTTYP_OPTIONEN.get(str(idee.get("contenttyp", "")).lower()) or (idee.get("contenttyp", ""),))[0]  # wie im Dialog
                st.markdown(f"**Idee {n + 1}: {idee.get('titel', '')}**")
                angaben = [f"Contenttyp: {typ}" if typ else "", f"Ziel: {idee['ziel']}" if idee.get("ziel") else ""]
                if any(angaben):
                    st.caption(" · ".join(a for a in angaben if a))
                st.markdown(idee.get("beschreibung", ""))
                if idee.get("umsetzung"):
                    st.markdown(f"**Umsetzung:** {idee['umsetzung']}")
                if idee.get("warum"):
                    st.caption(f"Warum passend: {idee['warum']}")
    st.html("<div style='height:8px'></div>")
    _, mid, _ = st.columns([2, 1, 2])
    with mid:
        if st.button("↻ Andere Ideen", width="stretch", key="ideen_neu"):
            profile = db.get_profile(st.session_state.user_id)
            try:
                erzeuge_vorschlaege_komplett(profile, ctx, [x.get("titel", "") for x in ideen])
                st.rerun()
            except Exception as e:
                st.error(f"Ideen konnten nicht erzeugt werden: {e}")


def render_vorschlaege_page():
    data = st.session_state.vorschlaege
    ctx = (data or {}).get("kontext") if isinstance(data, dict) and (data or {}).get("kontext") else st.session_state.chat_context
    thema = (data or {}).get("thema") or ctx.get("thema", "")
    subtitle = f'Thema: „{thema}"' if thema else ""
    if ctx.get("plattform_format"):
        subtitle += f" · Format: {format_label(ctx['plattform_format'])} · {verhaeltnis_aus(ctx)}"
    topbar("Vorschläge", subtitle)

    with st.container(key="pagebody"):
        if not data:
            st.info("Noch keine Vorschläge vorhanden. Starte zuerst einen Dialog.")
            if st.button("Zum Dialog", type="primary"):
                navigiere("chat")
                st.rerun()
            return

        if isinstance(data, dict) and "ideen" in data:
            render_ideen(data, ctx)
            return

        posts = data.get("posts", []) if isinstance(data, dict) else []
        if not posts:
            st.warning("Es wurden keine Posts gefunden.")
            return

        profile = db.get_profile(st.session_state.user_id)
        plattform = ctx.get("plattform_format", "ig_feed")
        verhaeltnis = verhaeltnis_aus(ctx)
        spec = spec_fuer(plattform, verhaeltnis)
        mit_bild = bild_erforderlich(ctx.get("content_type", "beides"))
        st.caption("Vorgaben " + format_label(plattform) + ": " + " · ".join(vorgaben_zusammenfassung(spec)))
        zeige_kontext_hinweise(profile)
        if mit_bild:
            pfad, _ = werbebild.finde_schriftdatei(profile.get("ci_schrift", ""), profile.get("ci_schrift_datei", ""))
            if (profile.get("ci_schrift") or profile.get("ci_schrift_datei")) and not pfad:
                zeige_schrift_status(profile)
        ansicht = None
        if data.get("evaluation"):  # Evaluationsmodus: zwischen Variante A und B umschalten
            ansicht = st.segmented_control("Variante", ["A", "B"], default="A", key="eval_ansicht",
                                           format_func=lambda v: {"A": "Variante 1",
                                                                  "B": "Variante 2"}[v]) or "A"
            if st.session_state.get("eval_fehler"):
                st.warning(f"Variante 2 konnte nicht erzeugt werden: {st.session_state['eval_fehler']}")
        cols = st.columns(min(3, len([p for p in posts if not ansicht or p.get("ab_variante") == ansicht]) or 1))

        for i, post in enumerate(posts):
            if ansicht and post.get("ab_variante") != ansicht:
                continue
            nummer = post.get("aspekt_nummer", i + 1)
            aspekt = post.get("aspekt", "")
            caption = post.get("full_caption", "")
            score = post.get("markenpassung")
            col = cols[i % len(cols)]
            karussell = ist_karussell(plattform)
            slides = karussell_slides(post, ctx) if karussell else []
            j = min(int(st.session_state.get(f"slide_{i}", 0)), max(0, len(slides) - 1)) if karussell else 0
            k = (lambda basis, _i=i, _j=j: sk(basis, _i, _j))
            standard_hl = slides[j]["headline"] if karussell else standard_headline(post, ctx)
            slide_post = karten_post(post, slides, j) if karussell else post
            st.session_state.setdefault(k("bildquelle"), "ki")
            bild_quelle_feld = st.session_state[k("bildquelle")]
            bild_profil = neutrales_profil(profile) if post.get("ab_variante") == "B" else profile

            with col:
                with st.container(border=True):
                    fb_plaetze = {}  # Platz für das Feedback direkt unter jedem Textfeld
                    fertiges_bild = None
                    bild = None
                    if mit_bild:
                        # 1) Basisbild bestimmen: eigenes Foto ODER KI-Bild
                        if bild_quelle_feld == "upload":
                            bild = st.session_state.get(k("bild"))
                            basis_bytes = bild.getvalue() if bild else None
                        else:
                            basis_bytes = st.session_state.get(k("ki_bild"))

                        # 2) Headline aufs Bild legen (CI-Farbe und -Schrift)
                        headline = hart_kuerzen(st.session_state.get(k("headline"), standard_hl),
                                                spec["ueberschrift_max"])
                        text_aufs_bild = st.session_state.get(f"textbild_{i}", True)
                        if basis_bytes:
                            try:
                                fertiges_bild = werbebild.erstelle_werbebild(
                                    basis_bytes,
                                    headline=headline if text_aufs_bild else "",
                                    plattform_format=plattform,
                                    ci_farbe=bild_profil.get("ci_farbe") or "#C9532E",
                                    ci_farbe_sekundaer=bild_profil.get("ci_farbe_sekundaer") or "",
                                    schrift_name=bild_profil.get("ci_schrift") or "",
                                    logo_pfad=bild_profil.get("logo_path") or "",
                                    schrift_datei=bild_profil.get("ci_schrift_datei") or "",
                                    logo_stil=st.session_state.get(f"logo_{i}", "kachel"),
                                    verhaeltnis=verhaeltnis,
                                    textflaeche=st.session_state.get(f"textflaeche_{i}", False),
                                    flaeche_deckkraft=st.session_state.get(f"deckkraft_{i}", 100),
                                    # Story: CTA als Knopf im Bild; Feed: CTA in der Caption
                                    cta=st.session_state.get(f"cta_{i}", post.get("cta", "")) if text_aufs_bild else "",
                                    ki_bild=bild_quelle_feld == "ki",  # Kennzeichnung „KI-generiert“ (Art. 50 KI-VO)
                                )
                            except Exception as e:
                                st.error(f"Bild konnte nicht verarbeitet werden: {e}")

                        if karussell:
                            zeige_karussell_streifen(karussell_fertige_bilder(i, slides, plattform, bild_profil, ctx), j,
                                                     spec["verhaeltnis"],
                                                     springen=st.session_state.pop(f"karte_sprung_{i}", False))
                            if fertiges_bild:
                                zeige_bildmodell(k("ki_modell"), bild_quelle_feld)
                            elif st.session_state.get(k("ki_fehler")):
                                with st.expander(f"Karte {j + 1}: KI-Bild fehlgeschlagen"):
                                    st.caption(st.session_state[k("ki_fehler")])
                        elif fertiges_bild:
                            st.image(fertiges_bild, width='stretch')
                            zeige_bildmodell(k("ki_modell"), bild_quelle_feld)
                            if score is not None:
                                st.caption(f"● {score} Markenpassung")
                        elif bild_quelle_feld == "upload":
                            _bild_platzhalter("Eigenes Foto unten hochladen – der Text kommt automatisch drauf", score)
                        elif st.session_state.get(k("ki_fehler")):
                            _bild_platzhalter("KI-Bild fehlgeschlagen – unten erneut versuchen", score)
                            with st.expander("Fehlerdetails"):
                                st.caption(st.session_state[k("ki_fehler")])
                        else:
                            _bild_platzhalter("Noch kein KI-Bild", score)

                    if karussell and mit_bild:
                        karussell_steuerung(i, j, slides, profile, ctx, post, plattform)

                    # Nur „Post n“ – der Unteraspekt ist eine interne Vorgabe und wird nicht angezeigt
                    st.markdown(f"**Post {nummer}**"
                                + (f" · Karussell mit {len(slides)} Karten" if karussell else ""))

                    if mit_bild:
                        bu_col, bk_col = st.columns(2)
                        with bu_col:
                            if st.button("Eigenes Foto", key=k("btn_upload"), width='stretch',
                                         type="primary" if bild_quelle_feld == "upload" else "secondary"):
                                st.session_state[k("bildquelle")] = "upload"
                                st.rerun()
                        with bk_col:
                            if st.button("KI-Bild", key=k("btn_ki"), width='stretch',
                                         type="primary" if bild_quelle_feld == "ki" else "secondary"):
                                st.session_state[k("bildquelle")] = "ki"
                                st.rerun()

                        if bild_quelle_feld == "upload":
                            st.file_uploader("Foto hochladen", type=["png", "jpg", "jpeg"], key=k("bild"),
                                             label_visibility="collapsed")
                        else:
                            hat_bild = bool(st.session_state.get(k("ki_bild")))
                            if st.button("↻ Neues KI-Bild" if hat_bild else "✨ KI-Bild erzeugen",
                                         key=k("ki_generieren"), width='stretch'):
                                prompt = baue_bild_prompt(profile, ctx, slide_post)
                                with st.spinner("Bild wird generiert …"):
                                    try:
                                        bild_bytes, modell = generiere_ki_bild(prompt, plattform, verhaeltnis=verhaeltnis_aus(ctx))
                                        st.session_state[k("ki_bild")] = bild_bytes
                                        st.session_state[k("ki_prompt")] = prompt
                                        st.session_state[k("ki_modell")] = {"modell": modell}
                                        st.session_state.pop(k("ki_fehler"), None)
                                        st.rerun()
                                    except Exception as e:
                                        st.error(str(e))

                        # Öffnet sich automatisch, wenn Feedback ein Feld darin betrifft
                        if st.session_state.pop(f"textexp_oeffnen_{i}", False):
                            st.session_state[f"textexp_{i}"] = True
                        with st.expander("Texte der Karten und Logo" if karussell else "Text und Logo auf dem Bild",
                                         key=f"textexp_{i}", on_change="rerun"):
                            st.checkbox("Text aufs Bild legen", value=True, key=f"textbild_{i}")
                            textflaeche_haken(i)  # bei den übrigen Bild-Einstellungen
                            if logo_vorhanden(bild_profil):
                                st.radio("Logo", list(LOGO_STILE), format_func=LOGO_STILE.get, key=f"logo_{i}",
                                         horizontal=True)
                            if karussell:
                                # Karussell: je Karte eine eigene Headline (aufs Bild) und ein Text zur Karte
                                st.caption("Je Karte: Headline (steht mittig auf dem Bild, vorzugsweise höchstens "
                                           "5 Wörter) und ein Text zur Karte (nur hier, nicht auf dem Bild).")
                                for x, karte in enumerate(slides):
                                    st.markdown(f"**Karte {x + 1}**")
                                    st.text_input("Headline", hart_kuerzen(karte["headline"], spec["ueberschrift_max"]),
                                                  key=sk("headline", i, x), max_chars=spec["ueberschrift_max"] or None)
                                    st.text_area("Text zur Karte", karte["text"], key=sk("kartentext", i, x), height=80)
                                    fb_plaetze[f"karte_{x + 1}"] = st.container()
                            else:
                                st.text_input(
                                    "Headline", hart_kuerzen(standard_hl, spec["ueberschrift_max"]),
                                    key=k("headline"), max_chars=spec["ueberschrift_max"] or None,
                                    help="Aufmerksamkeit (AIDA: Attention) – kurz und prägnant."
                                         + (f" Max. {spec['ueberschrift_max']} Zeichen." if spec["ueberschrift_max"] else ""),
                                )
                                fb_plaetze["headline"] = st.container()
                            if ohne_caption(plattform):  # Story: keine Caption -> CTA als Knopf im Bild
                                st.text_input("Call-to-Action (Knopf im Bild)", post.get("cta", ""), key=f"cta_{i}",
                                              help="Handlungsaufforderung (AIDA: Action, Appell nach Schulz von Thun). "
                                                   "Steht als Knopf unter der Headline. Leer lassen = kein Knopf.")
                                fb_plaetze["cta"] = st.container()
                                st.caption("Kurze Headline (vorzugsweise höchstens 5 Wörter) mittig auf dem Bild, "
                                           "darunter der CTA-Knopf.")
                            elif not karussell:  # Feed: nur die Headline mittig; CTA steht in der Caption
                                st.caption("Nur eine kurze Headline (vorzugsweise höchstens 5 Wörter), mittig auf "
                                           "dem Bild. Die Handlungsaufforderung steht am Ende der Caption.")

                    regel = hinweise.hashtag_regel_gilt(plattform)
                    if ohne_caption(plattform):  # Story: keine Caption, keine Hashtags
                        caption = caption_text = edited_caption = ""
                        st.caption("Storys werden ohne Caption veröffentlicht.")
                    else:
                        if regel and not post.get("hashtags_liste"):
                            # Absicherung: Hashtags stehen noch im Text (z. B. Aufbereitung abgebrochen) ->
                            # abtrennen und nach der Regel aufbauen: 1. Unternehmensname, 2. Ort, dann thematisch
                            caption, tags_im_text = hinweise.trenne_hashtags(caption)
                            vorschlaege = [t if str(t).startswith("#") else f"#{t}" for t in (post.get("hashtags") or [])]
                            post["full_caption"] = caption
                            post["hashtags_liste"] = hinweise.hashtags_aufbauen(profile, vorschlaege + tags_im_text)
                        caption_text = st.text_area(
                            "Caption", hart_kuerzen(caption, spec["primaertext_max"]), height=120, key=f"caption_{i}",
                            label_visibility="collapsed", max_chars=spec["primaertext_max"] or None,
                        )
                        fb_plaetze["caption"] = st.container()
                        if regel:
                            # Feed/Reel: Hashtags in eigener Zeile, zählen nicht zum Primärtext-Limit
                            hashtag_zeile = st.text_input(
                                "Hashtags", " ".join(post.get("hashtags_liste") or []), key=f"hashtags_{i}",
                                help="Genau 5 Hashtags: 1. Unternehmensname, 2. Ort, 3.–5. thematisch. "
                                     "Sie stehen unter dem Text und zählen nicht zum Zeichenlimit.")
                            hashtag_liste = hinweise.trenne_hashtags(hashtag_zeile)[1]
                            edited_caption = hinweise.caption_zusammensetzen(caption_text, hashtag_liste)
                        else:
                            edited_caption = caption_text
                        if post.get("original_caption"):
                            with st.expander("Ursprünglicher Entwurf (vor der Selbstprüfung)"):
                                st.caption(post["original_caption"])
                        zeichen_info = f"{len(caption_text)}"
                        if spec["primaertext_max"]:
                            zeichen_info += (f" / {spec['primaertext_min']}–{spec['primaertext_max']}"
                                             if spec["primaertext_min"] else f" / {spec['primaertext_max']}")
                        st.caption(f"Primärtext: {zeichen_info} Zeichen · {len(caption_text.split())} Wörter"
                                   + (" (ohne Hashtags)" if regel else "")
                                   + (f" · Empfehlung: {spec['caption_empfehlung']}" if spec.get("caption_empfehlung") else ""))
                    for hinweis in pruefe_texte(spec, caption_text,
                                                st.session_state.get(f"headline_{i}", "") if mit_bild else ""):
                        st.warning(hinweis, icon="⚠️")

                    # Automatische Hinweise (CI-Vorgaben, Anrede, veraltete Daten, Hashtag-Regel)
                    plant = st.session_state.get(f"aktion_{i}") == "Für Kalender einplanen"
                    stichtag = st.session_state.get(f"datum_{i}") if plant else None
                    zeige_ci_hinweise(
                        profile, edited_caption,
                        st.session_state.get(f"headline_{i}", "") if mit_bild and st.session_state.get(f"textbild_{i}", True) else "",
                        stichtag or datetime.date.today(),
                        f"hashtags_{i}" if regel else f"caption_{i}",
                        None if regel else spec["primaertext_max"],
                        hashtag_regel=regel,
                        text_key=f"caption_{i}",
                    )

                    # Konstruktiv-kritisches Feedback NUR zu eigenen Texten der Nutzer:innen und nur auf Wunsch,
                    # für alle Formate gleich, sobald ein Textelement selbst geändert wurde
                    generiert = textelemente_standard(post, plattform, ctx, spec, caption)
                    eingaben = textelemente_aus_eingaben(i, generiert)
                    bild_konzept = ((st.session_state.get(f"ki_prompt_{i}", "") if bild_quelle_feld == "ki"
                                     else "eigenes Foto") if mit_bild else "")
                    feedback = feedback_unter_feldern(i, generiert, eingaben, fb_plaetze, profile, ctx, plattform,
                                                      bild_konzept)
                    zeige_ueberarbeiten(i, profile, ctx, post, spec, feedback,
                                        mit_bild, bild_quelle_feld, plattform, generiert)

                    wunsch = st.session_state.get("wunsch_datum")  # im Kalender angetippter Tag
                    with st.expander("Jetzt veröffentlichen oder für Kalender einplanen", expanded=bool(wunsch)):
                        # Startwerte nur setzen, wenn das Feld noch nicht im Session State steht
                        # (der Kalender belegt es ggf. vor – beides zusammen löst eine Streamlit-Warnung aus)
                        start = lambda key, wert: {} if key in st.session_state else wert  # noqa: E731
                        aktion = st.radio(
                            "Aktion", ["Jetzt veröffentlichen", "Für Kalender einplanen"],
                            key=f"aktion_{i}", label_visibility="collapsed",
                            **start(f"aktion_{i}", {"index": 1 if wunsch else 0}),
                        )
                        geplantes_datum = geplante_zeit = None
                        if aktion == "Für Kalender einplanen":
                            geplantes_datum = st.date_input("Datum", min_value=datetime.date.today(), key=f"datum_{i}",
                                                            format="DD.MM.YYYY",
                                                            **start(f"datum_{i}", {"value": wunsch or "today"}))
                            geplante_zeit = st.time_input("Uhrzeit", key=f"zeit_{i}",
                                                          **start(f"zeit_{i}", {"value": datetime.time(9, 0) if wunsch else "now"}))

                        # Faktencheck vor der Freigabe (nur gegen das Profil)
                        fc_key = f"faktencheck_{i}"
                        entwurf = {
                            "caption": edited_caption,
                            "headline": (" | ".join(st.session_state.get(sk("headline", i, x), sl["headline"]) for x, sl in enumerate(slides)) if karussell else st.session_state.get(f"headline_{i}", "")) if mit_bild else "", "kartentexte": [st.session_state.get(sk("kartentext", i, x), sl["text"]) for x, sl in enumerate(slides)] if karussell else [],
                            "angebot": st.session_state.get(f"angebot_{i}", "") if mit_bild else "",
                            "geplantes_datum": geplantes_datum.isoformat() if geplantes_datum else "",
                            # vom Unternehmen im Dialog bestätigte Angaben (Rückfragen) gelten als belegt
                            "bestaetigte_angaben_aus_dialog": [f"{k['frage']} → {k['antwort']}"
                                                               for k in ctx.get("klaerungen", [])],
                        }
                        signatur = json.dumps(entwurf, sort_keys=True, ensure_ascii=False)
                        fc = st.session_state.get(fc_key)
                        if fc and fc["signatur"] != signatur:  # Text geändert -> alter Check gilt nicht mehr
                            st.session_state.pop(fc_key)
                            fc = None
                        freigeben = False
                        if st.button("Auswählen & freigeben", key=f"bestaetigen_{i}", type="primary",
                                     width='stretch'):
                            if mit_bild and not fertiges_bild:
                                st.error("Bitte zuerst ein Foto hochladen oder ein KI-Bild erzeugen.")
                            elif mit_bild and karussell and None in karussell_fertige_bilder(i, slides, plattform, bild_profil, ctx):
                                st.error("Für das Karussell fehlen noch Bilder – bitte alle Karten mit Bild versehen "
                                         "(„KI-Bilder für alle Karten erzeugen“ oder je Karte ein eigenes Foto).")
                            else:
                                if fc is None:
                                    with st.spinner("Faktencheck gegen dein Profil …"):
                                        fc = faktencheck_vor_freigabe(st.session_state.user_id, entwurf)
                                    fc["signatur"] = signatur
                                    st.session_state[fc_key] = fc
                                freigeben = not fc["blockierend"]
                        if fc:
                            zeige_faktencheck_ergebnis(fc)
                            if fc["blockierend"] and not freigeben:
                                st.caption("Passe die Caption an (der Check läuft dann beim Freigeben erneut) "
                                           "oder gib den Beitrag bewusst trotzdem frei.")
                                if st.button("Trotzdem freigeben", key=f"trotzdem_{i}", width="stretch"):
                                    freigeben = True
                        if freigeben:
                            try:
                                image_url, slide_urls, karten_bilder = "", "", []
                                # Das FERTIGE Werbebild (inkl. Text) wird in beiden Fällen dauerhaft
                                # gespeichert – der Beitrag erscheint danach im Kalender.
                                if mit_bild and karussell:
                                    karten_bilder = [b for b in karussell_fertige_bilder(i, slides, plattform, bild_profil, ctx) if b]
                                    urls = [speichere_generiertes_bild(b, st.session_state.user_id) for b in karten_bilder]
                                    image_url = urls[0] if urls else ""
                                    slide_urls = json.dumps(urls)
                                elif fertiges_bild:
                                    image_url = speichere_generiertes_bild(fertiges_bild, st.session_state.user_id)

                                if aktion == "Jetzt veröffentlichen":
                                    # Simulierte Veröffentlichung (siehe render_instagram_simulation):
                                    # kein echter Meta-Graph-API-Call, da das verknüpfte Instagram-/
                                    # Facebook-Konten je Testnutzer:in voraussetzen würde.
                                    # Gespeichert wird trotzdem: Status „veröffentlicht“, Zeitpunkt = jetzt.
                                    jetzt = datetime.datetime.now()
                                    db.add_scheduled_post(
                                        user_id=st.session_state.user_id, aspekt=aspekt, caption=edited_caption,
                                        image_url=image_url, bild_quelle=bild_quelle_feld if mit_bild else "",
                                        scheduled_date=jetzt.date().isoformat(),
                                        scheduled_time=jetzt.strftime("%H:%M"),
                                        status="veroeffentlicht", plattform_format=plattform, slide_urls=slide_urls,
                                        seitenverhaeltnis=verhaeltnis,
                                    )
                                    render_instagram_simulation(
                                        profile.get("unternehmensname", ""), edited_caption,
                                        bild_bytes=karten_bilder[0] if karten_bilder else fertiges_bild,
                                        image_url=image_url, plattform_format=plattform, slides=karten_bilder, verhaeltnis=verhaeltnis,
                                    )
                                    st.success(f"Post {nummer} wurde (simuliert) auf {format_label(plattform)} "
                                               f"veröffentlicht und im Kalender gespeichert "
                                               f"({datum_lang(jetzt.date())}, {jetzt.strftime('%H:%M')} Uhr).")
                                else:
                                    db.add_scheduled_post(
                                        user_id=st.session_state.user_id, aspekt=aspekt, caption=edited_caption,
                                        image_url=image_url, bild_quelle=bild_quelle_feld if mit_bild else "",
                                        scheduled_date=geplantes_datum.isoformat(),
                                        scheduled_time=geplante_zeit.strftime("%H:%M"),
                                        plattform_format=plattform, slide_urls=slide_urls,
                                        seitenverhaeltnis=verhaeltnis,
                                    )
                                    st.success(f"Post {nummer} wurde für {datum_lang(geplantes_datum)}, "
                                               f"{geplante_zeit.strftime('%H:%M')} Uhr eingeplant.")
                                    if st.session_state.get("wunsch_datum") == geplantes_datum:
                                        st.session_state.wunsch_datum = None
                            except Exception as e:
                                st.error(f"Aktion fehlgeschlagen: {e}")

        st.html("<div style='height:8px'></div>")
        _, mid, _ = st.columns([2, 1, 2])
        with mid:
            if st.button("↻ Neue Vorschläge generieren", width='stretch'):
                bisherige_aspekte = [p.get("aspekt", "") for p in posts if p.get("aspekt")]
                try:
                    erzeuge_vorschlaege_komplett(profile, ctx, bisherige_aspekte)
                    st.rerun()
                except Exception as e:
                    st.error(f"Generierung fehlgeschlagen: {e}")



# --- Seite: Kalender ----------------------------------------------------------

STATUS_TEXT = {"geplant": "Geplant · freigegeben", "veroeffentlicht": "Veröffentlicht"}


def post_titel(p: dict) -> str:
    """Kalender: Beiträge heißen nur „Post 1“, „Post 2“ … (fortlaufend nach Termin) – der Unteraspekt wird nicht
    angezeigt."""
    alle = db.list_scheduled_posts(p.get("user_id") or st.session_state.user_id)
    ids = [x["id"] for x in sorted(alle, key=lambda x: (x["scheduled_date"], x["scheduled_time"], x["id"]))]
    return f"Post {ids.index(p['id']) + 1}" if p.get("id") in ids else "Post"


def _beitrag_kurz(p: dict) -> str:
    pf = str(p.get("plattform_format") or "")
    kanal = "IG · " if pf.startswith("ig") else "FB · " if pf.startswith("fb") else ""
    symbol = "✅" if p["status"] == "veroeffentlicht" else "🕒"
    return f"{symbol} {p['scheduled_time']} · {kanal}{post_titel(p)}"


def _beitrag_tooltip(p: dict) -> str:
    """Vorschau beim Darüberfahren mit der Maus (Streamlit-Tooltip, Markdown)."""
    caption = (p.get("caption") or "").replace("\n", " ")
    if len(caption) > 180:
        caption = caption[:179] + "…"
    return (f"**{post_titel(p)}**  \n"
            f"{datum_lang(p['scheduled_date'])} · {p['scheduled_time']} Uhr  \n"
            f"{format_label(p.get('plattform_format')) or 'Beitrag'} · {STATUS_TEXT.get(p['status'], p['status'])}"
            f"\n\n{caption}\n\n*Klicken zum Öffnen*")


@st.cache_data(show_spinner=False, max_entries=64)
def _vorschaubild_datenurl(pfad: str) -> str:
    """Verkleinertes Bild als data-URL (lokale Dateien kann der Browser nicht direkt laden)."""
    from PIL import Image
    import io
    with Image.open(pfad) as im:
        im = im.convert("RGB")
        im.thumbnail((96, 120))
        puffer = io.BytesIO()
        im.save(puffer, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(puffer.getvalue()).decode()


def _vorschaubild_html(image_url: str) -> str:
    """Kleines Vorschaubild für „Als Nächstes geplant“; ohne Bild bleibt der Platzhalter."""
    src = ""
    quelle = bild_fuer_anzeige(image_url)
    if isinstance(quelle, str):
        try:
            src = _vorschaubild_datenurl(quelle)
        except Exception:
            src = ""
    elif quelle:
        src = "data:image/jpeg;base64," + _vorschau_base64(quelle, 240)
    if not src:
        return '<div class="ce-kommend-thumb"></div>'
    return (f'<div class="ce-kommend-thumb" style="background:url(\'{html.escape(src)}\') center/cover;"></div>')


def _dialog_zustand_leeren():
    """Eingaben eines früher geöffneten Dialogs verwerfen (sonst tauchen ungespeicherte Änderungen wieder auf)."""
    for key in [k for k in st.session_state.keys() if str(k).startswith("dlg_")]:
        del st.session_state[key]


@st.dialog("Beitrag", width="large")
def beitrag_dialog(post_id: int):
    """Detailansicht: Vorschau, Caption und Termin bis zur Veröffentlichung änderbar,
    veröffentlichen oder löschen (Human-in-the-loop)."""
    uid = st.session_state.user_id
    profile = db.get_profile(uid)
    p = db.get_scheduled_post(uid, post_id)
    if not p:
        st.error("Beitrag nicht gefunden.")
        return
    geplant = p["status"] == "geplant"
    spec = spec_fuer(p.get("plattform_format"), p.get("seitenverhaeltnis") or "")
    st.html(f"<span class='ce-status {p['status']}'>{html.escape(STATUS_TEXT.get(p['status'], ''))}</span>")
    st.markdown(f"#### {post_titel(p)}")

    cap_key = f"dlg_caption_{post_id}"
    tag_key = f"dlg_hashtags_{post_id}"
    regel = hinweise.hashtag_regel_gilt(p.get("plattform_format"))
    if regel:  # Feed/Reel: Text und Hashtag-Zeile getrennt bearbeiten
        gespeicherter_text, gespeicherte_tags = hinweise.trenne_hashtags(p["caption"])
        st.session_state.setdefault(cap_key, gespeicherter_text)
        st.session_state.setdefault(tag_key, " ".join(gespeicherte_tags))
    else:
        st.session_state.setdefault(cap_key, p["caption"])
    links, rechts = st.columns([2, 3], gap="large")
    with rechts:
        quelle = {"upload": "Eigenes Foto", "ki": "KI-Bild"}.get(p.get("bild_quelle"), "ohne Bild")
        st.caption(" · ".join(x for x in (format_label(p.get("plattform_format")), quelle) if x))
        if geplant:
            alt_datum = datetime.date.fromisoformat(p["scheduled_date"])
            c1, c2 = st.columns(2)
            with c1:
                datum = st.date_input("Datum", value=alt_datum, min_value=min(alt_datum, datetime.date.today()),
                                      key=f"dlg_datum_{post_id}", format="DD.MM.YYYY")
            with c2:
                zeit = st.time_input("Uhrzeit", value=datetime.time.fromisoformat(p["scheduled_time"]),
                                     key=f"dlg_zeit_{post_id}")
            if ohne_caption(p.get("plattform_format")):  # Story: keine Caption
                caption_text = ""
            else:
                caption_text = st.text_area("Caption", key=cap_key, height=160,
                                            max_chars=spec["primaertext_max"] or None)
            if regel:
                tags = hinweise.trenne_hashtags(st.text_input(
                    "Hashtags", key=tag_key, help="Genau 5 Hashtags: 1. Unternehmensname, 2. Ort, 3.–5. thematisch."))[1]
                caption = hinweise.caption_zusammensetzen(caption_text, tags)
            else:
                caption = caption_text
            if ohne_caption(p.get("plattform_format")):
                st.caption("Storys werden ohne Caption veröffentlicht.")
            else:
                st.caption(f"Primärtext: {len(caption_text)}"
                           + (f" / {spec['primaertext_max']}" if spec["primaertext_max"] else "") + " Zeichen"
                           + (" (ohne Hashtags)" if regel else ""))
            for h in pruefe_texte(spec, caption_text, ""):
                st.warning(h, icon="⚠️")
            zeige_ci_hinweise(profile, caption, "", datum, tag_key if regel else cap_key,
                              None if regel else spec["primaertext_max"], hashtag_regel=regel)

            def aenderungen_speichern() -> bool:
                if datum < datetime.date.today():
                    st.error("Das Datum darf nicht in der Vergangenheit liegen.")
                    return False
                db.update_scheduled_post(uid, post_id, caption, datum.isoformat(), zeit.strftime("%H:%M"))
                return True

            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button("Änderungen speichern", type="primary", key=f"dlg_speichern_{post_id}", width="stretch"):
                    if aenderungen_speichern():
                        st.success("Änderungen gespeichert.")
            with b2:
                if st.button("Jetzt veröffentlichen", key=f"dlg_publish_{post_id}", width="stretch"):
                    if aenderungen_speichern():
                        db.mark_post_published(st.session_state.user_id, post_id)
                        st.session_state.kalender_vorschau = db.get_scheduled_post(uid, post_id)
                        st.rerun()  # schließt den Dialog, Kalender zeigt die (simulierte) Veröffentlichung
            with b3:
                if st.button("Löschen", key=f"dlg_loeschen_btn_{post_id}", width="stretch"):
                    st.session_state[f"dlg_loeschen_{post_id}"] = True
            if st.session_state.get(f"dlg_loeschen_{post_id}"):
                st.warning("Diesen Beitrag wirklich löschen?")
                j, n = st.columns(2)
                with j:
                    if st.button("Ja, löschen", key=f"dlg_loeschen_ja_{post_id}", width="stretch"):
                        db.delete_scheduled_post(uid, post_id)
                        st.session_state.kal_meldung = f"„{p['aspekt'] or 'Beitrag'}“ wurde gelöscht."
                        st.rerun()
                with n:
                    if st.button("Abbrechen", key=f"dlg_loeschen_nein_{post_id}", width="stretch"):
                        st.session_state.pop(f"dlg_loeschen_{post_id}", None)
                        st.rerun(scope="fragment")
        else:
            caption = p["caption"]
            st.caption(f"{datum_lang(p['scheduled_date'])} · {p['scheduled_time']} Uhr")
            st.info("Dieser Beitrag ist bereits (simuliert) veröffentlicht und kann nicht mehr geändert werden.")
    with links:
        render_instagram_simulation(profile.get("unternehmensname", ""), caption, image_url=p.get("image_url") or "",
                                    plattform_format=p.get("plattform_format", ""), slides=_slide_urls(p),
                                    verhaeltnis=p.get("seitenverhaeltnis") or "")


@st.dialog("Beitrag einplanen")
def tag_dialog(iso: str):
    """Klick/Tipp auf einen Kalendertag: Beitrag für diesen Tag einplanen?"""
    tag = datetime.date.fromisoformat(iso)
    st.markdown(f"#### {datum_lang(tag)}")
    am_tag = [p for p in db.list_scheduled_posts(st.session_state.user_id) if p["scheduled_date"] == iso]
    for p in am_tag:
        c_text, c_knopf = st.columns([4, 1], vertical_alignment="center")
        with c_text:
            st.write(_beitrag_kurz(p))
        with c_knopf:
            if st.button("Öffnen", key=f"dlg_tag_oeffnen_{p['id']}", width="stretch"):
                st.session_state.kal_oeffnen = ("beitrag", p["id"])
                st.rerun()
    if tag < datetime.date.today():
        st.warning("Dieser Tag liegt in der Vergangenheit. Beiträge können nur ab heute eingeplant werden.")
        if st.button("Schließen", width="stretch"):
            st.rerun()
        return

    st.write("Möchtest du an diesem Tag einen weiteren Beitrag einplanen?" if am_tag
             else "Möchtest du an diesem Tag einen Beitrag einplanen?")
    if st.button("Neuen Beitrag erstellen", type="primary", width="stretch"):
        # Frischer Dialog; der Termin wird später bei den Vorschlägen vorausgewählt
        st.session_state.wunsch_datum = tag
        st.session_state.chat_context = {}
        st.session_state.chat_history = []
        navigiere("chat")
        st.rerun()
    vorhandene = (st.session_state.vorschlaege or {}).get("posts", []) if isinstance(st.session_state.vorschlaege, dict) else []
    if vorhandene and st.button("Einen der aktuellen Vorschläge einplanen", width="stretch"):
        st.session_state.wunsch_datum = tag
        for i in range(len(vorhandene)):
            st.session_state[f"aktion_{i}"] = "Für Kalender einplanen"
            st.session_state[f"datum_{i}"] = tag
            st.session_state.setdefault(f"zeit_{i}", datetime.time(9, 0))
        navigiere("vorschlaege")
        st.rerun()
    if st.button("Nein, danke", width="stretch"):
        st.rerun()


def render_kalender_page():
    year, month = st.session_state.kalender_monat
    topbar("Kalender", f"{MONATE[month - 1]} {year} · geplante und veröffentlichte Beiträge")

    with st.container(key="pagebody"):
        meldung = st.session_state.pop("kal_meldung", None)
        if meldung:
            st.success(meldung)
        col_main, col_side = st.columns([3, 1], gap="large")

        with col_main, st.container(key="kalraster"):
            col_prev, col_title, col_next = st.columns([1, 5, 1], vertical_alignment="center")
            with col_prev:
                if st.button("‹", key="monat_zurueck", help="Vorheriger Monat"):
                    st.session_state.kalender_monat = (year - 1, 12) if month == 1 else (year, month - 1)
                    st.rerun()
            with col_title:
                st.html(f"<div class='ce-monat'>{MONATE[month - 1]} {year}</div>")
            with col_next:
                if st.button("›", key="monat_vor", help="Nächster Monat"):
                    st.session_state.kalender_monat = (year + 1, 1) if month == 12 else (year, month + 1)
                    st.rerun()

            posts = db.list_scheduled_posts(st.session_state.user_id, year=year, month=month)
            posts_by_day = {}
            for p in posts:
                posts_by_day.setdefault(int(p["scheduled_date"].split("-")[2]), []).append(p)

            heute = datetime.date.today()
            wochen = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
            header_cols = st.columns(7, gap="small")
            for c, name in zip(header_cols, ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]):
                with c:
                    st.html(f"<div class='ce-wochentag'>{name}</div>")

            for woche in wochen:
                cols = st.columns(7, gap="small")
                for col, tag in zip(cols, woche):
                    with col:
                        if tag == 0:
                            st.html("<div class='ce-day-leer'></div>")
                            continue
                        datum = datetime.date(year, month, tag)
                        iso = datum.isoformat()
                        art = "heute" if datum == heute else "vergangen" if datum < heute else "tag"
                        # „_mitpost“: auf dem Handy als Punkt unter der Tageszahl markiert (Beiträge selbst in der Liste)
                        markierung = "_mitpost" if posts_by_day.get(tag) else ""
                        with st.container(border=True, key=f"kalzelle_{art}{markierung}_{iso}"):
                            # Tageszahl: Klick fragt, ob hier ein Beitrag eingeplant werden soll
                            if st.button(f"**{tag}**" if art == "heute" else str(tag), key=f"kaltag_{iso}",
                                         type="tertiary", help=f"{datum_lang(datum)} – Beitrag einplanen"):
                                st.session_state.kal_oeffnen = ("tag", iso)
                            # Beiträge: Maus darüber = Vorschau (Tooltip), Klick = Detailansicht
                            for p in posts_by_day.get(tag, []):
                                if st.button(_beitrag_kurz(p), key=f"kalpost_{p['status']}_{p['id']}",
                                             help=_beitrag_tooltip(p), width="stretch"):
                                    st.session_state.kal_oeffnen = ("beitrag", p["id"])
            st.html("<div class='ce-kal-hilfe-desktop'>Maus über einen Beitrag = Vorschau · Klick auf einen Beitrag = "
                    "öffnen und bearbeiten · Klick auf eine Tageszahl = Beitrag einplanen</div>"
                    "<div class='ce-kal-hilfe-handy'>● = Beitrag geplant · Tag antippen = Beiträge öffnen oder neuen "
                    "Beitrag einplanen</div>")

        with col_side, st.container(key="kalliste"):
            st.markdown("##### Als Nächstes geplant")
            kommende = [p for p in db.list_scheduled_posts(st.session_state.user_id) if p["status"] == "geplant"]
            if not kommende:
                st.caption("Keine geplanten Beiträge.")
            for p in kommende[:8]:
                st.html(
                    f"""
                    <div class="ce-kommend">
                      {_vorschaubild_html(p.get("image_url") or "")}
                      <div>
                        <div class="ce-kommend-titel">{html.escape(post_titel(p))}</div>
                        <div class="ce-kommend-sub">{datum_lang(p['scheduled_date'])} · {p['scheduled_time']} Uhr{(' · ' + format_label(p.get('plattform_format'))) if p.get('plattform_format') else ''}</div>
                      </div>
                    </div>
                    """
                )
                c_a, c_b = st.columns(2)
                with c_a:
                    if st.button("Bearbeiten", key=f"ansehen_{p['id']}", help=_beitrag_tooltip(p), width="stretch"):
                        st.session_state.kal_oeffnen = ("beitrag", p["id"])
                with c_b:
                    if st.button("Veröffentlichen", key=f"publish_{p['id']}", width="stretch"):
                        db.mark_post_published(st.session_state.user_id, p["id"])
                        # Vorschau merken statt direkt anzeigen + rerun (sonst verschwindet sie sofort)
                        st.session_state.kalender_vorschau = db.get_scheduled_post(st.session_state.user_id, p["id"])
                        st.rerun()

            vorschau = st.session_state.pop("kalender_vorschau", None)
            if vorschau:
                profile = db.get_profile(st.session_state.user_id)
                plattform_name = "Facebook" if str(vorschau.get("plattform_format", "")).startswith("fb") else "Instagram"
                st.success(f"Der Beitrag wurde auf deinem {plattform_name}-Profil veröffentlicht.")
                render_instagram_simulation(
                    profile.get("unternehmensname", ""), vorschau["caption"], image_url=vorschau["image_url"],
                    plattform_format=vorschau.get("plattform_format", ""), slides=_slide_urls(vorschau),
                    verhaeltnis=vorschau.get("seitenverhaeltnis") or "",
                )

            if st.button("+ Neuen Beitrag planen", width='stretch'):
                navigiere("chat" if not st.session_state.vorschlaege else "vorschlaege")
                st.rerun()

    # Dialog öffnen (immer nur einer pro Durchlauf)
    ziel = st.session_state.pop("kal_oeffnen", None)
    if ziel:
        _dialog_zustand_leeren()
        if ziel[0] == "beitrag":
            beitrag_dialog(ziel[1])
        else:
            tag_dialog(ziel[1])


# --- Seite: Unternehmensprofil -------------------------------------------------

def _website_norm(url: str) -> str:
    return (url or "").strip().rstrip("/").lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")


def website_tonalitaet_feld(profile: dict, url: str):
    """Direkt unter dem Website-Feld: Die Website wird NUR ausgelesen, wenn der
    Knopf aktiv angeklickt wird. Das Ergebnis wird angezeigt und beim Speichern des Profils übernommen.
    Steht in einem st.form – deshalb form_submit_button (löst nicht das Speichern aus)."""
    if st.form_submit_button("Tonalität der Website auslesen", key="website_auslesen"):
        if not (url or "").strip():
            st.warning("Bitte zuerst eine Website eintragen.")
        else:
            try:
                with st.spinner("Tonalität der Website wird ausgelesen …"):
                    text = tonalitaet_ermitteln(url)
                st.session_state.website_ausgelesen = {
                    "url": url.strip(), "text": text,
                    "zeit": datetime.datetime.now().isoformat(timespec="seconds")}
                st.success("Tonalität ausgelesen – wird mit dem Profil gespeichert.")
            except Exception as e:
                st.warning(f"Website konnte nicht ausgelesen werden: {e}")
    neu = st.session_state.get("website_ausgelesen")
    if neu and _website_norm(neu["url"]) == _website_norm(url):
        text, zeit, gespeichert = neu["text"], neu["zeit"], False
    elif (profile.get("website_tonalitaet") and _website_norm(profile.get("website")) == _website_norm(url)):
        text, zeit, gespeichert = profile["website_tonalitaet"], profile.get("website_abgerufen") or "", True
    else:
        if (url or "").strip():
            st.caption("Noch nicht ausgelesen. Optional – ohne Website richten sich die Beiträge nach Tonalität, "
                       "Ansprache und Stilbeispiel aus deinem Profil.")
        return
    try:
        stand = datetime.datetime.fromisoformat(zeit).strftime("%d.%m.%Y, %H:%M Uhr")
    except ValueError:
        stand = "unbekannt"
    st.caption(f"Ausgelesen am {stand}{'' if gespeichert else ' (noch nicht gespeichert)'}. Dient nur als grobe "
               "Orientierung für die Sprache der Beiträge; deine Angaben zu Tonalität und Ansprache haben Vorrang.")
    with st.expander("Ausgelesene Tonalität ansehen"):
        st.markdown(text)


def website_nach_speichern(user_id: int, altes_profil: dict, website: str):
    """Übernimmt beim Speichern die per Knopf ausgelesene Tonalität. Kein automatisches Auslesen mehr. Wurde die Website geändert oder entfernt, wird die alte Tonalität gelöscht."""
    website = (website or "").strip()
    neu = st.session_state.pop("website_ausgelesen", None)
    if website and neu and _website_norm(neu["url"]) == _website_norm(website):
        db.save_profile(user_id, website_tonalitaet=neu["text"], website_abgerufen=neu["zeit"])
    elif _website_norm(website) != _website_norm(altes_profil.get("website")):
        db.save_profile(user_id, website_tonalitaet="", website_abgerufen="")


def render_profil_page():
    topbar("Profil", "Diese Angaben werden bei jeder Content-Generierung als Kontext mitgegeben.")

    with st.container(key="pagebody"):
        profile = db.get_profile(st.session_state.user_id)

        with st.form("profil_formular"):
            werte = profil_felder(profile, mit_konten=True)
            if st.form_submit_button("Profil speichern", type="primary"):
                fehler = profil_fehler(werte)
                for f in fehler:
                    st.error(f)
                if not fehler:
                    profil_speichern(st.session_state.user_id, profile, werte)
                    st.session_state.profil_gespeichert = True
                    st.rerun()

        if st.session_state.pop("profil_gespeichert", False):
            st.success("Profil gespeichert.")
        aktuell = db.get_profile(st.session_state.user_id)
        stand = (aktuell.get("profil_aktualisiert") or "")[:10]
        st.caption(f"Zuletzt aktualisiert: {datum_lang(stand)}" if stand else "Noch nie über dieses Formular aktualisiert.")
        zeige_schrift_status(aktuell)


# --- Haupt-Routing -----------------------------------------------------------

if st.session_state.user_id is None:
    render_login()
elif st.session_state.page == "onboarding":
    # Bewusst ohne Sidebar: die einmalige Profil-Abfrage muss abgeschlossen werden,
    # bevor der Rest der App (Dialog, Vorschläge, Kalender) zugänglich ist.
    render_onboarding_page()
else:
    render_sidebar()
    seite = st.session_state.page
    if seite == "chat":
        render_chat_page()
    elif seite == "vorschlaege":
        render_vorschlaege_page()
    elif seite == "kalender":
        render_kalender_page()
    elif seite == "profil":
        render_profil_page()
    else:
        st.session_state.page = "chat"
        st.rerun()