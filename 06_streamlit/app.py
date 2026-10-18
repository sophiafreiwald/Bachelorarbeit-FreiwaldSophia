"""Content-Engine – Streamlit-Frontend.

Architektur:
  Login/Registrierung -> Chat-Dialog (Absichtserkennung via OpenAI) ->
  Vorschläge (3 Varianten, Bildquelle wählen, sofort veröffentlichen
  ODER für den Kalender einplanen) -> Kalender (Planungsübersicht,
  manuelles Veröffentlichen zum geplanten Zeitpunkt).

Persistenz: SQLite über db.py (Nutzer, Unternehmensprofil, geplante Beiträge).
Die Kommunikationstheorie-Logik (Agenda-Setting, AIDA, ELM, Cialdini, ...)
lebt bewusst NICHT hier, sondern in der manuell vorgegebenen Prompt-Architektur
des n8n-Workflows – dieses Frontend bleibt neutral und reicht nur die im
Dialog erkannten Eckdaten weiter.
"""

import base64
import calendar
import concurrent.futures
import datetime
import html
import json
import os
import re

import requests
import streamlit as st
from dotenv import load_dotenv

import db
import werbebild

load_dotenv()

# --- Konfiguration -------------------------------------------------------

WEBHOOK_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://sophiafrw.app.n8n.cloud/webhook/instagram-kampagne",
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "sophiafreiwald/Bachelorarbeit-FreiwaldSophia")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_IMAGE_PATH_PREFIX = "03_pictures"
OPENAI_API_KEY = os.environ.get("API_CHATGPT")
OPENAI_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
# Bildmodelle in Reihenfolge der Präferenz. gpt-image-1-mini ist das schnellste/
# günstigste; ist ein Modell für dein OpenAI-Konto nicht freigeschaltet
# (Organisations-Verifizierung), wird automatisch das nächste versucht und das
# funktionierende für den Rest der Sitzung gemerkt.
OPENAI_IMAGE_MODELS = [m.strip() for m in os.environ.get(
    "OPENAI_IMAGE_MODELS", "gpt-image-1-mini,gpt-image-1,dall-e-3").split(",") if m.strip()]
# "low" ist deutlich schneller und reicht, weil der Text per Code aufs Bild kommt.
# Für hochwertigere Bilder in der .env auf medium oder high setzen.
OPENAI_IMAGE_QUALITY = os.environ.get("OPENAI_IMAGE_QUALITY", "low")
GENERIERTE_BILDER_VERZEICHNIS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "uploads", "generated"
)

db.init_db()

# Prüft, ob app.py und werbebild.py vom selben Stand sind. Häufige Ursache für
# Fehler wie "unexpected keyword argument": eine alte werbebild.py im Ordner
# oder ein Streamlit-Server, der das alte Modul noch im Speicher hat.
WERBEBILD_VERSION_ERWARTET = 4
if getattr(werbebild, "VERSION", 0) != WERBEBILD_VERSION_ERWARTET:
    st.error(
        f"werbebild.py ist veraltet (Version {getattr(werbebild, 'VERSION', 'alt')}, erwartet "
        f"{WERBEBILD_VERSION_ERWARTET}). Bitte die neue werbebild.py in den Ordner "
        f"{os.path.dirname(os.path.abspath(werbebild.__file__))} legen und die App im Terminal "
        "mit Strg+C beenden und neu starten (streamlit run app.py)."
    )
    st.stop()

st.set_page_config(page_title="ContentEngine", layout="wide", initial_sidebar_state="expanded")

TONE_OPTIONEN = ["Professionell", "Locker", "Humorvoll", "Emotional", "Sachlich"]
ZIEL_OPTIONEN = ["Bekanntheit", "Image", "Kaufabsicht"]
SEGMENT_OPTIONEN = ["Stammkunden", "Neukunden"]
ANREDE_OPTIONEN = ["Sie", "Du"]
CONTENT_TYPE_LABELS = {
    "idee": "Nur Idee",
    "bild": "Nur Bild",
    "caption": "Nur Caption",
    "beides": "Bild + Caption",
}
# Plattform-Spezifikationen (Vorgaben laut Recherche, siehe Bachelorarbeit).
# primaertext = Caption, ueberschrift = Headline auf dem Bild, None = keine Vorgabe.
# Videoformate/Längen sind dokumentiert, der Prototyp erzeugt aber Bildbeiträge.
PLATTFORMEN = {"instagram": "Instagram", "facebook": "Facebook"}
BEITRAGSARTEN = {"feed": "Feed", "reels": "Reels", "story": "Story"}
PLATTFORM_SPEZIFIKA = {
    "ig_feed": {"plattform": "Instagram", "art": "Feed", "verhaeltnis": "4:5", "aufloesung": "1.440 × 1.800 px",
                "laenge": "1 Sek. – 60 Min.", "primaertext_min": None, "primaertext_max": 125,
                "ueberschrift_max": 40, "hashtags_max": 30},
    "ig_reels": {"plattform": "Instagram", "art": "Reels", "verhaeltnis": "9:16", "aufloesung": "1.440 × 2.560 px",
                 "laenge": "0 Sek. – 15 Min.", "primaertext_min": None, "primaertext_max": 44,
                 "ueberschrift_max": None, "hashtags_max": None},
    "ig_story": {"plattform": "Instagram", "art": "Story", "verhaeltnis": "9:16", "aufloesung": "1.440 × 2.560 px",
                 "laenge": "5 – 16 Sek.", "primaertext_min": None, "primaertext_max": 125,
                 "ueberschrift_max": None, "hashtags_max": None},
    "fb_feed": {"plattform": "Facebook", "art": "Feed", "verhaeltnis": "4:5", "aufloesung": "1.440 × 1.800 px",
                "laenge": "1 Sek. – 241 Min.", "primaertext_min": 50, "primaertext_max": 150,
                "ueberschrift_max": 27, "hashtags_max": None},
    "fb_reels": {"plattform": "Facebook", "art": "Reels", "verhaeltnis": "9:16", "aufloesung": "1.440 × 2.560 px",
                 "laenge": "keine Beschränkung", "primaertext_min": None, "primaertext_max": 40,
                 "ueberschrift_max": 55, "hashtags_max": None},
    "fb_story": {"plattform": "Facebook", "art": "Story", "verhaeltnis": "9:16", "aufloesung": "1.440 × 2.560 px",
                 "laenge": "1 Sek. – 3 Min.", "primaertext_min": None, "primaertext_max": 125,
                 "ueberschrift_max": 40, "hashtags_max": None},
}
DATEITYPEN = "JPG, PNG, MP4, MOV, GIF"


def vorgaben_zusammenfassung(spec: dict) -> list:
    teile = [f"Verhältnis {spec['verhaeltnis']}", spec["aufloesung"], f"Dateityp {DATEITYPEN}",
             f"Länge {spec['laenge']}"]
    if spec["primaertext_min"]:
        teile.append(f"Primärtext {spec['primaertext_min']}–{spec['primaertext_max']} Zeichen")
    else:
        teile.append(f"Primärtext max. {spec['primaertext_max']} Zeichen")
    if spec["ueberschrift_max"]:
        teile.append(f"Überschrift max. {spec['ueberschrift_max']} Zeichen")
    if spec["hashtags_max"]:
        teile.append(f"max. {spec['hashtags_max']} Hashtags")
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


def plattform_vorgaben(key: str) -> dict:
    """Die Vorgaben, die an n8n gehen, damit die Texte die Limits einhalten."""
    spec = PLATTFORM_SPEZIFIKA.get(key, PLATTFORM_SPEZIFIKA["ig_feed"])
    return {k: spec[k] for k in ("plattform", "art", "verhaeltnis", "primaertext_min",
                                 "primaertext_max", "ueberschrift_max", "hashtags_max")}


def bild_erforderlich(content_type: str) -> bool:
    return content_type in ("bild", "beides")
NAV_ITEMS = [("chat", "Dialog"), ("vorschlaege", "Vorschläge"), ("kalender", "Kalender"), ("profil", "Profil")]
LOGO_VERZEICHNIS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "logos")

# --- Design-Tokens & globales CSS (1:1 an das Mockup angelehnt) ----------

CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#FFFFFF; --bg-soft:#F6F5F3; --ink:#1A1816; --ink-soft:#6F6A63;
    --line:#E8E5E0; --accent:#C9532E; --accent-soft:#F6E2D8;
    --sidebar-bg:#171615; --sidebar-text:#EDEAE5; --sidebar-dim:#9A958D;
    --success:#2F6F52; --success-soft:#E1EEE6;
  }
  html, body, [class*="css"] { font-family: 'Work Sans', sans-serif; }
  h1,h2,h3,h4 { font-family: 'Space Grotesk', sans-serif; color: var(--ink); }
  /* NICHT den ganzen Header verstecken – darin liegt der Auf-/Zuklapp-Button
     der Sidebar. Nur das Streamlit-eigene Hamburger-Menü/Deploy-Button weg,
     der Header selbst bleibt (transparent, schmal) klickbar. */
  #MainMenu, [data-testid="stToolbarActions"], footer { visibility: hidden; height: 0; }
  header[data-testid="stHeader"] { background: transparent; height: 2.5rem; }
  .block-container { padding: 0 !important; max-width: 100% !important; }
  div[data-testid="stAppViewContainer"] > .main { background: var(--bg-soft); }

  /* --- Sidebar: dunkel, Akzentfarbe, wie im Mockup ------------------- */
  section[data-testid="stSidebar"] {
    background: var(--sidebar-bg) !important; width: 260px !important; min-width: 260px !important;
  }
  section[data-testid="stSidebar"] > div { padding-top: 28px; }
  section[data-testid="stSidebar"] * { color: var(--sidebar-text); }
  section[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.08); }

  .ce-logo { display:flex; align-items:center; gap:10px; padding: 0 20px 24px; }
  .ce-logo-badge { width:26px; height:26px; border-radius:8px; background:var(--accent);
                   display:flex; align-items:center; justify-content:center; flex:0 0 auto; }
  .ce-logo span { font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:18px; color:#fff; }

  section[data-testid="stSidebar"] div.stButton { padding: 0 12px; }
  section[data-testid="stSidebar"] div.stButton > button {
    width:100%; text-align:left; justify-content:flex-start; border:none !important;
    border-radius:10px; font-size:14px; padding:10px 12px; box-shadow:none;
  }
  section[data-testid="stSidebar"] div.stButton > button[kind="secondary"] {
    background:transparent !important; color: var(--sidebar-dim) !important; font-weight:500;
  }
  section[data-testid="stSidebar"] div.stButton > button[kind="primary"] {
    background:rgba(255,255,255,0.08) !important; color:#fff !important; font-weight:600;
  }
  section[data-testid="stSidebar"] div.stButton > button:hover {
    background:rgba(255,255,255,0.12) !important; color:#fff !important;
  }
  .ce-avatar-row { display:flex; align-items:center; gap:10px; padding:16px 20px 4px;
                   border-top:1px solid rgba(255,255,255,0.08); margin-top:12px; }
  .ce-avatar-circle { width:32px; height:32px; border-radius:50%; background:var(--accent);
                      display:flex; align-items:center; justify-content:center; font-size:13px;
                      font-weight:700; color:#fff; flex:0 0 auto; }
  .ce-avatar-name { font-size:13px; font-weight:600; color:#fff; }
  .ce-avatar-sub { color:var(--sidebar-dim); font-size:12px; }
  section[data-testid="stSidebar"] .st-key-sidebar_logout button {
    background:transparent !important; color:var(--sidebar-dim) !important; font-size:12px !important;
    font-weight:500 !important; padding:4px 12px !important; text-decoration:underline;
  }

  /* --- Haupt-Topbar (weißer Kopfbereich je Seite) -------------------- */
  .st-key-topbar { background:#fff; border-bottom:1px solid var(--line); padding:24px 40px; }
  .st-key-topbar h1 { font-size:20px; margin:0; }
  .st-key-topbar p { margin:4px 0 0; font-size:13px; color:var(--ink-soft); }
  .st-key-pagebody { padding:32px 40px; }

  /* --- Buttons allgemein ---------------------------------------------- */
  div.stButton > button[kind="primary"] {
    background: var(--accent); border-color: var(--accent); color:#fff; border-radius:9px;
    font-weight:600; font-size:14px;
  }
  div.stButton > button[kind="primary"]:hover { background:#a8431f; border-color:#a8431f; }
  div.stButton > button[kind="secondary"] {
    background:#fff; border:1px solid var(--line); border-radius:9px; color:var(--ink);
    font-weight:600; font-size:13px;
  }
  div.stButton > button[kind="secondary"]:hover { border-color:var(--accent); color:var(--accent); }

  /* Chat-Chips: pillenförmig statt rechteckig */
  .st-key-chat_chips div.stButton > button { border-radius:999px !important; font-size:12px !important;
    padding:6px 14px !important; font-weight:600 !important; }
  .st-key-chat_chips div.stButton > button[kind="secondary"] { color: var(--ink-soft) !important; }

  /* Chat-Bubbles */
  .ce-bubble-row { display:flex; gap:12px; align-items:flex-start; margin-bottom:16px; }
  .ce-bubble-row.user { flex-direction:row-reverse; }
  .ce-avatar-mini { width:30px; height:30px; border-radius:50%; flex:0 0 auto; display:flex;
    align-items:center; justify-content:center; font-size:12px; font-weight:700; color:#fff; }
  .ce-avatar-mini.assistant { background:var(--accent); }
  .ce-avatar-mini.user { background:#2D2B28; }
  .ce-bubble { padding:14px 18px; font-size:14px; line-height:1.6; border-radius:14px; }
  .ce-bubble.assistant { background:#fff; border:1px solid var(--line); border-top-left-radius:4px; max-width:560px; }
  .ce-bubble.user { background:var(--accent-soft); border-top-right-radius:4px; max-width:420px; }

  /* Vorschläge-Karten */
  .ce-card-image { aspect-ratio:1/1; background:var(--accent-soft); border-radius:12px 12px 0 0;
    display:flex; align-items:center; justify-content:center; position:relative; margin:-1rem -1rem 12px -1rem; }
  .ce-score-badge { position:absolute; top:10px; left:10px; background:#fff; border-radius:8px;
    padding:4px 9px; font-size:12px; font-weight:700; color:var(--success); }

  /* Kalender */
  .ce-day { border:1px solid var(--line); border-radius:10px; padding:6px; min-height:72px; background:#fff; }
  .ce-daynum { font-size:11px; font-weight:600; color: var(--ink-soft); }
  .ce-today { border-color: var(--accent) !important; box-shadow: 0 0 0 1px var(--accent); }
  .ce-chip { background: var(--accent-soft); color:#8A3417; border-radius:6px;
             padding:3px 6px; font-size:10px; font-weight:600; display:block; margin-top:3px; }

  /* Login */
  .ce-login-brand { background:var(--accent); color:#fff; border-radius:16px; padding:48px;
    min-height:560px; display:flex; flex-direction:column; justify-content:space-between; }
  .ce-login-brand h1 { color:#fff; font-size:32px; line-height:1.2; margin:0; }
  .ce-login-brand p { color:#FBE7DC; font-size:15px; line-height:1.6; }
  .ce-login-feature { display:flex; gap:10px; align-items:center; font-size:14px; margin-bottom:10px; }
  .st-key-login_card { background:#fff; border:1px solid var(--line); border-radius:16px; padding:36px; max-width:380px; }
</style>
"""
st.html(CSS)

# --- Session-Defaults ------------------------------------------------------

if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "page" not in st.session_state:
    st.session_state.page = "chat"
if "nav_zurueck" not in st.session_state:
    st.session_state.nav_zurueck = []
if "nav_vor" not in st.session_state:
    st.session_state.nav_vor = []
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


def render_instagram_simulation(unternehmensname: str, caption: str, bild_bytes=None, image_url: str = "",
                                plattform_format: str = ""):
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
        if bild_bytes:
            st.image(bild_bytes, width='stretch')
        elif image_url and (image_url.startswith("http") or os.path.exists(image_url)):
            st.image(image_url, width='stretch')
        else:
            st.html(
                "<div style='aspect-ratio:1/1;background:var(--accent-soft);border-radius:8px;"
                "display:flex;align-items:center;justify-content:center;color:var(--accent);"
                "font-size:13px;'>KI-Bild (wird im echten Betrieb von DALL·E generiert)</div>"
            )
        st.caption(caption)


# --- Hilfsfunktionen: GitHub-Bildupload & n8n-Aufruf -----------------------

def upload_image_to_github(file_bytes: bytes, filename: str) -> str:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN ist nicht gesetzt (siehe .env).")

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "bild.jpg"
    path = f"{GITHUB_IMAGE_PATH_PREFIX}/{timestamp}_{safe_name}"

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    payload = {
        "message": f"Upload Kampagnenbild {timestamp}",
        "content": base64.b64encode(file_bytes).decode(),
        "branch": GITHUB_BRANCH,
    }
    response = requests.put(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{path}"


def call_n8n(payload: dict) -> dict:
    response = requests.post(WEBHOOK_URL, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    return data


def generiere_vorschlaege(profile: dict, context: dict, anzahl: int = 3, bisherige_aspekte=None) -> dict:
    import random

    payload = {
        "thema": context.get("thema", ""),
        "kommunikationsziel": context.get("kommunikationsziel", "Bekanntheit"),
        "zielgruppensegment": context.get("zielgruppensegment", "Stammkunden"),
        "content_type": context.get("content_type", "beides"),
        "plattform_format": context.get("plattform_format", "ig_feed"),
        "plattform_vorgaben": plattform_vorgaben(context.get("plattform_format", "ig_feed")),
        "angebot": context.get("angebot", ""),
        "personen_im_bild": context.get("personen_im_bild"),
        "anzahl_posts": anzahl,
        "bisherige_aspekte": bisherige_aspekte or [],
        "varianten_seed": random.randint(1, 1_000_000),
        "publish_to_instagram": False,
        "selected_post": "",
        "image_url": "",
        "instagram_account_id": profile.get("instagram_account_id", ""),
        "unternehmensname": profile.get("unternehmensname", ""),
        "standort": profile.get("standort", ""),
        "branche": profile.get("branche", ""),
        "tone_of_voice": profile.get("tone_of_voice", "Professionell"),
    }
    return call_n8n(payload)


def setze_vorschlaege(data):
    """Neue Vorschläge speichern und alte, KI-generierte Bilder der vorherigen
    Runde verwerfen – sonst hängt Bild 1 der alten Runde an Post 1 der neuen."""
    st.session_state.vorschlaege = data
    # Auch Eingabefelder (Caption, Headline, CTA, Upload) der alten Runde verwerfen –
    # Streamlit-Widgets mit festem key würden sonst die ALTEN Texte weiter anzeigen.
    praefixe = ("ki_bild_", "ki_prompt_", "ki_fehler_", "caption_", "headline_", "cta_",
                "textbild_", "angebot_", "bild_", "bildquelle_", "aktion_", "datum_", "zeit_")
    for key in [k for k in st.session_state.keys() if str(k).startswith(praefixe)]:
        del st.session_state[key]


# --- OpenAI: Bildgenerierung ------------------------------------------------
#
# Vorher wurde in der App nirgends ein Bild erzeugt – es wurde nur post["image_url"]
# aus der n8n-Antwort angezeigt. Liefert n8n dieses Feld nicht (in content_engine.db
# haben alle "ki"-Posts eine leere image_url), bleibt der Platzhalter stehen.
# Jetzt erzeugt die App das Bild selbst mit dem OpenAI-Key aus der .env.

PLATTFORM_BILDGROESSE = {
    # Hochformat für alle Formate (4:5 und 9:16); (gpt-image-*, dall-e-3).
    # Das Ergebnis wird in werbebild.py exakt auf 1440×1800 bzw. 1440×2560 zugeschnitten.
    k: ("1024x1536", "1024x1792") for k in ("ig_feed", "ig_reels", "ig_story", "fb_feed", "fb_reels", "fb_story")
}

# Fallback-CTAs je Kommunikationsziel, falls n8n (noch) kein Feld "cta" liefert.
# Die eigentliche, theoriegeleitete CTA-Formulierung (AIDA: Action) soll aus der
# Prompt-Architektur in n8n kommen.
STANDARD_CTA = {
    "Bekanntheit": "Jetzt folgen",
    "Image": "Mehr erfahren",
    "Kaufabsicht": "Jetzt vorbeikommen",
}


def standard_headline(post: dict, context: dict = None) -> str:
    """n8n-Headline > Angebot (bei Aktionen muss das Angebot sofort erkennbar sein) > Aspekt."""
    angebot = ((context or {}).get("angebot") or "").strip()
    return (post.get("headline") or angebot or post.get("aspekt") or "").strip()


def standard_cta(post: dict, context: dict) -> str:
    return (post.get("cta") or STANDARD_CTA.get(context.get("kommunikationsziel"), "")).strip()


def baue_bild_prompt(profile: dict, context: dict, post: dict) -> str:
    """Bild-Prompt: bevorzugt der theoriebasierte 'bild_prompt' aus n8n; ohne ihn ein
    Fallback aus Profil + Kontext. Technische Vorgaben werden immer angehängt."""
    inhalt = (post.get("bild_prompt") or "").strip()
    if not inhalt:
        teile = [
            "Hochwertiges, fotorealistisches Werbefoto für Social Media.",
            f"Unternehmen: {profile.get('unternehmensname') or 'kleines Unternehmen'}"
            + (f", Branche: {profile['branche']}" if profile.get("branche") else "")
            + (f", Standort: {profile['standort']}" if profile.get("standort") else "") + ".",
            f"Kampagnenthema: {context.get('thema', '')}.",
            f"Konkreter Aspekt: {post.get('aspekt', '')}.",
            f"Bildstimmung passend zum Tone of Voice: {profile.get('tone_of_voice', 'Professionell')}.",
        ]
        if profile.get("strategie"):
            teile.append(f"Positionierung der Marke: {profile['strategie']}.")
        if profile.get("ci_farbe"):
            teile.append(f"Markenfarbe {profile['ci_farbe']} dezent in Farbgebung oder Requisiten aufgreifen.")
        inhalt = " ".join(teile)

    personen = context.get("personen_im_bild")
    technik = []
    if personen is True:
        technik.append("Menschen sind zu sehen, natürlich und authentisch.")
    elif personen is False:
        technik.append("Es sind KEINE Menschen zu sehen.")
    technik.append(
        "Hauptmotiv in der oberen Bildhälfte; unteres Drittel ruhig und detailarm lassen, "
        "dort wird später Werbetext eingeblendet."
    )
    technik.append("Absolut kein Text, keine Buchstaben, keine Logos, keine Wasserzeichen im Bild.")
    return inhalt + " " + " ".join(technik)


def _bild_anfrage(client, modell: str, prompt: str, plattform_format: str) -> bytes:
    groesse_neu, groesse_dalle = PLATTFORM_BILDGROESSE.get(plattform_format, ("1024x1536", "1024x1792"))
    if modell.startswith("dall-e"):
        result = client.images.generate(
            model=modell, prompt=prompt[:3900], size=groesse_dalle, n=1, response_format="b64_json",
        )
    else:
        # gpt-image-Modelle liefern immer b64_json; quality low/medium/high
        result = client.images.generate(
            model=modell, prompt=prompt, size=groesse_neu, n=1, quality=OPENAI_IMAGE_QUALITY,
        )
    bild = result.data[0]
    if getattr(bild, "b64_json", None):
        return base64.b64decode(bild.b64_json)
    if getattr(bild, "url", None):
        r = requests.get(bild.url, timeout=60)
        r.raise_for_status()
        return r.content
    raise RuntimeError("Die API hat weder Bilddaten noch eine URL zurückgegeben.")


def _modell_reihenfolge() -> list:
    gemerkt = st.session_state.get("bildmodell_ok")
    return [gemerkt] + [m for m in OPENAI_IMAGE_MODELS if m != gemerkt] if gemerkt else list(OPENAI_IMAGE_MODELS)


def generiere_ki_bild(prompt: str, plattform_format: str = "ig_feed", client=None, modelle=None):
    """Erzeugt EIN Bild. Gibt (bild_bytes, modell) zurück.

    Bewusst Bytes statt URL: OpenAI-Bild-URLs laufen nach ca. 1 Stunde ab.
    Thread-sicher (greift nicht auf st.session_state zu, wenn client/modelle übergeben werden).
    """
    client = client or _get_openai_client()
    modelle = modelle or _modell_reihenfolge()
    fehler = []
    for modell in modelle:
        try:
            return _bild_anfrage(client, modell, prompt, plattform_format), modell
        except Exception as e:  # nächstes Modell versuchen
            fehler.append(f"{modell}: {e}")
    raise RuntimeError("Bildgenerierung fehlgeschlagen – " + " | ".join(fehler))


def generiere_bilder_parallel(profile: dict, context: dict, posts: list) -> None:
    """Erzeugt die KI-Bilder für ALLE Posts gleichzeitig (eine Anfrage pro Post,
    parallel in Threads). Die Wartezeit entspricht damit etwa EINEM Bild statt drei.
    Ergebnisse landen in st.session_state (nur im Haupt-Thread geschrieben)."""
    client = _get_openai_client()
    modelle = _modell_reihenfolge()
    plattform = context.get("plattform_format", "ig_feed")
    prompts = {i: baue_bild_prompt(profile, context, p) for i, p in enumerate(posts)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(posts))) as pool:
        futures = {pool.submit(generiere_ki_bild, prompts[i], plattform, client, modelle): i for i in prompts}
        for f in concurrent.futures.as_completed(futures):
            i = futures[f]
            st.session_state[f"ki_prompt_{i}"] = prompts[i]
            try:
                bild, modell = f.result()
                st.session_state[f"ki_bild_{i}"] = bild
                st.session_state["bildmodell_ok"] = modell
                st.session_state.pop(f"ki_fehler_{i}", None)
            except Exception as e:
                st.session_state[f"ki_fehler_{i}"] = str(e)


# --- Zeichenlimits der Plattformen DURCHSETZEN -------------------------------
#
# n8n bekommt die Limits zwar mitgeschickt, Sprachmodelle zählen Zeichen aber
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


def text_auf_limit_bringen(client, text: str, minimum, maximum, art: str, hashtags_max=None) -> str:
    """Bringt einen Text in die Zeichenvorgabe. Thread-sicher (kein st.session_state)."""
    text = begrenze_hashtags((text or "").strip(), hashtags_max)
    if _passt(text, minimum, maximum) or client is None:
        return hart_kuerzen(text, maximum)
    bereich = (f"zwischen {minimum} und {maximum}" if minimum and maximum else
               f"höchstens {maximum}" if maximum else f"mindestens {minimum}")
    anweisung = (
        f"Formuliere diesen {art} so um, dass er {bereich} Zeichen hat (inklusive Leerzeichen, "
        f"Emojis und Hashtags). Kernbotschaft, Tonalität und Handlungsaufforderung beibehalten"
        + (f", höchstens {hashtags_max} Hashtags" if hashtags_max else "")
        + ". Antworte ausschließlich mit dem neuen Text, ohne Anführungszeichen.\n\nText:\n" + text
    )
    messages = [{"role": "system", "content": "Du bist Social-Media-Texter und hältst Zeichenlimits exakt ein."},
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
                     {"role": "user", "content": f"Dein Text hat {len(kandidat)} Zeichen. Erlaubt sind {bereich}. "
                                                 "Bitte korrigieren, nur den Text ausgeben."}]
    return hart_kuerzen(kandidat if len(kandidat) >= len(text) * 0.3 else text, maximum)


def passe_post_an_vorgaben(client, post: dict, spec: dict, context: dict) -> dict:
    """Gibt Caption und Headline zurück, die garantiert in die Plattformvorgaben passen."""
    caption = text_auf_limit_bringen(
        client, post.get("full_caption", ""), spec.get("primaertext_min"), spec.get("primaertext_max"),
        "Primärtext (Caption)", spec.get("hashtags_max"),
    )
    headline = text_auf_limit_bringen(
        client, standard_headline(post, context), None, spec.get("ueberschrift_max"), "Überschrift",
    )
    return {"full_caption": caption, "headline": headline}


def erzeuge_vorschlaege_komplett(profile: dict, context: dict, bisherige_aspekte=None) -> None:
    """Texte (n8n) holen, dann GLEICHZEITIG: Texte auf die Plattformlimits bringen und
    alle Bilder erzeugen – die Vorschläge erscheinen erst vollständig."""
    with st.spinner("Erstelle Beitragstexte …"):
        data = generiere_vorschlaege(profile, context, anzahl=3, bisherige_aspekte=bisherige_aspekte)
    setze_vorschlaege(data)
    posts = data.get("posts", []) if isinstance(data, dict) else []
    if not posts:
        return
    spec = PLATTFORM_SPEZIFIKA.get(context.get("plattform_format"), PLATTFORM_SPEZIFIKA["ig_feed"])
    client = _get_openai_client() if OPENAI_API_KEY else None
    mit_bildern = bild_erforderlich(context.get("content_type")) and client is not None
    text = f"Passe Texte an die Vorgaben an{' und erstelle ' + str(len(posts)) + ' Bilder' if mit_bildern else ''} …"
    with st.spinner(text):
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(posts))) as pool:
            text_jobs = {pool.submit(passe_post_an_vorgaben, client, p, spec, context): i
                         for i, p in enumerate(posts)}
            if mit_bildern:  # läuft parallel zu den Text-Jobs
                generiere_bilder_parallel(profile, context, posts)
            for f in concurrent.futures.as_completed(text_jobs):
                post = posts[text_jobs[f]]
                try:
                    angepasst = f.result()
                except Exception:
                    angepasst = {"full_caption": hart_kuerzen(post.get("full_caption", ""), spec["primaertext_max"]),
                                 "headline": hart_kuerzen(standard_headline(post, context), spec["ueberschrift_max"])}
                if angepasst["full_caption"] != post.get("full_caption", ""):
                    post["original_caption"] = post.get("full_caption", "")
                if angepasst["headline"] != standard_headline(post, context):
                    post["original_headline"] = standard_headline(post, context)
                post.update(angepasst)


def speichere_generiertes_bild(bild_bytes: bytes, user_id: int) -> str:
    """Persistiert ein fertiges Werbebild für den Kalender: bevorzugt auf GitHub
    (öffentliche URL, später für die echte Instagram-API nötig), sonst lokal."""
    dateiname = f"post_{user_id}_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.jpg"
    if GITHUB_TOKEN:
        try:
            return upload_image_to_github(bild_bytes, dateiname)
        except Exception:
            pass  # Fallback: lokal speichern
    os.makedirs(GENERIERTE_BILDER_VERZEICHNIS, exist_ok=True)
    pfad = os.path.join(GENERIERTE_BILDER_VERZEICHNIS, dateiname)
    with open(pfad, "wb") as f:
        f.write(bild_bytes)
    return pfad


def formatiere_chat_text(text: str) -> str:
    """Escaped Chat-Text für st.html und übernimmt einfache Markdown-Auszeichnung
    (**fett**, Zeilenumbrüche), die GPT gerne verwendet."""
    sicher = html.escape(text or "")
    sicher = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", sicher)
    return sicher.replace("\n", "<br>")


# --- OpenAI: Absichtserkennung im Chat-Dialog -----------------------------

UPDATE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "update_content_request",
            "description": (
                "Erfasst bzw. aktualisiert die strukturierten Eckdaten der Content-Anfrage, "
                "sobald sie aus dem Dialog erkennbar sind. Immer mit dem aktuellen Gesamtstand "
                "aufrufen, auch wenn nur ein Feld neu hinzukommt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thema": {"type": "string", "description": "Konkretes Kampagnenthema"},
                    "kommunikationsziel": {"type": "string", "enum": ZIEL_OPTIONEN},
                    "zielgruppensegment": {"type": "string", "enum": SEGMENT_OPTIONEN},
                    "angebot": {
                        "type": "string",
                        "description": (
                            "Konkretes Angebot bzw. Aktion, falls genannt – wörtlich mit Zahlen, z. B. "
                            "'10 % Rabatt auf Brezeln', '2 Brezeln für 1,50 €', 'Gratis Kaffee zum Frühstück'. "
                            "Leer lassen, wenn es keine Aktion gibt."
                        ),
                    },
                    "bereit_zur_generierung": {
                        "type": "boolean",
                        "description": (
                            "true, sobald thema, kommunikationsziel und zielgruppensegment "
                            "ausreichend klar sind."
                        ),
                    },
                },
                "required": ["bereit_zur_generierung"],
            },
        },
    }
]

def build_system_prompt(profile: dict) -> str:
    anrede = profile.get("anrede", "Sie")
    anrede_hinweis = (
        "Duze den Nutzer in deinen Antworten (informelle Ansprache)."
        if anrede == "Du" else
        "Sieze den Nutzer in deinen Antworten (formelle Ansprache)."
    )
    profil_kontext = []
    if profile.get("branche"):
        profil_kontext.append(f"Branche: {profile['branche']}")
    if profile.get("zielgruppe"):
        profil_kontext.append(f"Zielgruppe laut Unternehmensprofil: {profile['zielgruppe']}")
    if profile.get("strategie"):
        profil_kontext.append(f"Positionierung: {profile['strategie']}")
    kontext_zeile = (" Bekannter Unternehmenskontext – " + "; ".join(profil_kontext) + ".") if profil_kontext else ""

    return (
        "Du bist der Dialog-Assistent einer Content-Engine für ressourcenknappe KMU. "
        "Content-Typ, Plattform-Format und die Frage nach Personen im Bild werden NICHT von dir "
        "im Dialog erfragt – das läuft bereits über Auswahl-Buttons in der Oberfläche, bevor du "
        "diese Nachricht siehst. Du klärst in wenigen, kurzen Rückfragen (auf Deutsch, freundlich, "
        "prägnant) nur noch folgende drei Eckdaten für den Beitrag: "
        "(1) thema, (2) kommunikationsziel (Bekanntheit, Image oder Kaufabsicht), "
        "(3) zielgruppensegment (Stammkunden oder Neukunden – welches Segment DIESER Beitrag "
        "konkret ansprechen soll). Nennt der Nutzer eine konkrete Aktion (Rabatt, Preis, Gratis-Angebot, "
        "Zeitraum), erfasse sie zusätzlich wörtlich im Feld angebot – frage aber nicht aktiv danach, "
        "wenn das Ziel nicht Kaufabsicht ist. "
        + kontext_zeile + " " + anrede_hinweis + " "
        "Frage immer nur nach dem, was noch fehlt – wiederhole keine bereits beantworteten Punkte "
        "und frage nicht erneut nach der allgemeinen Zielgruppe, wenn sie oben schon bekannt ist. "
        "Wenn der Nutzer nach aktuellen Trends oder Ideen für Social-Media-Content fragt, nenne "
        "2-3 branchenübliche Content-Trends oder Themen-Ideen (basierend auf deinem allgemeinen "
        "Wissen, nicht auf einer Live-Recherche – weise kurz darauf hin, dass es sich um "
        "allgemeine Einschätzungen und nicht um tagesaktuelle Trendsuche handelt) und frage dann, "
        "ob eine davon als Thema übernommen werden soll. "
        "Rufe nach JEDER Nutzernachricht das Tool update_content_request mit dem aktuellen "
        "Gesamtstand auf. Sobald thema, kommunikationsziel und zielgruppensegment klar sind, "
        "setze bereit_zur_generierung=true und bestätige dem Nutzer kurz, dass die Angaben vollständig sind."
    )


def _get_openai_client():
    from openai import OpenAI

    if not OPENAI_API_KEY:
        raise RuntimeError("API_CHATGPT ist nicht gesetzt (siehe .env).")
    return OpenAI(api_key=OPENAI_API_KEY)


def chat_turn(user_text: str):
    """Verarbeitet eine Nutzer-Nachricht: aktualisiert chat_context, liefert Assistant-Antwort."""
    client = _get_openai_client()
    profile = db.get_profile(st.session_state.user_id)

    messages = [{"role": "system", "content": build_system_prompt(profile)}]
    for m in st.session_state.chat_history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_text})

    # Tool-Aufruf ERZWINGEN statt "auto": Bei "auto" lässt das Modell den Aufruf
    # gelegentlich weg -> dialog_bereit wird nie gesetzt -> es werden keine
    # Vorschläge erzeugt, ohne dass eine Fehlermeldung erscheint.
    completion = client.chat.completions.create(
        model=OPENAI_MODEL, messages=messages, tools=UPDATE_TOOL,
        tool_choice={"type": "function", "function": {"name": "update_content_request"}},
    )
    msg = completion.choices[0].message

    if msg.tool_calls:
        tool_call = msg.tool_calls[0]
        try:
            args = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, TypeError):
            args = {}
        for k, v in args.items():
            if v is None:
                continue
            if k == "bereit_zur_generierung":
                st.session_state.chat_context["dialog_bereit"] = v
            else:
                st.session_state.chat_context[k] = v

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{
                "id": tool_call.id, "type": "function",
                "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
            }],
        })
        messages.append({
            "role": "tool", "tool_call_id": tool_call.id,
            "content": json.dumps({"status": "ok", "context": st.session_state.chat_context}),
        })

        followup = client.chat.completions.create(model=OPENAI_MODEL, messages=messages)
        reply_text = followup.choices[0].message.content or ""
    else:
        reply_text = msg.content or ""

    return reply_text


def sende_chat_nachricht(text: str):
    text = text.strip()
    if not text:
        return
    st.session_state.chat_history.append({"role": "user", "content": text})
    try:
        reply = chat_turn(text)
    except Exception as e:
        reply = f"⚠️ Die Anfrage an die KI ist fehlgeschlagen: {e}"
    st.session_state.chat_history.append({"role": "assistant", "content": reply})
    pruefe_und_generiere()


def chip_antwort(user_text: str, assistant_text: str):
    """Für reine Button-Auswahlen (Format, Personen im Bild): keine GPT-Kosten,
    nur eine feste Bestätigung im Chat-Verlauf, danach Bereitschaft prüfen."""
    st.session_state.chat_history.append({"role": "user", "content": user_text})
    st.session_state.chat_history.append({"role": "assistant", "content": assistant_text})
    pruefe_und_generiere()


def fehlende_angaben() -> list:
    """Was fehlt noch bis zur Generierung? (für den Status-Hinweis im Chat)"""
    ctx = st.session_state.chat_context
    fehlt = []
    if not ctx.get("content_type"):
        fehlt.append("Art des Inhalts")
    if not ctx.get("plattform_format"):
        fehlt.append("Format")
    if bild_erforderlich(ctx.get("content_type")) and ctx.get("personen_im_bild") is None:
        fehlt.append("Personen im Bild ja/nein")
    for feld, label in (("thema", "Thema"), ("kommunikationsziel", "Kommunikationsziel"),
                        ("zielgruppensegment", "Stamm- oder Neukunden")):
        if not ctx.get(feld):
            fehlt.append(label)
    return fehlt


def pruefe_und_generiere(erzwingen: bool = False):
    """Löst die Vorschlags-Generierung aus, sobald alle nötigen Angaben vorliegen:
    Dialog (thema/kommunikationsziel/zielgruppensegment), content_type, plattform_format
    und – falls ein Bild gebraucht wird – personen_im_bild.
    erzwingen=True: manueller Start per Button, auch wenn das Modell
    'bereit_zur_generierung' nicht gesetzt hat."""
    ctx = st.session_state.chat_context
    if not ctx.get("dialog_bereit") and not erzwingen:
        return False
    if not ctx.get("content_type") or not ctx.get("plattform_format"):
        return False
    if bild_erforderlich(ctx.get("content_type")) and ctx.get("personen_im_bild") is None:
        return False

    profile = db.get_profile(st.session_state.user_id)
    bisherige_aspekte = []
    vorherige = st.session_state.get("vorschlaege")
    if vorherige and isinstance(vorherige, dict):
        bisherige_aspekte = [p.get("aspekt", "") for p in vorherige.get("posts", []) if p.get("aspekt")]

    try:
        erzeuge_vorschlaege_komplett(profile, ctx, bisherige_aspekte)
        navigiere("vorschlaege")
    except Exception as e:  # vorher nur RequestException – z. B. JSON-Fehler gingen verloren
        detail = ""
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            detail = f" – Antwort von n8n: {e.response.text[:300]}"
        st.session_state.chat_history.append(
            {"role": "assistant", "content": f"⚠️ Generierung fehlgeschlagen: {e}{detail}"}
        )
        return False
    ctx["dialog_bereit"] = False
    return True


# --- Login / Registrierung -------------------------------------------------

def render_login():
    st.html('<div style="height:48px"></div>')
    col_brand, col_spacer, col_form = st.columns([5, 1, 4])

    with col_brand:
        st.html(
            """
            <div class="ce-login-brand">
              <div style="display:flex; align-items:center; gap:10px;">
                <div style="width:28px;height:28px;border-radius:8px;background:#fff;display:flex;
                            align-items:center;justify-content:center;">
                  <svg width="16" height="16" viewBox="0 0 28 28" fill="none">
                    <path d="M8 20 L14 8 L20 20" stroke="#C9532E" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </div>
                <span style="font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:20px;">ContentEngine</span>
              </div>
              <div>
                <h1>Social-Media-Content, der zu deinem Unternehmen passt.</h1>
                <p style="margin-top:20px;">Im Dialog Ideen entwickeln, Bild und Caption generieren lassen und
                direkt auf Instagram veröffentlichen – mit deinem Unternehmensprofil als Grundlage.</p>
                <div style="margin-top:28px;">
                  <div class="ce-login-feature">✓ Dialoggestützte Ideenfindung</div>
                  <div class="ce-login-feature">✓ Eigenes oder KI-generiertes Bildmaterial</div>
                  <div class="ce-login-feature">✓ Planung &amp; Freigabe im Kalender</div>
                </div>
              </div>
              <div style="font-size:12px; color:#F6D9C8;">© 2026 ContentEngine · Prototyp im Rahmen einer Bachelorarbeit</div>
            </div>
            """
        )

    with col_form:
        with st.container(key="login_card"):
            modus = st.radio(
                "Modus", ["Anmelden", "Registrieren"], horizontal=True, label_visibility="collapsed",
            )
            st.markdown(
                "#### Willkommen zurück" if modus == "Anmelden" else "#### Konto erstellen"
            )
            st.caption(
                "Melde dich mit deinen Zugangsdaten an." if modus == "Anmelden"
                else "Lege dein Unternehmensprofil an."
            )

            unternehmensname = ""
            if modus == "Registrieren":
                unternehmensname = st.text_input("Unternehmensname", placeholder="z. B. Bäckerei Freiwald")
            email = st.text_input("E-Mail", placeholder="name@unternehmen.de")
            passwort = st.text_input("Passwort", type="password", placeholder="••••••••")

            if modus == "Anmelden":
                if st.button("Anmelden", type="primary", width='stretch'):
                    if not email or not passwort:
                        st.error("Bitte E-Mail und Passwort angeben.")
                    else:
                        user_id = db.verify_login(email, passwort)
                        if user_id is None:
                            st.error("E-Mail oder Passwort ist falsch.")
                        else:
                            st.session_state.user_id = user_id
                            vorhandenes_profil = db.get_profile(user_id)
                            if not vorhandenes_profil.get("onboarding_done"):
                                st.session_state.page = "onboarding"
                            st.rerun()
            else:
                if st.button("Konto erstellen", type="primary", width='stretch'):
                    if not email or not passwort:
                        st.error("Bitte E-Mail und Passwort angeben.")
                    elif db.email_exists(email):
                        st.error("Für diese E-Mail existiert bereits ein Konto.")
                    else:
                        user_id = db.create_user(email, passwort)
                        if unternehmensname:
                            db.save_profile(user_id, unternehmensname=unternehmensname)
                        st.session_state.user_id = user_id
                        st.session_state.page = "onboarding"
                        st.rerun()


# --- Onboarding (einmalige Abfrage aller Unternehmensdaten nach Registrierung) ---

def render_onboarding_page():
    st.html('<div style="height:32px"></div>')
    _, mid, _ = st.columns([1, 3, 1])
    with mid:
        st.markdown("### Willkommen! Lass uns dein Unternehmensprofil einrichten")
        st.caption(
            "Diese Angaben werden einmalig erfasst und danach bei jeder Content-Generierung "
            "automatisch berücksichtigt – du musst sie im Dialog nicht erneut angeben. Du kannst "
            "sie später jederzeit unter „Profil“ anpassen."
        )

        profile = db.get_profile(st.session_state.user_id)

        with st.form("onboarding_formular"):
            st.markdown("##### Unternehmen")
            unternehmensname = st.text_input("Unternehmensname", profile.get("unternehmensname", ""))
            branche = st.text_input("Branche", profile.get("branche", ""), placeholder="z. B. Bäckerei, Fitnessstudio")
            standort = st.text_input("Standort", profile.get("standort", ""))
            strategie = st.text_area(
                "Unternehmensstrategie / Positionierung", profile.get("strategie", ""),
                help="Wofür möchte das Unternehmen stehen (z. B. Premium-Anbieter, nachhaltig, regional, familiär)?",
            )

            st.markdown("##### Zielgruppe & Ansprache")
            zielgruppe = st.text_area(
                "Zielgruppe", profile.get("zielgruppe", ""),
                placeholder="z. B. Berufstätige 25-45 Jahre, gesundheitsbewusst, in der Region",
                help="Wird als Kontext für alle generierten Inhalte verwendet.",
            )
            anrede = st.radio(
                "Ansprache der Zielgruppe", ANREDE_OPTIONEN, horizontal=True,
                index=ANREDE_OPTIONEN.index(profile.get("anrede", "Sie")) if profile.get("anrede") in ANREDE_OPTIONEN else 0,
            )
            tone_of_voice = st.selectbox(
                "Tone of Voice", TONE_OPTIONEN,
                index=TONE_OPTIONEN.index(profile.get("tone_of_voice", "Professionell"))
                if profile.get("tone_of_voice") in TONE_OPTIONEN else 0,
            )

            st.markdown("##### CI-Guide")
            c1, c2 = st.columns(2)
            with c1:
                ci_farbe = st.color_picker("Primärfarbe (Headline & Button)", profile.get("ci_farbe", "#000000"))
            with c2:
                ci_farbe_sekundaer = st.color_picker("Sekundärfarbe (Button-Text & Akzent)", profile.get("ci_farbe_sekundaer") or "#FFFFFF")
            ci_schrift = st.text_input("Schriftart (z. B. Montserrat, Arial)", profile.get("ci_schrift", ""),
                                       help="Google-Fonts-Schriften werden automatisch geladen. Für Hausschriften "
                                            "bitte zusätzlich die Schriftdatei hochladen.")
            schrift_upload = st.file_uploader("Schriftdatei (optional, .ttf/.otf – am besten der fette Schnitt)",
                                              type=["ttf", "otf"], key=None)
            logo_upload = st.file_uploader("Logo (optional)", type=["png", "jpg", "jpeg", "svg"])
            ci_besonderheiten = st.text_area(
                "Sonstige CI-Besonderheiten", profile.get("ci_besonderheiten", ""),
                placeholder="z. B. immer mit Hashtag #, nie Emojis, bestimmte Formulierungen vermeiden …",
            )

            if st.form_submit_button("Profil speichern & los geht's", type="primary", width='stretch'):
                logo_path = profile.get("logo_path", "")
                if logo_upload is not None:
                    logo_path = save_logo(logo_upload, st.session_state.user_id)
                schrift_datei = profile.get("ci_schrift_datei", "")
                if schrift_upload is not None:
                    schrift_datei = save_schrift(schrift_upload, st.session_state.user_id)
                db.save_profile(
                    st.session_state.user_id, ci_schrift_datei=schrift_datei,
                    unternehmensname=unternehmensname, branche=branche, standort=standort,
                    strategie=strategie, zielgruppe=zielgruppe, anrede=anrede,
                    tone_of_voice=tone_of_voice, ci_farbe=ci_farbe, ci_farbe_sekundaer=ci_farbe_sekundaer,
                    ci_schrift=ci_schrift, ci_besonderheiten=ci_besonderheiten, logo_path=logo_path,
                    onboarding_done=1,
                )
                navigiere("chat", verlauf=False)
                st.rerun()


# --- Sidebar / Navigation ---------------------------------------------------

def render_sidebar():
    profile = db.get_profile(st.session_state.user_id)
    name = profile.get("unternehmensname") or "Unternehmensprofil"

    with st.sidebar:
        st.html(
            """
            <div class="ce-logo">
              <div class="ce-logo-badge">
                <svg width="14" height="14" viewBox="0 0 28 28" fill="none">
                  <path d="M8 20 L14 8 L20 20" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </div>
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
        with st.container(key="sidebar_logout"):
            if st.button("Abmelden", key="nav_logout", width='content'):
                st.session_state.user_id = None
                st.session_state.chat_history = []
                st.session_state.chat_context = {}
                st.session_state.vorschlaege = None
                st.session_state.nav_zurueck = []
                st.session_state.nav_vor = []
                st.session_state.page = "chat"
                st.rerun()


SEITEN_TITEL = dict(NAV_ITEMS)


def navigiere(ziel: str, verlauf: bool = True):
    """Seitenwechsel mit Browser-ähnlichem Verlauf (für die Zurück-/Vor-Pfeile)."""
    aktuell = st.session_state.page
    if ziel == aktuell:
        return
    if verlauf and aktuell in SEITEN_TITEL:
        st.session_state.nav_zurueck.append(aktuell)
        st.session_state.nav_vor = []
    st.session_state.page = ziel


def _nav_zurueck():
    if st.session_state.nav_zurueck:
        st.session_state.nav_vor.append(st.session_state.page)
        st.session_state.page = st.session_state.nav_zurueck.pop()


def _nav_vor():
    if st.session_state.nav_vor:
        st.session_state.nav_zurueck.append(st.session_state.page)
        st.session_state.page = st.session_state.nav_vor.pop()


def topbar(title: str, subtitle: str = ""):
    with st.container(key="topbar"):
        c_pfeile, c_titel = st.columns([1, 9], vertical_alignment="center")
        with c_pfeile:
            z, v = st.columns(2)
            zurueck = st.session_state.nav_zurueck
            vor = st.session_state.nav_vor
            with z:
                st.button(
                    "←", key="nav_back", on_click=_nav_zurueck, disabled=not zurueck,
                    help=f"Zurück zu „{SEITEN_TITEL.get(zurueck[-1], '')}“" if zurueck else "Zurück",
                )
            with v:
                st.button(
                    "→", key="nav_forward", on_click=_nav_vor, disabled=not vor,
                    help=f"Weiter zu „{SEITEN_TITEL.get(vor[-1], '')}“" if vor else "Weiter",
                )
        with c_titel:
            st.html(f"<h1>{title}</h1>")
            if subtitle:
                st.html(f"<p>{subtitle}</p>")


# --- Seite: Chat-Dialog ------------------------------------------------------

def render_chat_page():
    topbar("Dialog", "Erzähl der Content-Engine, was du heute brauchst.")

    with st.container(key="pagebody"):
        if not OPENAI_API_KEY:
            st.warning("API_CHATGPT ist nicht gesetzt – der Dialog kann keine Anfragen an die KI schicken.")

        if st.session_state.chat_context:
            if st.button("🔄 Neue Anfrage starten", key="reset_chat"):
                st.session_state.chat_context = {}
                st.session_state.chat_history = []
                st.rerun()

        if not st.session_state.chat_history:
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": "Hi! Woran möchtest du heute arbeiten – neue Idee finden, eine Caption schreiben, "
                            "ein Bild generieren oder gleich beides?",
            })

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
                    "background:#F6E2D8;border-top-right-radius:4px;max-width:420px;"
                    if rolle == "user" else
                    "background:#fff;border:1px solid #E8E5E0;border-top-left-radius:4px;max-width:560px;"
                )
                avatar_bg = "#2D2B28" if rolle == "user" else "#C9532E"
                st.html(
                    f"""
                    <div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;{ausrichtung}">
                      <div style="width:30px;height:30px;border-radius:50%;flex:0 0 auto;display:flex;
                                  align-items:center;justify-content:center;font-size:12px;font-weight:700;
                                  color:#fff;background:{avatar_bg};">{html.escape(avatar_kuerzel)}</div>
                      <div style="padding:14px 18px;font-size:14px;line-height:1.6;border-radius:14px;
                                  font-family:'Work Sans',sans-serif;color:#1A1816;{bubble_style}">
                        {formatiere_chat_text(m['content'])}
                      </div>
                    </div>
                    """
                )

        ctx = st.session_state.chat_context

        if not ctx.get("content_type"):
            with st.container(key="chat_chips"):
                chip_cols = st.columns(5)
                for col, (key, label) in zip(chip_cols[:4], CONTENT_TYPE_LABELS.items()):
                    with col:
                        if st.button(label, key=f"chip_ct_{key}", width='stretch'):
                            ctx["content_type"] = key
                            # FIX: Vorher wurde die Rückfrage des Assistenten als NUTZER-
                            # Nachricht abgeschickt und landete in der Nutzer-Bubble.
                            # Jetzt: Nutzer sagt nur, was er gewählt hat; die Rückfrage
                            # kommt als feste Assistenten-Nachricht (spart zudem einen GPT-Call).
                            chip_antwort(
                                f"Ich möchte: {label}.",
                                "Super! Worum soll es in dem Beitrag gehen, wen möchtest du damit "
                                "erreichen (Stammkunden oder Neukunden) und was ist das "
                                "Kommunikationsziel (Bekanntheit, Image oder Kaufabsicht)?",
                            )
                            st.rerun()
                with chip_cols[4]:
                    if st.button("📈 Trend-Ideen", key="chip_trends", width='stretch'):
                        sende_chat_nachricht(
                            "Zeig mir aktuelle Content-Trend-Ideen für Instagram passend zu meiner Branche."
                        )
                        st.rerun()

        elif not ctx.get("plattform_format"):
            with st.container(key="chat_chips"):
                st.caption("Wo und in welcher Form soll der Beitrag erscheinen?")
                c_pf, c_art, c_ok = st.columns([2, 2, 1], vertical_alignment="bottom")
                with c_pf:
                    plattform = st.selectbox("Plattform", list(PLATTFORMEN), format_func=PLATTFORMEN.get,
                                             key="auswahl_plattform")
                with c_art:
                    art = st.selectbox("Art des Beitrags", list(BEITRAGSARTEN), format_func=BEITRAGSARTEN.get,
                                       key="auswahl_beitragsart")
                schluessel = f"{'ig' if plattform == 'instagram' else 'fb'}_{art}"
                spec = PLATTFORM_SPEZIFIKA[schluessel]
                with c_ok:
                    if st.button("Übernehmen", key="format_uebernehmen", type="primary", width='stretch'):
                        ctx["plattform_format"] = schluessel
                        chip_antwort(f"Format: {format_label(schluessel)}",
                                     f"Alles klar, {format_label(schluessel)} ({spec['verhaeltnis']}, "
                                     f"{spec['aufloesung']}).")
                        st.rerun()
                st.caption(" · ".join(vorgaben_zusammenfassung(spec)))

        elif bild_erforderlich(ctx.get("content_type")) and ctx.get("personen_im_bild") is None:
            with st.container(key="chat_chips"):
                st.caption("Sollen im Bild Personen zu sehen sein?")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Mit Personen", key="chip_personen_ja", width='stretch'):
                        ctx["personen_im_bild"] = True
                        chip_antwort("Mit Personen im Bild.", "Verstanden – mit Personen.")
                        st.rerun()
                with c2:
                    if st.button("Ohne Personen", key="chip_personen_nein", width='stretch'):
                        ctx["personen_im_bild"] = False
                        chip_antwort("Ohne Personen im Bild.", "Verstanden – ohne Personen.")
                        st.rerun()

        # Status: zeigt, was bis zur Generierung noch fehlt, und bietet einen
        # manuellen Start, falls alles da ist, die Generierung aber nicht anlief.
        if ctx.get("content_type"):
            fehlt = fehlende_angaben()
            if fehlt:
                st.caption("Noch offen für die Vorschläge: " + ", ".join(fehlt))
            else:
                hinweis_col, button_col = st.columns([3, 1], vertical_alignment="center")
                with hinweis_col:
                    st.caption("✓ Alle Angaben vollständig.")
                with button_col:
                    if st.button("Vorschläge erstellen →", key="manuell_generieren", type="primary",
                                 width='stretch'):
                        pruefe_und_generiere(erzwingen=True)
                        st.rerun()

        nutzer_text = st.chat_input("Nachricht schreiben …")
        if nutzer_text:
            sende_chat_nachricht(nutzer_text)
            st.rerun()

        kontext = st.session_state.chat_context
        if kontext:
            with st.expander("Erkannte Eckdaten (Debug)"):
                st.json(kontext)


# --- Seite: Vorschläge -------------------------------------------------------

def _bild_platzhalter(text: str, score=None):
    badge = f'<div class="ce-score-badge">● {html.escape(str(score))} Markenpassung</div>' if score is not None else ""
    st.html(
        f"""
        <div class="ce-card-image">
          <svg width="36" height="36" viewBox="0 0 40 40" fill="none">
            <rect x="4" y="8" width="32" height="24" rx="3" stroke="#C9532E" stroke-width="2"/>
            <circle cx="13" cy="16" r="3" stroke="#C9532E" stroke-width="2"/>
            <path d="M4 26l9-8 7 6 5-4 11 10" stroke="#C9532E" stroke-width="2" stroke-linejoin="round"/>
          </svg>
          {badge}
        </div>
        <div style="text-align:center; font-size:11px; color:var(--ink-soft); margin-top:-6px;">{html.escape(text)}</div>
        """
    )


def render_vorschlaege_page():
    data = st.session_state.vorschlaege
    ctx = st.session_state.chat_context
    thema = (data or {}).get("thema") or ctx.get("thema", "")
    segment = (data or {}).get("zielgruppensegment") or ctx.get("zielgruppensegment", "")
    subtitle = f'Thema: „{thema}" · Zielgruppe: {segment}' if thema else ""
    if ctx.get("plattform_format"):
        subtitle += f" · Format: {format_label(ctx['plattform_format'])}"
    topbar("Vorschläge", subtitle)

    with st.container(key="pagebody"):
        if not data:
            st.info("Noch keine Vorschläge vorhanden. Starte zuerst einen Dialog.")
            if st.button("Zum Dialog", type="primary"):
                navigiere("chat")
                st.rerun()
            return

        posts = data.get("posts", []) if isinstance(data, dict) else []
        if not posts:
            st.warning("Es wurden keine Posts gefunden.")
            with st.expander("Rohantwort (Debug)"):
                st.json(data)
            return

        profile = db.get_profile(st.session_state.user_id)
        plattform = ctx.get("plattform_format", "ig_feed")
        spec = PLATTFORM_SPEZIFIKA.get(plattform, PLATTFORM_SPEZIFIKA["ig_feed"])
        mit_bild = bild_erforderlich(ctx.get("content_type", "beides"))
        st.caption("Vorgaben " + format_label(plattform) + ": " + " · ".join(vorgaben_zusammenfassung(spec)))
        if mit_bild:
            pfad, _ = werbebild.finde_schriftdatei(profile.get("ci_schrift", ""), profile.get("ci_schrift_datei", ""))
            if (profile.get("ci_schrift") or profile.get("ci_schrift_datei")) and not pfad:
                zeige_schrift_status(profile)
        cols = st.columns(min(3, len(posts)))

        for i, post in enumerate(posts):
            nummer = post.get("aspekt_nummer", i + 1)
            aspekt = post.get("aspekt", "")
            caption = post.get("full_caption", "")
            score = post.get("markenpassung")
            col = cols[i % len(cols)]
            st.session_state.setdefault(f"bildquelle_{i}", "ki")
            bild_quelle_feld = st.session_state[f"bildquelle_{i}"]

            with col:
                with st.container(border=True):
                    fertiges_bild = None
                    bild = None
                    if mit_bild:
                        # 1) Basisbild bestimmen: eigenes Foto ODER KI-Bild
                        if bild_quelle_feld == "upload":
                            bild = st.session_state.get(f"bild_{i}")
                            basis_bytes = bild.getvalue() if bild else None
                        else:
                            basis_bytes = st.session_state.get(f"ki_bild_{i}")

                        # 2) Text aufs Bild legen (Headline + CTA in CI-Farben)
                        headline = hart_kuerzen(st.session_state.get(f"headline_{i}", standard_headline(post, ctx)),
                                                spec["ueberschrift_max"])
                        angebot = st.session_state.get(f"angebot_{i}", post.get("angebot") or ctx.get("angebot", ""))
                        cta = st.session_state.get(f"cta_{i}", standard_cta(post, ctx))
                        text_aufs_bild = st.session_state.get(f"textbild_{i}", True)
                        if basis_bytes:
                            try:
                                fertiges_bild = werbebild.erstelle_werbebild(
                                    basis_bytes,
                                    headline=headline if text_aufs_bild else "",
                                    cta=cta if text_aufs_bild else "",
                                    plattform_format=plattform,
                                    ci_farbe=profile.get("ci_farbe") or "#C9532E",
                                    ci_farbe_sekundaer=profile.get("ci_farbe_sekundaer") or "",
                                    schrift_name=profile.get("ci_schrift") or "",
                                    logo_pfad=profile.get("logo_path") or "",
                                    schrift_datei=profile.get("ci_schrift_datei") or "",
                                    angebot=angebot if text_aufs_bild else "",
                                )
                            except Exception as e:
                                st.error(f"Bild konnte nicht verarbeitet werden: {e}")

                        if fertiges_bild:
                            st.image(fertiges_bild, width='stretch')
                            if score is not None:
                                st.caption(f"● {score} Markenpassung")
                        elif bild_quelle_feld == "upload":
                            _bild_platzhalter("Eigenes Foto unten hochladen – der Text kommt automatisch drauf", score)
                        elif st.session_state.get(f"ki_fehler_{i}"):
                            _bild_platzhalter("KI-Bild fehlgeschlagen – unten erneut versuchen", score)
                            with st.expander("Fehlerdetails"):
                                st.caption(st.session_state[f"ki_fehler_{i}"])
                        else:
                            _bild_platzhalter("Noch kein KI-Bild", score)

                    st.markdown(f"**Post {nummer}: {aspekt}**")

                    if mit_bild:
                        bu_col, bk_col = st.columns(2)
                        with bu_col:
                            if st.button("Eigenes Foto", key=f"btn_upload_{i}", width='stretch',
                                         type="primary" if bild_quelle_feld == "upload" else "secondary"):
                                st.session_state[f"bildquelle_{i}"] = "upload"
                                st.rerun()
                        with bk_col:
                            if st.button("KI-Bild", key=f"btn_ki_{i}", width='stretch',
                                         type="primary" if bild_quelle_feld == "ki" else "secondary"):
                                st.session_state[f"bildquelle_{i}"] = "ki"
                                st.rerun()

                        if bild_quelle_feld == "upload":
                            st.file_uploader("Foto hochladen", type=["png", "jpg", "jpeg"], key=f"bild_{i}",
                                             label_visibility="collapsed")
                        else:
                            hat_bild = bool(st.session_state.get(f"ki_bild_{i}"))
                            if st.button("↻ Neues KI-Bild" if hat_bild else "✨ KI-Bild erzeugen",
                                         key=f"ki_generieren_{i}", width='stretch'):
                                prompt = baue_bild_prompt(profile, ctx, post)
                                with st.spinner("Bild wird generiert …"):
                                    try:
                                        bild_bytes, modell = generiere_ki_bild(prompt, plattform)
                                        st.session_state[f"ki_bild_{i}"] = bild_bytes
                                        st.session_state[f"ki_prompt_{i}"] = prompt
                                        st.session_state["bildmodell_ok"] = modell
                                        st.session_state.pop(f"ki_fehler_{i}", None)
                                        st.rerun()
                                    except Exception as e:
                                        st.error(str(e))
                            if st.session_state.get(f"ki_prompt_{i}"):
                                with st.expander("Verwendeter Bild-Prompt"):
                                    st.caption(st.session_state[f"ki_prompt_{i}"])

                        with st.expander("Text auf dem Bild", expanded=False):
                            st.checkbox("Text aufs Bild legen", value=True, key=f"textbild_{i}")
                            st.text_input(
                                "Headline", hart_kuerzen(standard_headline(post, ctx), spec["ueberschrift_max"]),
                                key=f"headline_{i}", max_chars=spec["ueberschrift_max"] or None,
                                help="Aufmerksamkeit (AIDA: Attention) – kurz und prägnant."
                                     + (f" Max. {spec['ueberschrift_max']} Zeichen." if spec["ueberschrift_max"] else ""),
                            )
                            st.text_input("Angebot / Aktion (Störer)", post.get("angebot") or ctx.get("angebot", ""),
                                          key=f"angebot_{i}",
                                          help="Z. B. „10 % Rabatt“ oder „nur 1,50 €“ – erscheint als runder "
                                               "Aktions-Button oben auf dem Bild. Leer lassen = kein Störer.")
                            st.text_input("Call-to-Action", standard_cta(post, ctx), key=f"cta_{i}",
                                          help="Handlungsaufforderung (AIDA: Action) passend zum Kommunikationsziel.")

                    edited_caption = st.text_area(
                        "Caption", hart_kuerzen(caption, spec["primaertext_max"]), height=120, key=f"caption_{i}",
                        label_visibility="collapsed", max_chars=spec["primaertext_max"],
                    )
                    if post.get("original_caption"):
                        with st.expander(f"Automatisch auf {spec['primaertext_max']} Zeichen angepasst – Original"):
                            st.caption(post["original_caption"])
                    zeichen_info = f"{len(edited_caption)}"
                    zeichen_info += (f" / {spec['primaertext_min']}–{spec['primaertext_max']}" if spec["primaertext_min"]
                                     else f" / {spec['primaertext_max']}")
                    st.caption(f"Primärtext: {zeichen_info} Zeichen")
                    for hinweis in pruefe_texte(spec, edited_caption,
                                                st.session_state.get(f"headline_{i}", "") if mit_bild else ""):
                        st.warning(hinweis, icon="⚠️")

                    with st.expander("Jetzt veröffentlichen oder für Kalender einplanen"):
                        aktion = st.radio(
                            "Aktion", ["Jetzt veröffentlichen", "Für Kalender einplanen"],
                            key=f"aktion_{i}", label_visibility="collapsed",
                        )
                        geplantes_datum = geplante_zeit = None
                        if aktion == "Für Kalender einplanen":
                            geplantes_datum = st.date_input("Datum", min_value=datetime.date.today(), key=f"datum_{i}")
                            geplante_zeit = st.time_input("Uhrzeit", key=f"zeit_{i}")

                        if st.button("Auswählen & freigeben", key=f"bestaetigen_{i}", type="primary",
                                     width='stretch'):
                            if mit_bild and not fertiges_bild:
                                st.error("Bitte zuerst ein Foto hochladen oder ein KI-Bild erzeugen.")
                            else:
                                try:
                                    image_url = ""
                                    # Beim Einplanen das FERTIGE Werbebild (inkl. Text) dauerhaft speichern
                                    if fertiges_bild and aktion == "Für Kalender einplanen":
                                        image_url = speichere_generiertes_bild(fertiges_bild, st.session_state.user_id)

                                    if aktion == "Jetzt veröffentlichen":
                                        # Simulierte Veröffentlichung (siehe render_instagram_simulation):
                                        # kein echter Meta-Graph-API-Call, da das ein verknüpftes
                                        # Instagram Business Account je Testnutzer:in voraussetzen würde.
                                        render_instagram_simulation(
                                            profile.get("unternehmensname", ""), edited_caption,
                                            bild_bytes=fertiges_bild, image_url=image_url,
                                            plattform_format=plattform,
                                        )
                                        st.success(f"Post {nummer} wurde (simuliert) auf {format_label(plattform)} veröffentlicht.")
                                    else:
                                        db.add_scheduled_post(
                                            user_id=st.session_state.user_id, aspekt=aspekt, caption=edited_caption,
                                            image_url=image_url, bild_quelle=bild_quelle_feld if mit_bild else "",
                                            scheduled_date=geplantes_datum.isoformat(),
                                            scheduled_time=geplante_zeit.strftime("%H:%M"),
                                            plattform_format=plattform,
                                        )
                                        st.success(f"Post {nummer} wurde für {geplantes_datum} eingeplant.")
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

        with st.expander("Rohantwort von n8n (Debug)"):
            st.json(data)


# --- Seite: Kalender ----------------------------------------------------------

def render_kalender_page():
    year, month = st.session_state.kalender_monat
    monatsname = calendar.month_name[month]
    topbar("Kalender", f"{monatsname} {year} · geplante Beiträge")

    with st.container(key="pagebody"):
        col_main, col_side = st.columns([3, 1], gap="large")

        with col_main:
            col_prev, col_title, col_next = st.columns([1, 5, 1])
            with col_prev:
                if st.button("‹"):
                    st.session_state.kalender_monat = (year - 1, 12) if month == 1 else (year, month - 1)
                    st.rerun()
            with col_next:
                if st.button("›"):
                    st.session_state.kalender_monat = (year + 1, 1) if month == 12 else (year, month + 1)
                    st.rerun()

            posts = db.list_scheduled_posts(st.session_state.user_id, year=year, month=month)
            posts_by_day = {}
            for p in posts:
                tag = int(p["scheduled_date"].split("-")[2])
                posts_by_day.setdefault(tag, []).append(p)

            heute = datetime.date.today()
            wochen = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
            wochentage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

            header_cols = st.columns(7)
            for c, name in zip(header_cols, wochentage):
                with c:
                    st.html(
                        f"<div style='font-size:11px; font-weight:600; color:var(--ink-soft); text-align:center'>{name}</div>"
                    )

            for woche in wochen:
                cols = st.columns(7)
                for col, tag in zip(cols, woche):
                    with col:
                        if tag == 0:
                            st.html("<div class='ce-day' style='opacity:0.3'></div>")
                            continue
                        ist_heute = (year, month, tag) == (heute.year, heute.month, heute.day)
                        klass = "ce-day ce-today" if ist_heute else "ce-day"
                        chips = "".join(
                            f"<span class='ce-chip'>{'✅' if p['status'] == 'veroeffentlicht' else '🕒'} "
                            f"{p['scheduled_time']} · "
                            f"{('IG · ' if str(p.get('plattform_format', '')).startswith('ig') else 'FB · ' if str(p.get('plattform_format', '')).startswith('fb') else '')}"
                            f"{html.escape(p['aspekt'] or 'Beitrag')}</span>"
                            for p in posts_by_day.get(tag, [])
                        )
                        st.html(
                            f"<div class='{klass}'><div class='ce-daynum'>{tag}</div>{chips}</div>"
                        )

        with col_side:
            st.markdown("##### Als Nächstes geplant")
            alle_posts = db.list_scheduled_posts(st.session_state.user_id)
            kommende = [p for p in alle_posts if p["status"] == "geplant"]

            if not kommende:
                st.caption("Keine geplanten Beiträge.")
            else:
                for p in kommende[:8]:
                    st.html(
                        f"""
                        <div style="display:flex; gap:10px; margin-bottom:12px;">
                          <div style="width:40px; height:40px; border-radius:8px; background:var(--accent-soft); flex:0 0 auto;"></div>
                          <div>
                            <div style="font-size:13px; font-weight:600;">{p['aspekt'] or 'Beitrag'}</div>
                            <div style="font-size:12px; color:var(--ink-soft);">{p['scheduled_date']} · {p['scheduled_time']} Uhr{(' · ' + format_label(p.get('plattform_format'))) if p.get('plattform_format') else ''}</div>
                          </div>
                        </div>
                        """
                    )
                    if st.button("Jetzt veröffentlichen", key=f"publish_{p['id']}", width='stretch'):
                        db.mark_post_published(p["id"])
                        # Vorschau merken statt direkt anzeigen + rerun (sonst verschwindet sie sofort)
                        st.session_state.kalender_vorschau = p
                        st.rerun()

            vorschau = st.session_state.pop("kalender_vorschau", None)
            if vorschau:
                profile = db.get_profile(st.session_state.user_id)
                st.success("(Simuliert) veröffentlicht.")
                render_instagram_simulation(
                    profile.get("unternehmensname", ""), vorschau["caption"], image_url=vorschau["image_url"],
                    plattform_format=vorschau.get("plattform_format", ""),
                )

            if st.button("+ Neuen Beitrag planen", width='stretch'):
                navigiere("chat" if not st.session_state.vorschlaege else "vorschlaege")
                st.rerun()


# --- Seite: Unternehmensprofil -------------------------------------------------

def render_profil_page():
    topbar("Profil", "Diese Angaben werden bei jeder Content-Generierung als Kontext mitgegeben.")

    with st.container(key="pagebody"):
        profile = db.get_profile(st.session_state.user_id)

        if profile.get("logo_path") and os.path.exists(profile["logo_path"]):
            st.image(profile["logo_path"], width=96, caption="Aktuelles Logo")

        with st.form("profil_formular"):
            st.markdown("##### Unternehmen")
            unternehmensname = st.text_input("Unternehmensname", profile.get("unternehmensname", ""))
            branche = st.text_input("Branche", profile.get("branche", ""))
            standort = st.text_input("Standort", profile.get("standort", ""))
            strategie = st.text_area(
                "Unternehmensstrategie / Positionierung", profile.get("strategie", ""),
                help="Kurzbeschreibung, wofür das Unternehmen stehen möchte (z. B. Premium-Anbieter, "
                     "nachhaltig, regional, familiär).",
            )

            st.markdown("##### Zielgruppe & Ansprache")
            zielgruppe = st.text_area("Zielgruppe", profile.get("zielgruppe", ""))
            anrede = st.radio(
                "Ansprache der Zielgruppe", ANREDE_OPTIONEN, horizontal=True,
                index=ANREDE_OPTIONEN.index(profile.get("anrede", "Sie")) if profile.get("anrede") in ANREDE_OPTIONEN else 0,
            )
            tone_of_voice = st.selectbox(
                "Tone of Voice", TONE_OPTIONEN,
                index=TONE_OPTIONEN.index(profile.get("tone_of_voice", "Professionell"))
                if profile.get("tone_of_voice") in TONE_OPTIONEN else 0,
            )

            st.markdown("##### CI-Guide")
            c1, c2 = st.columns(2)
            with c1:
                ci_farbe = st.color_picker("Primärfarbe (Headline & Button)", profile.get("ci_farbe", "#000000"))
            with c2:
                ci_farbe_sekundaer = st.color_picker("Sekundärfarbe (Button-Text & Akzent)", profile.get("ci_farbe_sekundaer") or "#FFFFFF")
            ci_schrift = st.text_input("Schriftart (z. B. Montserrat, Arial)", profile.get("ci_schrift", ""),
                                       help="Google-Fonts-Schriften werden automatisch geladen. Für Hausschriften "
                                            "bitte zusätzlich die Schriftdatei hochladen.")
            schrift_upload = st.file_uploader("Schriftdatei (optional, .ttf/.otf – am besten der fette Schnitt)",
                                              type=["ttf", "otf"], key=None)
            logo_upload = st.file_uploader("Logo ersetzen (optional)", type=["png", "jpg", "jpeg", "svg"])
            ci_besonderheiten = st.text_area("Sonstige CI-Besonderheiten", profile.get("ci_besonderheiten", ""))

            instagram_account_id = st.text_input(
                "Instagram Business Account ID (optional, für spätere echte Anbindung)",
                profile.get("instagram_account_id", ""),
                help="Die Veröffentlichung läuft im Prototyp als Simulation – ohne echte Verknüpfung.",
            )

            if st.form_submit_button("Profil speichern", type="primary"):
                logo_path = profile.get("logo_path", "")
                if logo_upload is not None:
                    logo_path = save_logo(logo_upload, st.session_state.user_id)
                schrift_datei = profile.get("ci_schrift_datei", "")
                if schrift_upload is not None:
                    schrift_datei = save_schrift(schrift_upload, st.session_state.user_id)
                db.save_profile(
                    st.session_state.user_id, ci_schrift_datei=schrift_datei,
                    unternehmensname=unternehmensname, branche=branche,
                    standort=standort, tone_of_voice=tone_of_voice, ci_farbe=ci_farbe,
                    ci_farbe_sekundaer=ci_farbe_sekundaer, ci_schrift=ci_schrift,
                    ci_besonderheiten=ci_besonderheiten, strategie=strategie, zielgruppe=zielgruppe,
                    anrede=anrede, instagram_account_id=instagram_account_id, logo_path=logo_path,
                )
                st.session_state.profil_gespeichert = True
                st.rerun()

        if st.session_state.pop("profil_gespeichert", False):
            st.success("Profil gespeichert.")
        zeige_schrift_status(db.get_profile(st.session_state.user_id))


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