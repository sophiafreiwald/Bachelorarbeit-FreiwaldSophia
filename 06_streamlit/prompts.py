"""Prompt-Architektur der Content-Engine: alle Anweisungen an die Sprach- und Bildmodelle.

Quellenhinweise stehen nur als Kommentare (# Quellen: …) und werden nicht an das Modell geschickt.

Übersicht (in der Reihenfolge, in der die App die Prompts aufruft)
  0. VORBEREITUNG        – Website-Tonalität auslesen (einmalig im Profil); Ergebnis fließt als Eingabe in 2 ein
  1. DIALOG              – Frage „thema“ (KI), Wirkung/Art des Beitrags (feste Auswahl), Klärung, Auswertung
                           freier Antworten
  2. POST_GENERIERUNG    – EIN Gesamtprompt nach der letzten Dialogfrage (Rahmenmodell, Werbebotschaft,
                           Bildgestaltung); 2b Ideen (Inhaltsart „Nur Idee“)
  3. QUALITÄTSSICHERUNG  – je Beitrag vor der Anzeige: 3a Selbstprüfung (Feedback) → 3b Überarbeiten
     TEXT                  → 3c Kürzen (nur bei überschrittenem Zeichenlimit) → 3d Anrede-Korrektur (nur bei Verstoß)
  4. BILDERSTELLUNG      – zusammengesetzte Endform für das Bildmodell (parallel zu 3)
  5. FAKTENCHECK         – vor der Freigabe
  B. VERGLEICHSVARIANTE  – naiver Prompt für den A/B-Test (Variante B)
"""

import datetime
import json
import re


# --- PROFILFAKTEN (Hilfsfunktionen, für alle Prompts einheitlich aufbereitet) ----------------

WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def oeffnungszeiten(profile: dict) -> dict:
    try:
        daten = json.loads(profile.get("oeffnungszeiten") or "{}")
    except ValueError:
        return {}
    return {t: str(daten.get(t) or "").strip() for t in WOCHENTAGE}


def oeffnungszeiten_text(profile: dict) -> str:
    """'Mo 06:00–18:00; …' – nur Tage mit Angabe."""
    return "; ".join(f"{t} {z}" for t, z in oeffnungszeiten(profile).items() if z)


def anschrift_text(profile: dict) -> str:
    ort = " ".join(x for x in (profile.get("plz"), profile.get("ort") or profile.get("standort")) if x)
    return ", ".join(x for x in (profile.get("strasse"), ort) if x)


def profil_fakten(profile: dict) -> dict:
    """Überprüfbare Fakten aus dem Profil, getrennt beschriftet (leere Angaben entfallen)."""
    fakten = {
        "Anschrift": anschrift_text(profile),
        "Öffnungszeiten": oeffnungszeiten_text(profile),
        "Telefon": profile.get("telefon") or "",
        "E-Mail": profile.get("kontakt_email") or "",
    }
    return {k: v.strip() for k, v in fakten.items() if v and v.strip()}


def profil_fakten_text(profile: dict) -> str:
    return " ".join(f"{k}: {v}." for k, v in profil_fakten(profile).items())


def _heute() -> str:
    return datetime.date.today().isoformat()


# Persona für die Dialog-Prompts. Quelle: White et al., 2023 – Persona Pattern
ROLLE_KMU = "Nimm die Rolle eines Marketingmanagers für die Plattformen Instagram und Facebook ein."


# --- 0. VORBEREITUNG: WEBSITE-TONALITÄT (einmalig im Profil, fließt in 2 ein) ----------------
# Ablauf: app.website_text_abrufen liefert den Seitentext → WEBSITE_TONALITAET_SYSTEM → Profilfeld
# „website_tonalitaet“ → in 2 als TONALITÄT_LAUT_WEBSITE an den Gesamtprompt übergeben.
# Die Website wird nur genutzt, um die Tonalität der Unternehmenstexte grob auszulesen. Fakten (Öffnungszeiten,
# Anschrift, Preise …) kommen ausschließlich aus dem Profil.

WEBSITE_TONALITAET_SYSTEM = (
    "Du analysierst den bereitgestellten Text einer Unternehmenswebsite AUSSCHLIESSLICH sprachlich: Wie formuliert "
    "das Unternehmen seine Texte? Beschreibe die Tonalität knapp in 4 bis 6 Stichpunkten:\n"
    "- Anrede der Leserinnen und Leser (Du, Sie, Ihr oder neutral)\n"
    "- Satzlänge und Satzbau (z. B. kurz und direkt, ausführlich, viele Aufzählungen)\n"
    "- Wortwahl (z. B. sachlich, emotional, fachlich, umgangssprachlich, regional)\n"
    "- Stimmung und Haltung (z. B. herzlich, traditionsbewusst, modern, zurückhaltend)\n"
    "- Auffälligkeiten wie Ausrufezeichen, Emojis, Wir-Form oder wiederkehrende Formulierungen (höchstens drei "
    "kurze wörtliche Beispiele)\n\n"
    "Übernimm KEINE Fakten (keine Öffnungszeiten, Anschrift, Kontaktdaten, Preise, Produkte oder Aktionen). "
    "Beschreibe nur, was im Text tatsächlich erkennbar ist, und erfinde nichts. Navigation, Cookie-Hinweise und "
    "Impressumsangaben sind keine Unternehmenstexte und bleiben unberücksichtigt. Ist zu wenig zusammenhängender "
    "Text vorhanden, antworte genau: „Zu wenig Text, um die Tonalität zu bestimmen.“\n\n"
    "Antworte als kurze Stichpunktliste auf Deutsch, ohne Einleitung."
)


# --- 1. DIALOG ------------------------------------------------------------------------------
# Ablauf (Reihenfolge legt app.naechster_schritt fest; Vorgehen nach Eiring, 2026):
# Beitrag oder nur Ideen → THEMA → WIRKUNG (Ziel wird abgeleitet) → Art des Beitrags → Format (Inhalt: Bild + Caption,
# Story nur Bild)
# → Personen im Bild → KLÄRUNG → Zusammenfassung. Die Zielgruppe stammt aus der Registrierung.

# Zieloptionen = psychographische Ziele nach Meffert et al. (2019)
ZIEL_OPTIONEN = ["Bekanntheit", "Produktwissen", "Einstellung", "Kauf- und Wiederkaufabsicht"]

# Wirkungsebenen der Marketingkommunikation nach Bruhn (2014). Jede Ebene bestimmt die passenden
# psychographischen Ziele nach Meffert et al. (2019): Bekanntheit + Produktwissen → kognitiv,
# Einstellung → affektiv, Kauf- und Wiederkaufabsicht → konativ. Angezeigt werden label/text mit Alltagsbeispielen.
WIRKUNGSEBENEN = {
    "kognitiv": {"label": "Informieren", "label_sie": "Informieren",
                 "text": "z. B. ein neues Produkt vorstellen oder erklären, wie etwas funktioniert",
                 "text_sie": "z. B. ein neues Produkt vorstellen oder erklären, wie etwas funktioniert",
                 "bedeutung": "kognitiv-orientiert: Erhöhung des Wissensstandes",
                 "ziele": ["Bekanntheit", "Produktwissen"]},
    "affektiv": {"label": "Sympathie aufbauen", "label_sie": "Sympathie aufbauen",
                 "text": "z. B. das Team zeigen oder Vertrauen schaffen",
                 "text_sie": "z. B. das Team zeigen oder Vertrauen schaffen",
                 "bedeutung": "affektiv-orientiert: Schaffung emotionaler Markenverbundenheit",
                 "ziele": ["Einstellung"]},
    "konativ": {"label": "Zum Handeln bewegen", "label_sie": "Zum Handeln bewegen",
                "text": "z. B. zum Kaufen, Buchen oder Vorbeikommen anregen",
                "text_sie": "z. B. zum Kaufen, Buchen oder Vorbeikommen anregen",
                "bedeutung": "konativ-orientiert: Steigerung der Kaufabsicht",
                "ziele": ["Kauf- und Wiederkaufabsicht"]},
}


def ziel_zur_wirkung(ebene: str) -> str:
    """Kommunikationsziel nach Meffert et al. (2019) zur gewählten Wirkungsebene (keine eigene Dialogfrage).
    Bei kognitiv gehen beide Ziele an die KI, die je Beitrag den passenden Schwerpunkt wählt."""
    return " und ".join((WIRKUNGSEBENEN.get(ebene) or {}).get("ziele", []))


def wirkungsebene_zu_ziel(ziel: str) -> str:
    """Leitet die Wirkungsebene aus einem Meffert-Ziel ab (z. B. bei freier Antwort im Chat)."""
    return next((k for k, v in WIRKUNGSEBENEN.items() if ziel in v["ziele"] or ziel == " und ".join(v["ziele"])), "")


def wirkungsebene_zeile(p: dict) -> str:
    e = WIRKUNGSEBENEN.get(p.get("wirkungsebene") or wirkungsebene_zu_ziel(p.get("kommunikationsziel") or ""))
    return e["bedeutung"] if e else "nicht festgelegt"


def _dialog_kontext(profile: dict, ctx: dict, schritt: str = "") -> str:
    """User-Kontext der Dialog-Prompts, ggf. mit AKTUELLER_SCHRITT."""
    daten = {
        "HEUTE": _heute(),
        "UNTERNEHMEN": {
            "Name": profile.get("unternehmensname", ""), "Branche": profile.get("branche", ""),
            "Social-Media-Strategie (Ziele)": profile.get("strategie", ""),
            "Positionierung": profile.get("positionierung", ""),
            "Zielgruppe allgemein": profile.get("zielgruppe", ""),
            **profil_fakten(profile),
        },
        "BISHERIGE_ANGABEN": {k: ctx.get(k) for k in ("content_type", "plattform_format", "thema", "angebot",
                                                     "wirkungsebene", "kommunikationsziel", "inhaltlicher_contenttyp", "personen_im_bild")
                              if ctx.get(k) not in (None, "")},
        "RUECKFRAGEN_UND_ANTWORTEN": ctx.get("klaerungen") or [],
    }
    if schritt:
        daten = {"AKTUELLER_SCHRITT": schritt, **daten}
    return json.dumps(daten, ensure_ascii=False)


# 1.1 Frage „thema“
# Quellen: White et al., 2023 (Persona; Flipped Interaction); Eiring, 2026 (Themen-/Contentplanung)
DIALOG_THEMA_SYSTEM = (
    ROLLE_KMU + " Unterstütze die Planung organischer Instagram- und Facebook-Inhalte.\n\n"
    "Führe einen schrittweisen Dialog. Nutze die vorliegenden Unternehmensinformationen, die bisherige "
    "Unterhaltung und bereits getroffene Formatentscheidungen. Stelle nur die Rückfragen, die für einen "
    "umsetzbaren Beitrag notwendig sind; wiederhole keine beantworteten Fragen.\n\n"
    "AKTUELLER SCHRITT: Erfrage das Thema bzw. den Anlass des Beitrags. Stelle GENAU EINE kurze und konkrete "
    "Frage mit höchstens zwei Sätzen. Schreibe auf Deutsch, freundlich, direkt und in Du-Form, ohne Floskeln.\n\n"
    "Gib unter „vorschlaege“ GENAU DREI verschiedene, möglichst konkrete Themenideen mit höchstens acht Wörtern "
    "je Idee zurück. Orientiere dich an Branche, Produkten, Positionierung, übergeordneten Zielen und – falls "
    "passend – der Jahreszeit am unter HEUTE angegebenen Datum. Vorschläge dürfen Ideen sein, aber keine "
    "erfundenen Unternehmensfakten oder angeblich recherchierten aktuellen Trends enthalten.\n\n"
    "Setze „empfehlung“ auf null. Erläutere unter „begruendung“ kurz, worauf die Vorschläge aufbauen. "
    "Unterstelle keine unbekannten Zielgruppeninteressen.\n\n"
    "Antworte ausschließlich als valides JSON mit exakt diesen Schlüsseln:\n"
    '{"frage":"...","vorschlaege":["...","...","..."],"empfehlung":null,"begruendung":"..."}'
)

_DIALOG_SYSTEM = {"thema": DIALOG_THEMA_SYSTEM}


# 1.2b Inhaltlicher Contenttyp
# Quellen: Shahbaznezhad et al., 2021 (rational/informativ, interaktional, transaktional);
# Abgrenzung zum Kommunikationsziel: Bruhn, 2014; Meffert et al., 2019. Das Unternehmen wählt den Typ selbst.
INHALTLICHE_CONTENTTYPEN = {
    "informativ": "Informativ",
    "interaktional": "Interaktional",
    "transaktional": "Transaktional",
}

# 1.2c Klärung offener Punkte vor der Zusammenfassung
# Quelle: White et al., 2023 – Flipped Interaction Pattern („solange gezielte Fragen stellen, bis das Ergebnis
# klar ist“); höchstens drei Rückfragen
KLAERUNG_SYSTEM = (
    ROLLE_KMU + "\n\n"
    "AKTUELLER SCHRITT: Klärung. Alle Pflichtangaben des Dialogs liegen vor (BISHERIGE_ANGABEN). Stelle so lange "
    "gezielte Fragen, bis klar ist, welcher Beitrag erstellt werden soll. Prüfe dafür, ob für einen umsetzbaren, "
    "sachlich korrekten Beitrag noch eine WESENTLICHE Information fehlt oder unklar ist – zum Beispiel: Ein Angebot "
    "ohne Zeitraum oder Bedingungen, ein mehrdeutiges Thema, ein Anlass ohne Datum, ein Produkt, das im "
    "Unternehmensprofil nicht vorkommt.\n\n"
    "Frage NICHT nach Angaben, die bereits in BISHERIGE_ANGABEN, im UNTERNEHMEN oder in RUECKFRAGEN_UND_ANTWORTEN "
    "stehen, und nicht nach Ziel, Art des Beitrags, Plattform, Format, Inhaltsart oder Personen im Bild – diese "
    "sind entschieden. Erfinde keinen Klärungsbedarf: Ist alles Wesentliche klar, setze „klar“ auf true.\n\n"
    "Ist etwas unklar, setze „klar“ auf false und stelle GENAU EINE kurze, konkrete Frage (höchstens zwei Sätze, "
    "Deutsch, Du-Form). Gib unter „vorschlaege“ bis zu drei kurze mögliche Antworten, sofern sinnvoll; erfinde "
    "dabei keine Unternehmensfakten.\n\n"
    "Antworte ausschließlich als valides JSON mit exakt diesen Schlüsseln:\n"
    '{"klar":true,"frage":"","vorschlaege":[]}'
)
KLAERUNG_MAX_FRAGEN = 3


def klaerung_prompts(profile: dict, ctx: dict) -> dict:
    """Prompts für den Klärungsschritt. Antwort als JSON {"klar": bool, "frage": str, "vorschlaege": [str]}."""
    return {"system": KLAERUNG_SYSTEM, "user": _dialog_kontext(profile, ctx, "klaerung")}


def klaerungen_text(p: dict) -> str:
    """Rückfragen und Antworten aus dem Klärungsschritt als Zeile für den Gesamtprompt."""
    return " | ".join(f"{k.get('frage', '')} → {k.get('antwort', '')}" for k in (p.get("klaerungen") or [])) or "keine"


def contenttyp_zeile(p: dict) -> str:
    """{INHALTLICHER_CONTENTTYP} für den Gesamtprompt."""
    typ = p.get("inhaltlicher_contenttyp")
    if typ in INHALTLICHE_CONTENTTYPEN:
        return f"{typ} ({INHALTLICHE_CONTENTTYPEN[typ]})"
    return "nicht festgelegt"


# Ideen statt Rückfrage: Ist die Person unsicher oder bittet um Vorschläge,
# stellt die KI selbst konkrete Ideen vor, statt erneut nach dem Thema zu fragen.
IDEEN_MODUS_ZUSATZ = (
    "\n\nWICHTIG – IDEEN ERBETEN: Die Person weiß das Thema noch nicht oder hat um Vorschläge gebeten. Frage "
    "NICHT erneut nach dem Thema. Formuliere „frage“ stattdessen so: ein kurzer Einleitungssatz, dann die drei Ideen "
    "als Liste – je Idee der Titel fett und ein Satz, was der Beitrag zeigen würde und warum er zum Unternehmen "
    "passt –, zum Schluss „Welche Idee gefällt dir? Du kannst sie auch anpassen.“ „vorschlaege“ enthält dieselben "
    "drei Titel (höchstens acht Wörter je Titel)."
)


def dialog_frage_prompts(schritt: str, profile: dict, ctx: dict, ausschliessen=(), ideen_modus: bool = False) -> dict:
    """Prompts für die Frage eines Dialogschritts. Antwort als JSON
    {"frage": str, "vorschlaege": [str], "empfehlung": str|null, "begruendung": str}."""
    system = _DIALOG_SYSTEM[schritt]
    if ideen_modus:
        system += IDEEN_MODUS_ZUSATZ
    if ausschliessen:
        # Technische Ergänzung (Knopf „Andere Ideen“): bereits gezeigte Ideen nicht wiederholen
        system += ("\n\nDiese Ideen wurden schon vorgeschlagen und dürfen NICHT erneut kommen: "
                   + "; ".join(ausschliessen) + ".")
    return {"system": system, "user": _dialog_kontext(profile, ctx)}


# 1.3 Auswertung freier Antworten
# Quelle: White et al., 2023 (Flipped Interaction) für die Rückmeldung
DIALOG_AUSWERTUNG_SYSTEM = (
    "Du extrahierst Angaben aus der freien Antwort im geführten Content-Engine-Dialog. Unterscheide ausdrücklich "
    "geäußerte Angaben von Vorschlägen und Vermutungen.\n\n"
    "AKTUELLER SCHRITT wird vom User-Payload angegeben. Erfasse ALLE in der Antwort ausdrücklich enthaltenen "
    "Informationen, auch wenn sie einem späteren Schritt zuzuordnen sind:\n"
    "- „thema“: konkretes Beitragsthema, kurz formuliert.\n"
    "- „angebot“: genanntes Angebot einschließlich vorhandener Zahlen, Bedingungen und Zeitraum; niemals neue "
    "Angebotsdetails ergänzen.\n"
    "- „kommunikationsziel“: ausschließlich „Bekanntheit“, „Produktwissen“, „Einstellung“ oder „Kauf- und "
    "Wiederkaufabsicht“, und nur wenn die Person "
    "das Ziel ausdrücklich benennt. Nicht aus einem Rabatt oder Thema automatisch als Nutzerentscheidung ableiten.\n"
    "- „inhaltlicher_contenttyp“: ausschließlich „informativ“, „interaktional“ (auch: emotional), „transaktional“ "
    "(auch: verkaufsorientiert), und nur wenn die Person die Art des Beitrags "
    "ausdrücklich benennt. Nicht aus Thema, Angebot oder Ziel ableiten.\n"
    "- „ideen_gewuenscht“: true, wenn Ideen oder Vorschläge erbeten werden ODER die Person beim aktuellen Schritt "
    "„thema“ unsicher ist (z. B. „weiß ich noch nicht“, „keine Ahnung“, „mach du mal“), sonst false. In diesem Fall "
    "keine Rückfrage stellen – die App zeigt direkt konkrete Ideen.\n"
    "Nicht genannte Angaben erhalten null.\n\n"
    "Ein relativer Zeitraum wie „nächste Woche“ darf anhand von HEUTE kalenderlogisch aufgelöst werden, sofern "
    "eindeutig. Kennzeichne in „rueckmeldung“ knapp, dass der Zeitraum interpretiert wurde; erfinde keine Uhrzeit "
    "und keine Aktionsbedingungen. Bei Mehrdeutigkeit frage zur Klärung nach, ohne den Zeitpunkt als gesicherten "
    "Fakt auszugeben.\n\n"
    "„rueckmeldung“ ist ein bis zwei kurze, freundliche Sätze in Du-Form: bestätige die tatsächlich übernommenen "
    "Informationen; falls etwas für den AKTUELLEN SCHRITT unklar ist, formuliere eine einzige präzise Rückfrage. "
    "Weisen Angaben einen sachlichen Konflikt zur bekannten Positionierung oder einen bereits verstrichenen "
    "Zeitraum auf, weise konstruktiv darauf hin. Frage nicht nach anderen Schritten – das übernimmt die App. "
    "Wiederhole keine alten Fragen.\n\n"
    "Antworte ausschließlich mit validem JSON und exakt diesen Schlüsseln:\n"
    '{"thema":null,"angebot":null,"kommunikationsziel":null,"inhaltlicher_contenttyp":null,"ideen_gewuenscht":false,'
    '"rueckmeldung":"..."}'
)


def dialog_auswertung_prompts(schritt: str, antwort: str, profile: dict, ctx: dict) -> dict:
    """Prompts zur Auswertung einer FREIEN Antwort. Antwort als JSON
    {"thema", "angebot", "kommunikationsziel", "ideen_gewuenscht", "rueckmeldung"}."""
    user = _dialog_kontext(profile, ctx, schritt) + "\n\nANTWORT:\n" + antwort
    return {"system": DIALOG_AUSWERTUNG_SYSTEM, "user": user}


# --- 2. POST_GENERIERUNG (Gesamtprompt) -------------------------------------------------


# Modell-Einstellungen für die Beitragserstellung (ein Aufruf nach der letzten Dialogfrage, app.erzeuge_beitraege)
MODELL = "gpt-6-luna"  # wie app.OPENAI_MODEL
GESAMT_TEMPERATUR = 0.8
ANZAHL_BEITRAEGE = 3

def _unternehmensprofil_block(p: dict) -> str:
    """{UNTERNEHMENSPROFIL}: alle Profilangaben aus der Datenbank, je Angabe eine Zeile."""
    zeilen = [("Name", p.get("unternehmensname")), ("Branche", p.get("branche")),
              ("Positionierung", p.get("positionierung")), ("Social-Media-Strategie (Ziele)", p.get("strategie")),
              ("Tone of Voice", p.get("tone_of_voice"))]
    zeilen += list(profil_fakten(p).items())
    text = "\n".join(f"- {k}: {str(v).strip()}" for k, v in zeilen if v and str(v).strip())
    return text or "- keine Angaben"


def profilvorgaben_text(p: dict) -> str:
    """{VERBINDLICHE_PROFILVORGABEN}: Unternehmensname, Ansprache und CI-Vorgaben aus dem Profil – müssen IMMER
    genau so verwendet werden."""
    name = (p.get("unternehmensname") or "").strip()
    sie = p.get("anrede") != "Du"
    teile = []
    if name:
        teile.append(f"Unternehmensname immer exakt „{name}“ (gleiche Schreibweise, Groß-/Kleinschreibung und "
                     "Rechtsform; nicht abkürzen, nicht übersetzen, nicht umformulieren)")
    teile.append("Ansprache ausschließlich in der Sie-Form (Sie, Ihnen, Ihr, Ihre …), nie duzen" if sie else
                 "Ansprache ausschließlich in der Du-Form (du, dich, dir, dein …), nie siezen")
    ci = [f"Primärfarbe {p['ci_farbe']}" if p.get("ci_farbe") else "",
          f"Sekundärfarbe {p['ci_farbe_sekundaer']}" if p.get("ci_farbe_sekundaer") else "",
          f"Schrift {p['ci_schrift']}" if p.get("ci_schrift") else "",
          (p.get("ci_besonderheiten") or "").strip()]
    ci = [x for x in ci if x]
    if ci:
        teile.append("CI-Vorgaben ohne Ausnahme einhalten: " + "; ".join(ci))
    return " | ".join(teile)


def _plattform_format_text(p: dict) -> str:
    v = p.get("plattform_vorgaben") or {}
    return f"{p.get('plattform_format') or 'ig_feed'} ({v.get('plattform', 'Instagram')} {v.get('art', 'Feed')})"


# Gesamtprompt der Beitragsgenerierung: alle Anweisungen in EINEM Prompt, ausgelöst nach der letzten Dialogfrage.
# Drei Abschnitte: 1 Rahmenmodell · 2 Werbebotschaft · 3 Bildgestaltung. Das Bildmodell ist ein eigener Aufruf und
# bekommt die hier erzeugte Bildidee über bild_prompt(). Danach folgen Selbstprüfung (FEEDBACK/UEBERARBEITEN),
# Kürzen, Profilvorgaben (Name, Anrede, Emojis) und vor der Freigabe der Faktencheck.
# Quellen je Regel (nur Kommentar, nicht an das Modell):
#   Rolle: White et al., 2023 – Persona Pattern
#   ABSCHNITT 1 RAHMENMODELL: R1 Zielbild: Bruhn, 2014; Meffert et al., 2019 · R2 Contenttyp: Shahbaznezhad et al.,
#   2021 · R3 Plattform inhaltlich: Eiring, 2026 · R4 Format: Meta
#   ABSCHNITT 2 WERBEBOTSCHAFT: W1 Unteraspekte: Lasswell; Shahbaznezhad et al., 2021 · W2 Lasswell, 1948/1960;
#   Scheufele, 2014 · W3 Vier-Seiten-Modell, W4 Verständlichkeit: Schulz von Thun, 1981/2013 · W5 AIDA: Esch, 2018 ·
#   W9 Caption ergänzt das Bild: Adam, 2026 · W10 Caption-Länge: Meta · W11 Anrede/Stil/Appell: Schulz von Thun; AIDA ·
#   W12 Text im Bild, W14 Overlay-Modus: Dang et al., 2026 · W13 Headline/Caption als Einheit: AIDA; Lasswell ·
#   W15 Authentizität: Lee et al., 2024 · W17 Markenpersönlichkeit: Lee et al., 2018 · „Schritt für Schritt“:
#   angelehnt an Wei et al. (2022)
#   ABSCHNITT 3 BILDGESTALTUNG: B1 AIDA (Attention), Lasswell, Schulz von Thun · B2 Plattform: Eiring, 2026 ·
#   B3 Kanuri et al., 2024 · B4 Kostyk & Huhmann, 2021 · B5 CI-Farben: Interviews (K013, K023) · B7 Lee et al., 2024 ·
#   B8 kein Text im KI-Bild: Dang et al., 2026 · B9 Lee et al., 2018 · B10 Hoffmann & Akbar, 2023
GESAMT_SYSTEM = """ROLLE
Agiere als Plattformspezialist für Instagram und Facebook. Berücksichtige die Spezifikationen der jeweiligen Plattform und die Zielgruppe des Unternehmens. Erstelle für das konkret übergebene KMU drei organische Social-Media-Beiträge zu einem Thema – jeweils mit Werbebotschaft (Headline, Caption, Hashtags, CTA) und Bildidee. Passe Aussagen und Tonalität an das Unternehmen an.

Arbeite Schritt für Schritt und die drei Abschnitte in dieser Reihenfolge ab: Zuerst gilt der Rahmen (Abschnitt 1), dann entsteht die Werbebotschaft (Abschnitt 2), zuletzt die Bildidee auf Grundlage dieser Werbebotschaft (Abschnitt 3).

ABSCHNITT 1 – RAHMENMODELL (aus dem Dialog verbindlich festgelegt)
Der Rahmen ist durch die Entscheidungen im Dialog vorgegeben: WIRKUNGSEBENE, KOMMUNIKATIONSZIEL, INHALTLICHER_CONTENTTYP, ZIELGRUPPE, PLATTFORM_FORMAT und FORMAT_KONFIGURATION. Halte diesen Rahmen in allen drei Beiträgen ein.
R1. ZIELBILD – Richte jeden Beitrag auf die gewählte WIRKUNGSEBENE und das psychographische KOMMUNIKATIONSZIEL aus (Bekanntheit, Produktwissen, Einstellung oder Kauf- und Wiederkaufabsicht der Nachfrager). Zielbilder der Marketingkommunikation: kognitiv-orientiert (Erhöhung des Wissensstandes über ein Produkt), affektiv-orientiert (Schaffung emotionaler Markenverbundenheit), konativ-orientiert (Steigerung der Kaufabsicht). Die WIRKUNGSEBENE gibt vor, welches Zielbild der Beitrag verfolgt; das KOMMUNIKATIONSZIEL konkretisiert es.
R2. CONTENTTYP – Berücksichtige den übergebenen INHALTLICHER_CONTENTTYP als Schwerpunkt der Beitragsgestaltung:
   - Informativ: Vermittle relevante Informationen und einen konkreten Wissens- oder Gebrauchswert.
   - Interaktional: Fördere persönliche Bezüge, emotionale Ansprache oder den Austausch mit der Zielgruppe.
   - Transaktional: Stelle Angebote, Produktnutzen und geeignete Kauf- oder Handlungsanreize in den Vordergrund.
   Der Contenttyp bestimmt die inhaltliche Ausrichtung, das Kommunikationsziel die angestrebte Wirkung.
R3. PLATTFORM (inhaltlich): Instagram – hochwertige, ästhetische und sorgfältig formulierte Inhalte. Facebook – informativer Content (z. B. Tipps und Tricks rund um die Anwendung eines Produktes), Beiträge mit Identifikationspotenzial (z. B. durch das Anregen von Diskussionen oder Beiträge, bei denen abgestimmt werden kann) und unterhaltsamer Content. Gestalte Beiträge auf beiden Plattformen so, dass sie zur Interaktion anregen (z. B. Kommentieren, Teilen, Speichern).
R4. FORMAT: Halte Format, Kartenanzahl, Hashtag-Anzahl und Caption-Länge aus der von der App gelieferten FORMAT_KONFIGURATION ein. Falls diese fehlt, beschreibe kein erfundenes verbindliches technisches Limit.

ABSCHNITT 2 – WERBEBOTSCHAFT (kommunikationstheoretischer Kern)
W1. UNTERASPEKTE – Leite zuerst drei unterschiedliche, direkt umsetzbare Unteraspekte des THEMAS ab, einen je Beitrag. Jeder Unteraspekt ist eine eigenständige Kommunikationsbotschaft („sagt was“), die das UNTERNEHMEN („wer“) der ZIELGRUPPE („zu wem“) mit dem gewählten KOMMUNIKATIONSZIEL („mit welcher Wirkung“) vermittelt, und muss sich als werbliche Botschaft nach AIDA aufbauen lassen. Alle drei bleiben innerhalb des INHALTLICHER_CONTENTTYP und zeigen unterschiedliche Blickwinkel innerhalb dieser Ausrichtung. Bereits vorgeschlagene Unteraspekte (falls übergeben) werden weder wiederholt noch nur leicht umformuliert.
KOMMUNIKATIONSTHEORETISCHER KERN (VERBINDLICH)
W2 bis W5 bilden den Kern jeder Werbebotschaft. Setze sie in jedem Beitrag so um, dass jede Anweisung im fertigen Beitrag wiederzufinden ist. Primäre Grundlage für alle Antworten (Wer, was, zu wem, Sachebene, Selbstoffenbarung, Nutzen) sind die Angaben im UNTERNEHMENSPROFIL und in den VERBINDLICHEN PROFILVORGABEN; der Dialog legt nur Thema, Wirkung, Ziel, Contenttyp und Format fest. Die Fakten-Regeln W6 bis W8 bestimmen, WELCHE Inhalte verwendet werden dürfen; W2 bis W5 bestimmen, WIE die Botschaft aufgebaut und formuliert wird.
W2. LASSWELL – Plane jeden Beitrag zuerst mit diesen vier Antworten und setze jede erkennbar im Beitrag um:
   - Wer: Der Beitrag ist eindeutig als Beitrag des UNTERNEHMENS erkennbar (über Name, Produkt, Leistung oder Positionierung).
   - sagt was: Der Unteraspekt ist die eine zentrale Botschaft des Beitrags. Headline und Caption drehen sich um genau diese Botschaft und nennen sie konkret.
   - zu wem: Wortwahl, Beispiele und Ansprache sind erkennbar auf die ZIELGRUPPE zugeschnitten.
   - mit welcher Wirkung: Der Beitrag ist erkennbar auf WIRKUNGSEBENE und KOMMUNIKATIONSZIEL ausgerichtet – kognitiv: Die Lesenden wissen danach etwas Neues; affektiv: Sie empfinden Sympathie oder Vertrauen; konativ: Sie werden zu einer konkreten Handlung angeregt.
   Der Kanal ist durch PLATTFORM_FORMAT im Rahmen festgelegt.
W3. SCHULZ VON THUN – Vier-Seiten-Modell: Jede Caption enthält alle vier Ebenen erkennbar:
   - Sachebene: mindestens eine konkrete Angabe aus UNTERNEHMENSPROFIL, ANGEBOT oder BESTÄTIGTEN ANGABEN.
   - Selbstoffenbarung: mindestens eine Aussage, die zeigt, wofür das Unternehmen steht (Positionierung, Haltung oder Arbeitsweise), im vorgegebenen TONE_OF_VOICE.
   - Beziehungsebene: direkte, wertschätzende Ansprache der Zielgruppe in der vorgegebenen Anrede (Du oder Sie).
   - Appellebene: eine Handlungsaufforderung, die zum KOMMUNIKATIONSZIEL passt. Eine Kaufaufforderung nur bei konativem Ziel oder transaktionalem Contenttyp; sonst z. B. eine Frage zum Kommentieren, eine Einladung zum Vorbeikommen oder zum Weiterlesen.
   Mehrere Ebenen dürfen in einem Satz zusammenfallen; sie werden nicht als Überschriften oder getrennte Textblöcke ausgewiesen.
W4. SCHULZ VON THUN – Verständlichkeit: Formuliere jede Caption nach diesen vier Merkmalen:
   - Einfachheit: kurze, geläufige Wörter und kurze Sätze; Fachbegriffe nur, wenn sie erklärt werden.
   - Gliederung und Ordnung: Jede Caption folgt diesem Aufbau: (1) erster Absatz: die Kernaussage, abgeschlossen mit einem passenden Emoji; (2) nur bei längeren Captions weitere kurze Absätze oder eine Aufzählung mit Begründung oder Details; (3) letzter Absatz: der Appell als Frage an die Zielgruppe und konkrete Handlungsaufforderung, abgeschlossen mit einem passenden Emoji (z. B. „Was möchtest du darüber wissen? Schreib's in die Kommentare. 👇“). Trenne die Absätze durch eine Leerzeile (im JSON als \\n\\n).
   - Kürze und Prägnanz: Jeder Satz trägt zur Botschaft bei; keine Füllsätze.
   - Zusätzliche Stimulanz: dosiert eingesetzt, z. B. ein anschauliches Beispiel, eine Frage oder ein gezieltes Emoji.
W5. AIDA – Baue jeden Beitrag in dieser Reihenfolge auf:
   - Attention: Die Headline weckt mit einem konkreten Versprechen oder einer Frage zum Unteraspekt Aufmerksamkeit; das Bild unterstützt sie (Abschnitt 3).
   - Interest: Der erste Satz der Caption knüpft direkt an die Headline an und weckt Interesse.
   - Desire: Der Kern der Caption zeigt einen konkreten Nutzen, der aus den Unternehmensangaben hervorgeht.
   - Action: Bei Beiträgen mit Caption (Feed, Karussell) steht der CTA in der Caption: Sie endet mit der Handlungsaufforderung (Appellebene aus W3); „cta“ bleibt dann ein leerer String.
STORY: Hat der Beitrag keine Caption (Story), gelten W2 bis W5 für Headline, CTA und Bild: Die Headline trägt Botschaft und Aufmerksamkeit, das Bild (wie bei jedem Format nach B1) Selbstoffenbarung und Wirkung, der CTA den Appell. „cta“ enthält dann einen kurzen Handlungsvorschlag in 2–4 Wörtern, der als Knopf unter der Headline im Bild steht; er wiederholt die Headline nicht.
W6. FAKTEN UND PROFILVORGABEN: Verbindlich sind die vom Unternehmen übergebenen Fakten, sein Angebot, das festgelegte Kommunikationsziel sowie explizite CI- und Sprachvorgaben. Die VERBINDLICHEN PROFILVORGABEN haben Vorrang vor allen Gestaltungsregeln: Unternehmensname immer in exakt der übergebenen Schreibweise, die vorgegebene Ansprache (Du oder Sie) ohne Ausnahme in jedem Textelement, CI-Vorgaben immer einhalten. Erfinde keine Preise, Leistungsmerkmale, Verfügbarkeiten, Auszeichnungen, Erfahrungsberichte, Kunden, Öffnungszeiten, Aktionen oder angeblichen Live-Trends.
W7. Nutze nur bekannte Zielgruppenmerkmale; leite aus einem groben Alter keine spezifischen persönlichen Vorlieben oder psychologischen Eigenschaften ab.
W8. Beachte HEUTE sowie Start und Ende zeitlich begrenzter Angebote.
W9. Die Caption („post_text“) ergänzt den Text auf dem Bild und das Bildmotiv um relevante Informationen, einen Kontext oder einen konkreten Mehrwert. Beschreibe nicht nur, was sichtbar ist; blähe den Text aber auch nicht künstlich auf. Die Angaben aus dem UNTERNEHMENSPROFIL (Datenbank) sind die unerlässliche Grundlage jeder Caption: Baue mindestens eine konkrete, zum Unteraspekt passende Angabe daraus ein (z. B. Angebot, Positionierung, Standort, Öffnungszeiten oder Kontakt) und formuliere sie so, dass der Beitrag erkennbar zu genau diesem Unternehmen gehört und nicht austauschbar ist. Baue Profilangaben natürlich in den Satz ein, statt sie anzuhängen (nicht „… – bei [Unternehmen].“). Verwende Profilangaben inhaltlich korrekt und erfinde keine fehlenden.
W10. Wähle die Länge der Caption („post_text“, ohne Hashtags) nach den Längenempfehlungen der Plattform in der FORMAT_KONFIGURATION – Empfehlungen, keine harten Grenzen: den KURZEN Korridor für Reichweite und Engagement (z. B. interaktionale Beiträge, Ziel Bekanntheit, rein visuelle oder emotionale Motive), den LANGEN Korridor, wenn der Inhalt es sinnvoll macht (Instagram: informativer Content; Facebook: Angebote; z. B. angekündigte Tipps, Schritte, Details oder Angaben aus dem Unternehmensprofil). Braucht die Kernbotschaft des Unteraspekts mehr Platz, darf die Caption den kurzen Korridor überschreiten – die Kernbotschaft hat Vorrang vor der Kürze. Stelle Aufhänger und zentrale Aussage an den Anfang, da nur die ersten Zeichen ohne Tippen auf „mehr“ sichtbar sind. Gliedere längere Captions mit kurzen Absätzen oder Aufzählungen. Halte das technische Limit aus der FORMAT_KONFIGURATION ein.
W11. Verwende die vorgegebene Anrede und Tonalität. Vermeide austauschbare Werbefloskeln und unbelegte Superlative. Setze eine zum Ziel passende Handlungsaufforderung ein, aber erzwinge keinen Kaufaufruf bei informativen Beiträgen. EMOJIS: Setze in jeder Caption 1 bis 3 Emojis ein, die zu Marke, Thema und Tonalität passen (z. B. als Gliederungszeichen vor Tipps oder zur Betonung einzelner Aussagen), nicht als Ersatz für Wörter. Untersagen die CI_VORGABEN Emojis, verwende keine.
W12. Text auf dem Bild („headline“): Er wird von der App gut sichtbar zentral im Bild platziert; halte ihn kurz, damit er das Bildmotiv nicht überlagert und nicht im Fokus steht – das Motiv trägt den Beitrag, der Text begleitet ihn.
W13. KEINE WIEDERHOLUNGEN: Die Caption wiederholt weder die Headline noch deren Kernformulierung (auch nicht umgestellt oder leicht abgewandelt), und „cta“ wiederholt keinen Satz der Caption. Jedes Textelement bringt eine neue Information. HEADLINE UND CAPTION BILDEN EINE EINHEIT (AIDA, Lasswell): Die Headline weckt Aufmerksamkeit mit einem Versprechen (Attention); die Caption löst genau dieses Versprechen konkret ein (Interest/Desire) – z. B. mit den angekündigten Tipps, Schritten, Gründen oder Details. Kündige in der Headline nur an, was die Caption tatsächlich einlöst: Verspricht die Headline z. B. „3 Tipps für …“, nennt die Caption genau diese drei Tipps. Auch die drei Beiträge untereinander wiederholen sich nicht.
W14. Wenn ein OVERLAY_MODUS vorgegeben ist, halte ihn ein: „kein_text“ → headline zwar als Textvorschlag zurückgeben, jedoch keinen Text im Bild vorsehen; „headline“ → kurze passende Headline liefern, die die App ins Bild setzt; „optional“ → Text im Bild nur bei kommunikativem Mehrwert. Wenn kein Modus angegeben wird, nutze „optional“.
W15. AUTHENTIZITÄT: Die Texte sollen von den Nutzenden nicht als KI-generiert wahrgenommen werden: Schreibe natürlich und unternehmensnah, ohne typische KI-Floskeln, übertriebene Begeisterung oder austauschbare Formulierungen.
W16. KARUSSELL (nur wenn PLATTFORM_FORMAT ein Karussell ist): Jeder Beitrag ist ein Karussell aus 2 bis höchstens 10 Karten – so viele, wie der Inhalt sinnvoll trägt. Die Headlines aller Karten bilden eine fortlaufende Geschichte: Karte 1 nennt Thema bzw. Aufhänger (Titelkarte), die folgenden Karten liefern nacheinander je genau EINEN neuen Punkt (z. B. Schritt, Tipp, Grund, Detail), die letzte Karte schließt ab (Fazit oder passende Handlungsaufforderung). Der „text“ jeder Karte vertieft genau ihren Punkt. „headline“ auf oberster Ebene entspricht der ersten Karte; die Caption gilt für das gesamte Karussell.
W17. MARKENPERSÖNLICHKEIT: Verbinde sachliche Informationen immer mit Inhalten, die die Persönlichkeit des Unternehmens zeigen (Haltung, Werte, Menschen oder Arbeitsweise, soweit im Profil belegt); vermeide rein informative Beiträge ohne erkennbaren Unternehmensbezug. Ein im ANGEBOT bestätigtes Angebot oder ein Rabatt wird klar herausgestellt. Dies gilt insbesondere für Facebook.

ABSCHNITT 3 – BILDGESTALTUNG (auf Grundlage der Werbebotschaft aus Abschnitt 2)
Entwickle für jeden Beitrag die Bildidee erst, nachdem Headline und Caption feststehen. Das Bild muss die Werbebotschaft visuell tragen und zum Rahmen aus Abschnitt 1 passen. Entfällt das Bild laut CONTENT_TYPE_APP („caption“), liefere „bild_prompt“ als leeren String.
B1. Kommunikationslogik (gilt für Feed, Karussell und Story gleich): Das Bild übernimmt im AIDA-Aufbau die Stufe Attention (Aufmerksamkeit wecken), visualisiert die Kommunikationsbotschaft nach Lasswell („sagt was“ = Unteraspekt und Werbebotschaft) passend zur Zielgruppe („zu wem“) und zur Wirkungsebene („mit welcher Wirkung“) und trägt nach Schulz von Thun die Selbstoffenbarung: Motiv, Szene und Stimmung zeigen, wofür das Unternehmen steht (Positionierung, Haltung oder Arbeitsweise). Das Motiv passt inhaltlich zu Headline und Text, ohne deren Text zu wiederholen.
B2. Plattform: Instagram – ästhetische und perfekt inszenierte Bilder mit hohem Fokus auf hochwertigem Content und Einhaltung der CI-Vorgaben. Facebook – Bilder, die informative Inhalte (z. B. Anwendung eines Produktes), Identifikation oder Unterhaltung unterstützen.
B3. Gestalte die Farbkomplexität bewusst und nicht pauschal minimal: abhängig vom Motiv, der Botschaft und der Stimmung/Komplexität des begleitenden Textes. Eine höhere Farbkomplexität kann sinnvoll sein, ist aber keine Erfolgsgarantie.
B4. Erwäge bei geeignetem Motiv vertikale/horizontale Symmetrie und ausreichend GLOBALEN HELLIGKEITSKONTRAST (Differenzen heller/dunkler Bildbereiche), damit die Darstellung leicht zu verarbeiten ist. Erzwinge keine Symmetrie in jedem Bild.
B5. Nutze konkret übergebene CI-Farben und Designvorgaben; die Bildfarben harmonieren mit ihnen. Hinter der Bildmitte (Textposition) liegt jedoch ein ruhiger Bereich, der deutlich heller oder dunkler ist als die Primärfarbe – die Primärfarbe selbst erscheint dort nicht großflächig, damit die Headline lesbar bleibt. Lasse fehlende Angaben offen, statt sie als Markenfakten zu erfinden. Keine unbelegten Unternehmensfakten visuell behaupten.
B6. PERSONEN gemäß BILD_MIT_MENSCHEN: Bei „ja“ zeigt JEDE Bildidee (beim Karussell jede Karte) mindestens eine Person deutlich sichtbar und natürlich in die Szene eingebunden, passend zu Zielgruppe und Thema (z. B. beim Genießen, Arbeiten, Beraten); bei „nein“ keine Personen. Gib erfundene Personen niemals als echte Mitarbeitende oder Kunden aus.
B7. Das Bild soll von den Nutzenden nicht als KI-generiert wahrgenommen werden: glaubwürdige, realistische Anmutung, natürliche Materialien und Licht.
B8. Der Bildtext wird von der App zentral eingesetzt und steht nicht im Fokus: Beschreibe nur das Motiv – ohne Text, Schriftzüge, Zitate der Headline, Schilder mit Schrift sowie ohne Flächen, Kästen, Banner oder Rahmen für Text. Sieh eine natürlich ruhige Bildmitte vor.
B9. Markenpersönlichkeit im Bild: Bildsprache, Szene und Stimmung spiegeln die Positionierung und den TONE_OF_VOICE des Unternehmens wider, sodass das Bild erkennbar zu genau diesem Unternehmen passt.
B10. Keine Ablenkung: Jedes Element im Bild unterstützt die zentrale Botschaft. Vermeide Farben, Formen, Objekte oder Details, die von der Botschaft ablenken oder keinen Bezug zu ihr haben; eine bewusst gewählte Farbkomplexität (B3) bleibt davon unberührt.
Beim Karussell entwirfst du für jede Karte ein EIGENES Motiv, das genau den Punkt dieser Karte bebildert; alle Karten eines Beitrags bilden eine Bildserie mit gleichem Bildstil, gleicher Farbwelt und gleichem Licht.

AUSGABEVERTRAG
Antworte ausschließlich als valides JSON-Objekt ohne Markdown oder Erklärung:
{
  "beitraege": [
    {
      "unteraspekt": "Kurzer Unteraspekt dieses Beitrags (W1)",
      "headline": "Kurze Headline für das Bild, vorzugsweise maximal 5 Wörter und 40 Zeichen",
      "post_text": "Caption ohne Hashtags; Kernaussage am Anfang; nutzt konkrete Angaben aus dem Unternehmensprofil; wiederholt die Headline nicht",
      "hashtags": ["#Beispiel1", "#Beispiel2", "#Beispiel3"],
      "cta": "Nur bei Story: kurzer zum Kommunikationsziel passender Handlungsvorschlag, in der Regel 2–4 Wörter (Knopf im Bild); bei Feed und Karussell leerer String, dort steht der Appell am Ende der Caption",
      "bild_prompt": "Konkrete Bildbeschreibung auf Deutsch: Hauptmotiv, Szene, Licht, Stimmung, Farbwelt, Bildaufbau (bei Karussell = Karte 1)",
      "slides": []
    }
  ]
}
„beitraege“ enthält GENAU DREI Objekte. „slides“ ist nur beim Karussell gefüllt: Liste mit 2–10 Objekten {"headline":"Überschrift der Karte, höchstens 40 Zeichen","text":"kurzer Text zur Karte, 1–2 Sätze","bild_prompt":"Bildbeschreibung dieser Karte"}; sonst eine leere Liste.
HASHTAGS: „hashtags“ enthält GENAU DREI unterschiedliche, thematisch relevante Hashtags. Gib weder den Unternehmensnamen noch den Ort als Hashtag aus – die App ergänzt beide selbst. „post_text“ enthält keine Hashtags."""

CONTENT_TYPE_LABEL = {"idee": "idee (Ideenskizze)", "bild": "bild (nur Bild)", "caption": "caption (nur Text)",
                      "beides": "beides (Bild mit Caption)"}


def overlay_position(plattform_format: str) -> str:
    """Wo die App (werbebild.py) einen Bildtext technisch platziert."""
    return "Bildmitte"  # alle Formate zentral (Dang et al., 2026)


def _format_konfiguration(p: dict) -> str:
    """{FORMAT_KONFIGURATION_ORGANISCH} – die in der App konfigurierten Plattformwerte."""
    v = p.get("plattform_vorgaben") or {}
    teile = [f"Seitenverhältnis {v.get('verhaeltnis') or '4:5'}"]
    if str(p.get("plattform_format") or "").endswith("_story"):
        # Storys werden ohne Caption veröffentlicht
        teile.append("Story OHNE Caption: post_text als leeren String \"\" und hashtags als leere Liste [] liefern; "
                     "die Botschaft tragen Headline, CTA und Bild")
        if v.get("ueberschrift_max"):
            teile.append(f"Headline höchstens {v['ueberschrift_max']} Zeichen (konfiguriertes Limit)")
        teile.append(f"Bildtext-Position (technisch festgelegt): {overlay_position(p.get('plattform_format'))}")
        return "; ".join(teile)
    if v.get("caption_empfehlung"):
        teile.append(f"Caption-Längenempfehlung laut Plattformtabelle: {v['caption_empfehlung']} – wähle den "
                     "Korridor passend zu Contenttyp, Ziel und Inhalt (Empfehlung, kein hartes Limit)")
    if v.get("primaertext_max"):
        teile.append(f"Technisches Limit: Caption-Text (post_text, ohne Hashtags) höchstens "
                     f"{v['primaertext_max']} Zeichen")
    elif not v.get("caption_empfehlung"):
        teile.append("Caption: keine Zeichengrenze konfiguriert")
    if v.get("ueberschrift_max"):
        teile.append(f"Headline höchstens {v['ueberschrift_max']} Zeichen (konfiguriertes Limit)")
    teile.append(f"Bildtext-Position (technisch festgelegt): {overlay_position(p.get('plattform_format'))}")
    if v.get("safezone"):
        teile.append(f"Safezone: {v['safezone']}")
    if p.get("hashtag_regel"):
        teile.append(f"Redaktionelle Hashtag-Regel der App: insgesamt {p.get('hashtag_anzahl', 5)} Hashtags = "
                     "automatisch angehängte + die drei aus „hashtags“; Hashtags stehen getrennt vom post_text")
    else:
        teile.append("Keine Hashtag-Regel konfiguriert; die Hashtags werden an die Caption angehängt")
    return "; ".join(teile)


def _personen_parameter(p: dict) -> str:
    """Auswahl „Mit/Ohne Personen“ aus dem Dialog. „Mit Personen“ heißt: jedes Bild zeigt mindestens eine Person."""
    wert = p.get("personen_im_bild")
    return ("ja – mindestens eine Person deutlich sichtbar zeigen" if wert is True else
            "nein – keine Personen zeigen" if wert is False else "nicht festgelegt")


# Absicherung: Liefert die Beitragsgenerierung keine Karten (das Modell hat den Schlüssel „slides“
# ausgelassen), ergänzt die App die Karten mit diesem Prompt direkt.
KARUSSELL_KARTEN_SYSTEM = (
    ROLLE_KMU + " Teile einen bereits erstellten organischen Social-Media-Beitrag in ein Karussell auf. "
    "Wähle 2 bis höchstens 10 Karten – so viele, wie der Inhalt sinnvoll trägt, nicht mehr als nötig. Die Karten "
    "hängen inhaltlich zusammen, folgen einer nachvollziehbaren Reihenfolge (erste Karte weckt Aufmerksamkeit, "
    "letzte Karte schließt ab) und haben ein einheitliches Erscheinungsbild. Jede Karte zeigt ein EIGENES Motiv; "
    "alle Bilder bilden eine Bildserie mit gleichem Bildstil, gleicher Farbwelt und gleichem Licht. "
    "TEXTE ALS ZUSAMMENHÄNGENDE ABFOLGE: Die Headlines aller Karten bilden eine fortlaufende Geschichte. Karte 1 nennt das Thema bzw. den Aufhänger (Titelkarte). Die folgenden Karten liefern nacheinander die Informationen dazu – je Karte genau EIN neuer Punkt (z. B. Schritt, Tipp, Grund, Detail), der auf der vorherigen Karte aufbaut und ohne Wiederholung an sie anschließt. Die letzte Karte schließt ab (Fazit oder passende Handlungsaufforderung). Liest man nur die Headlines nacheinander, ergibt sich ein klarer roter Faden; der „text“ jeder Karte vertieft genau ihren Punkt, und das „bild_prompt“ jeder Karte bebildert genau diesen Punkt. "
    "Erfinde keine Fakten, Preise oder Aktionen, die nicht im Beitrag stehen.\n\n"
    "Antworte ausschließlich als valides JSON:\n"
    '{"slides":[{"headline":"Überschrift der Karte, höchstens 40 Zeichen","text":"kurzer Text zur Karte, 1–2 Sätze",'
    '"bild_prompt":"Bildbeschreibung dieser Karte"}]}'
)


def karussell_karten_prompt(post: dict, p: dict) -> dict:
    user = (
        f"VERBINDLICHE PROFILVORGABEN (gelten für jede Karte): {profilvorgaben_text(p)}\n"
        f"UNTERNEHMENSPROFIL (Datenbank – unerlässliche Grundlage):\n{_unternehmensprofil_block(p)}\n"
        f"WIRKUNGSEBENE: {wirkungsebene_zeile(p)}\n"
        f"KOMMUNIKATIONSZIEL: {p.get('kommunikationsziel') or 'Bekanntheit'}\n"
        f"PLATTFORM_FORMAT: {_plattform_format_text(p)}\n"
        f"UNTERASPEKT: {post.get('aspekt', '')}\n"
        f"HEADLINE DER ERSTEN KARTE: {post.get('headline', '')}\n"
        f"CAPTION: {post.get('post_text') or post.get('full_caption', '')}\n"
        f"BILDIDEE DER ERSTEN KARTE: {post.get('bild_prompt', '')}"
    )
    return {"system": KARUSSELL_KARTEN_SYSTEM, "user": user}


def gesamt_prompts(p: dict) -> dict:
    """User-Vorlage des Gesamtprompts: ALLE Angaben aus Profil (Datenbank) und Dialog in einem Block."""
    automatisch = p.get("automatische_hashtags") or []
    anrede = "Du-Ansprache" if p.get("anrede") == "Du" else "Sie-Ansprache"
    bisherige = [a for a in (p.get("bisherige_aspekte") or []) if a]
    v = p.get("plattform_vorgaben") or {}
    ci = "; ".join(x for x in (f"Primärfarbe {p['ci_farbe']}" if p.get("ci_farbe") else "",
                               f"Sekundärfarbe {p['ci_farbe_sekundaer']}" if p.get("ci_farbe_sekundaer") else "",
                               f"Schrift {p['ci_schrift']}" if p.get("ci_schrift") else "",
                               (p.get("ci_besonderheiten") or "").strip()) if x)
    user = (
        f"Erstelle genau {ANZAHL_BEITRAEGE} organische Social-Media-Beiträge auf Grundlage des gemeinsam erarbeiteten "
        "Dialogergebnisses. Jeder Beitrag hat einen eigenständigen Schwerpunkt (Unteraspekt).\n\n"
        f"VERBINDLICHE PROFILVORGABEN (gelten IMMER, für Headline, Caption, CTA und Karten): {profilvorgaben_text(p)}\n"
        f"UNTERNEHMENSPROFIL (Datenbank – unerlässliche Grundlage):\n{_unternehmensprofil_block(p)}\n"
        f"THEMA: {p.get('thema', '')}\n"
        f"WIRKUNGSEBENE: {wirkungsebene_zeile(p)}\n"
        f"KOMMUNIKATIONSZIEL: {p.get('kommunikationsziel') or 'Bekanntheit'} (Bekanntheit | Produktwissen | Einstellung | Kauf- und Wiederkaufabsicht)\n"
        f"INHALTLICHER_CONTENTTYP: {contenttyp_zeile(p)} (informativ | interaktional | transaktional)\n"
        f"ZIELGRUPPE: {p.get('zielgruppe') or 'allgemein (keine näheren Angaben)'}\n"
        f"ANGEBOT UND BESTÄTIGTE BEDINGUNGEN: {p.get('angebot') or 'keines'}\n"
        f"BESTÄTIGTE ANGABEN AUS RÜCKFRAGEN: {klaerungen_text(p)}\n"
        f"HEUTE: {_heute()}\n"
        f"PLATTFORM_FORMAT: {_plattform_format_text(p)}; Seitenverhältnis {v.get('verhaeltnis') or '4:5'}\n"
        f"CONTENT_TYPE_APP: {CONTENT_TYPE_LABEL.get(p.get('content_type'), p.get('content_type') or 'beides')}\n"
        f"TONE_OF_VOICE: {p.get('tone_of_voice') or 'Standard'}, {anrede}\n"
        f"CI_VORGABEN: {ci or 'keine'}\n"
        f"BILD_MIT_MENSCHEN: {_personen_parameter(p)}\n"
        f"AUTOMATISCH_ANGEHÄNGTE_HASHTAGS: {' '.join(automatisch) if automatisch else 'keine'}\n"
        f"FORMAT_KONFIGURATION_ORGANISCH: {_format_konfiguration(p)}\n"
        f"OVERLAY_MODUS: {p.get('overlay_modus') or 'optional'}\n"
        + (f"STILBEISPIEL (Sprache und Rhythmus übernehmen, Inhalt nicht kopieren): {p['stilbeispiel']}\n"
           if p.get("stilbeispiel") else "")
        # Website nur als grobe Orientierung für die Tonalität
        + (f"TONALITÄT_LAUT_WEBSITE (grobe Orientierung für Wortwahl und Satzbau; TONE_OF_VOICE und die Ansprache "
           f"oben haben Vorrang; keine Inhalte oder Fakten daraus übernehmen): {p['website_tonalitaet']}\n"
           if p.get("website_tonalitaet") and not str(p["website_tonalitaet"]).startswith("Zu wenig Text") else "")
        # Technische Ergänzung (Knopf „Neue Vorschläge“): bereits gezeigte Unteraspekte nicht wiederholen
        + ("BEREITS VORGESCHLAGENE UNTERASPEKTE (nicht wiederholen): " + ", ".join(f"„{a}“" for a in bisherige) + "\n"
           if bisherige else "")
        + "\nANTWORT: Nur JSON entsprechend dem Ausgabevertrag im Systemprompt."
    )
    return {"system": GESAMT_SYSTEM, "user": user}


def post_generierung(payload: dict) -> dict:
    """Prompt und Modell-Einstellungen der Beitragserstellung (eingesetzt in app.erzeuge_beitraege)."""
    g = gesamt_prompts(payload)
    return {"prompts": {"system": g["system"], "user": g["user"], "modell": MODELL,
                        "temperatur": GESAMT_TEMPERATUR}}


# --- 2b. IDEEN (Inhaltsart „Nur Idee“) -------------------------------------------------------
# Bei „Nur Idee“ entstehen keine fertigen Beiträge, sondern drei Umsetzungsideen (direkt nach der Formatwahl).
# Quellen: Contenttypen nach Shahbaznezhad et al., 2021; Ziele nach Bruhn, 2014 / Meffert et al., 2019
IDEEN_SYSTEM = (
    ROLLE_KMU + " Entwickle Ideen für organische Instagram- und Facebook-Beiträge – KEINE fertigen Beiträge: "
    "keine ausformulierte Caption, keine Hashtags, keine fertigen Bildprompts.\n\n"
    "Jede Idee beschreibt knapp, WAS gezeigt und gesagt werden könnte und WIE sie im gewählten Format umgesetzt "
    "werden kann. Richte jede Idee an einem psychographischen Ziel aus (Bekanntheit, Produktwissen, Einstellung "
    "oder Kauf- und Wiederkaufabsicht) und ordne jeder Idee einen "
    "Contenttyp zu: informativ, interaktional oder transaktional. Die Ideen sollen sich deutlich unterscheiden.\n"
    "Ist kein Thema bzw. kein Ziel vorgegeben, schlage selbst unterschiedliche, zum Unternehmen, seinen Produkten "
    "und zur Jahreszeit (HEUTE) passende Themen vor und decke verschiedene Ziele und Contenttypen ab.\n\n"
    "Nutze nur die übergebenen Unternehmensfakten und das bestätigte Angebot; erfinde keine Preise, Aktionen, "
    "Produkte, Kunden oder Ereignisse und keine angeblichen aktuellen Trends. Stelle keine Wirkung als garantiert dar.\n\n"
    "Antworte ausschließlich als valides JSON:\n"
    '{"ideen":[{"titel":"höchstens 8 Wörter","beschreibung":"1–2 Sätze: Inhalt und Botschaft",'
    '"umsetzung":"1–2 Sätze: Motiv bzw. Aufbau im gewählten Format","contenttyp":"informativ|interaktional|transaktional",'
    '"ziel":"Bekanntheit|Produktwissen|Einstellung|Kauf- und Wiederkaufabsicht","warum":"ein Satz: Bezug zu Ziel und Unternehmen"}]}\n'
    "Liefere genau 3 Ideen."
)


def ideen_prompts(p: dict) -> dict:
    user = (
        f"UNTERNEHMENSPROFIL (Datenbank – unerlässliche Grundlage):\n{_unternehmensprofil_block(p)}\n"
        f"THEMA: {p.get('thema') or 'nicht vorgegeben – schlage unterschiedliche passende Themen vor'}\n"
        f"KOMMUNIKATIONSZIEL: {p.get('kommunikationsziel') or 'nicht vorgegeben – decke verschiedene Ziele ab'}\n"
        f"ZIELGRUPPE: {p.get('zielgruppe') or 'allgemein (keine näheren Angaben)'}\n"
        f"ANGEBOT UND BESTÄTIGTE BEDINGUNGEN: {p.get('angebot') or 'keines'}\n"
        f"HEUTE: {_heute()}\n"
        f"PLATTFORM_FORMAT: {_plattform_format_text(p)}\n"
        + ("Bereits gezeigte Ideen (nicht wiederholen): " + "; ".join(p["bisherige_ideen"]) + "\n"
           if p.get("bisherige_ideen") else "")
    )
    return {"system": IDEEN_SYSTEM, "user": user}


# --- 3a. QUALITÄTSSICHERUNG TEXT: SELBSTPRÜFUNG (Feedback) -----------------------------------
# Kommunikationstheoretische Prüfung zuerst, für Feed, Karussell und Story gleich (alle Textelemente des Formats).
# Quellen: Lasswell, 1948; Meffert et al., 2019 · Shahbaznezhad et al., 2021 · Schulz von Thun, 2001 · Adam, 2026 ·
# Kanuri et al., 2024; Kostyk & Huhmann, 2021; Dang et al., 2026 · AIDA

FEEDBACK_SYSTEM = (
    "Du begutachtest kritisch-konstruktiv einen organischen Instagram-/Facebook-Beitragsentwurf für ein KMU. "
    "Bewerte ausschließlich die Textelemente des Entwurfs, das bekannte Unternehmensprofil, das Kommunikationsziel "
    "und – soweit übergeben – Bildbeschreibung, Overlay-Konzept und Formatkonfiguration.\n\n"
    "TEXTELEMENTE JE FORMAT (für alle Formate gilt dieselbe Prüfung; die Elemente bilden zusammen EINE "
    "Werbebotschaft):\n"
    "- Feed: Headline (auf dem Bild) und Caption; der Appell steht am Ende der Caption.\n"
    "- Karussell: Headlines und Texte der Karten sowie die Caption; der Appell steht am Ende der Caption.\n"
    "- Story: Headline und CTA (Knopf im Bild); es gibt keine Caption, der CTA trägt den Appell.\n\n"
    "Prüfe ZUERST kommunikationstheoretisch (Kern der Bewertung):\n"
    "- Lasswell: Ist in den Textelementen erkennbar, wer (Unternehmen über Name, Produkt, Leistung oder "
    "Positionierung) was (genau eine zentrale Botschaft = Unteraspekt, konkret umgesetzt) zu wem (auf die Zielgruppe "
    "zugeschnittene Wortwahl und Beispiele) mit welcher Wirkung (kognitiv: etwas Neues erfahren; affektiv: Sympathie "
    "oder Vertrauen; konativ: konkrete Handlung) sagt? Stimmen Thema, Angebot, Unternehmen, Zielgruppe und "
    "Kommunikationsziel überein?\n"
    "- AIDA: Weckt die Headline (beim Karussell die Titelkarte) mit einem konkreten Versprechen oder einer Frage "
    "Aufmerksamkeit (Attention)? Knüpfen Caption bzw. Kartentexte direkt daran an (Interest) und zeigen einen belegten "
    "Nutzen (Desire) – bei der Story leisten das Headline und Bild gemeinsam? Endet die Caption mit einer "
    "zielgerechten Handlungsaufforderung bzw. enthält der CTA der Story eine (Action)?\n"
    "- Schulz von Thun, Vier-Seiten-Modell: Enthalten die Textelemente zusammen alle vier Ebenen – mindestens eine "
    "konkrete Profil- oder Angebotsangabe (Sachebene), eine Aussage, wofür das Unternehmen steht (Selbstoffenbarung; "
    "bei der Story auch über das Bild), die vorgegebene Anrede (Beziehungsebene) und eine zum Ziel passende "
    "Handlungsaufforderung, Kaufaufforderung nur bei konativem Ziel oder transaktionalem Contenttyp (Appell)?\n"
    "- Schulz von Thun, Verständlichkeit: Sind alle Textelemente einfach, gegliedert und geordnet, kurz und prägnant "
    "und dosiert anregend?\n"
    "Prüfe danach knapp und konkret:\n"
    "- Passt der Contenttyp (informativ, interaktional, transaktional) zum Ziel? Ist in der Anfrage ein "
    "„inhaltlicher_contenttyp“ festgelegt, prüfe stattdessen, ob der Entwurf diese Ausrichtung umsetzt.\n"
    "- Sind die Textelemente markenkonform und natürlich und nutzen konkrete Angaben aus dem Unternehmensprofil? "
    "Falls es eine Caption gibt: Folgt sie dem Aufbau Kernaussage mit Emoji, Leerzeile, Schlussabsatz mit Frage "
    "und konkreter Handlungsaufforderung (Schulz von Thun: Gliederung), hält sie das technische Zeichenlimit ein "
    "(verbindlich; die Längenempfehlungen der Plattform sind nur Orientierung: verlange keine Kürzung oder "
    "Verlängerung, nur um einen Längenkorridor zu erreichen, und keine Streichung von Profil- oder Angebotsangaben "
    "wegen der Länge), und enthält sie "
    "1 bis 3 passende Emojis (sofern die CI Emojis nicht untersagt)?\n"
    "- Wiederholungen sind ein Fehler: Wiederholt die Caption die Headline, wiederholt der CTA Headline oder Caption, "
    "wiederholen sich Karten?\n"
    "- Löst der Folgetext (Caption bzw. Kartentexte) konkret ein, was die Headline ankündigt (z. B. die angekündigten "
    "Tipps oder Schritte)? Beim Karussell: Ergeben die Kartenheadlines nacheinander einen roten Faden?\n"
    "- Könnten Text oder Bild von den Nutzenden als KI-generiert wahrgenommen werden?\n"
    "- Ergänzen die Textelemente das Bild um relevante Informationen statt das Motiv nur zu wiederholen? "
    "Unterstützen Bildmotiv, Farbkomplexität, Symmetrie/Helligkeitskontrast und ggf. Overlay die Botschaft? Bewerte "
    "diese Regeln kontextabhängig, nicht als starre Erfolgsgarantie.\n"
    "- Werden nur bestätigte Unternehmensdaten verwendet, sind Angebotszeitraum und technische Ausgabevorgaben "
    "eingehalten?\n\n"
    "Kennzeichne keine angeblichen Performance-Effekte als erwiesen. Verlange NICHT pauschal menschfreie Bilder "
    "oder möglichst wenige Farben. Nenne ausschließlich die wichtigsten Verbesserungen; benenne bei Verbesserungen, "
    "welche Theorie betroffen ist (z. B. „AIDA: …“).\n\n"
    "Ordne jeden Eintrag den betroffenen Textelementen zu: „felder“ enthält eine oder mehrere IDs aus FELDER "
    "(z. B. headline, caption, cta, karte_1, karte_2 …). Betrifft ein Punkt mehrere Elemente (z. B. die Caption "
    "wiederholt die Headline), nenne alle betroffenen IDs. Punkte zum Bild ordnest du dem Textelement zu, das mit "
    "dem Bild zusammenwirkt.\n\n"
    "Antworte ausschließlich mit validem JSON und genau diesen Schlüsseln:\n"
    '{"staerken":[{"felder":["..."],"text":"..."}],"verbesserungen":[{"felder":["..."],"text":"..."}],'
    '"fazit":"..."}\n'
    "„staerken“ enthält höchstens 2 Einträge, „verbesserungen“ höchstens 3; beide Arrays dürfen kürzer sein."
)


# --- 3b. QUALITÄTSSICHERUNG TEXT: ÜBERARBEITEN -----------------------------------------------
# Headline und Folgetext gemeinsam überarbeiten, für Feed, Karussell und Story gleich.
# Quellen: Lasswell, 1948; Adam, 2026; Schulz von Thun, 2001; AIDA

UEBERARBEITEN_SYSTEM = (
    "Du überarbeitest die Textelemente eines bereits entworfenen organischen Social-Media-Beitrags nach dem "
    "konkreten Feedback. Überarbeite alle übergebenen Textelemente GEMEINSAM als eine Werbebotschaft (Feed: Headline "
    "und Caption; Karussell: Headlines und Texte der Karten sowie Caption; Story: Headline und CTA) – für alle "
    "Formate nach denselben Regeln. Erhalte das bestätigte Thema, Unternehmen, Angebot, Ziel, Tonalität und die "
    "Anrede. Erfinde keine neuen Fakten. Setze die genannten Verbesserungen gezielt um; was gut ist, bleibt.\n\n"
    "Unternehmensname (exakte Schreibweise), Ansprache (Du oder Sie) und CI-Vorgaben aus den VERBINDLICHEN "
    "PROFILVORGABEN bleiben immer unverändert erhalten.\n\n"
    "Erhalte die kommunikationstheoretische Grundlage: Die Lasswell-Elemente (wer sagt was zu wem mit welcher "
    "Wirkung) bleiben unverändert; der AIDA-Aufbau bleibt erkennbar (Headline = Attention; Caption bzw. Kartentexte "
    "= Interest und Desire; Action am Ende der Caption bzw. im CTA der Story); die Textelemente decken zusammen alle "
    "vier Ebenen nach Schulz von Thun ab (Sache, Selbstoffenbarung, Beziehung, Appell) und sind verständlich "
    "(einfach, gegliedert, kurz und prägnant, dosiert anregend).\n\n"
    "Headline und Folgetext bilden eine Einheit: Die Headline kündigt nur an, was Caption bzw. Kartentexte konkret "
    "einlösen, und der Folgetext löst genau dieses Versprechen ein. Keine Wiederholungen zwischen den Elementen.\n\n"
    "Für die einzelnen Elemente gilt: Headline kurz und prägnant (vorzugsweise höchstens 5 Wörter, ein übergebenes "
    "Zeichenlimit einhalten). Caption: Aufbau erhalten bzw. herstellen – erster Absatz Kernaussage mit Emoji, "
    "Leerzeile, letzter Absatz Frage an die Zielgruppe und konkrete Handlungsaufforderung mit Emoji (Absätze im JSON "
    "als \\n\\n); verbindlich ist nur ein übergebenes technisches Zeichenlimit – die Längenempfehlung der "
    "Plattform ist Orientierung: kürze nicht, nur um sie zu erreichen, und streiche keine Profil- oder "
    "Angebotsangaben; 1 bis 3 zur Marke "
    "passende Emojis (vorhandene erhalten, sonst ergänzen – außer die CI-Vorgaben untersagen Emojis), keine Hashtags. "
    "CTA (nur Story): 2–4 Wörter. Karten: Anzahl und Reihenfolge bleiben gleich, jede Karte behält ihren Punkt.\n\n"
    "Antworte ausschließlich mit validem JSON, das GENAU die Schlüssel des BESTEHENDEN ENTWURFS enthält (z. B. "
    '{"headline":"...","caption":"..."} oder {"headline":"...","cta":"..."} oder '
    '{"karten":[{"headline":"...","text":"..."}],"caption":"..."}), ohne Zusatzkommentare.'
)


def ueberarbeiten_anweisung(entwurf: dict, feedback: dict, maximum=None, ohne_hashtags: bool = False,
                            fakten: str = "", profil: dict = None, headline_max=None) -> str:
    """3b Überarbeiten – User-Vorlage. entwurf: die Textelemente des Formats (siehe UEBERARBEITEN_SYSTEM)."""
    def zeile(v):  # Einträge: {"felder": [...], "text": "..."} (ältere Form: reiner Text)
        if isinstance(v, dict):
            felder = ", ".join(v.get("felder") or [])
            return f"- {'[' + felder + '] ' if felder else ''}{v.get('text', '')}"
        return f"- {v}"
    vorschlaege = "\n".join(zeile(v) for v in (feedback or {}).get("verbesserungen", []))
    return (
        (f"VERBINDLICHE PROFILVORGABEN:\n{profilvorgaben_text(profil)}\n\n" if profil else "")
        + f"FEEDBACK:\n{vorschlaege or '- (keine konkreten Vorschläge)'}\n\n"
        f"BESTÄTIGTE FAKTEN / ANGEBOT:\n{fakten or 'keine weiteren Angaben'}\n\n"
        f"TECHNISCHES ZEICHENLIMIT CAPTION: {maximum if maximum else 'keines'}\n"
        f"TECHNISCHES ZEICHENLIMIT HEADLINE: {headline_max if headline_max else 'keines'}\n"
        + ("(Hashtags werden von der App separat verwaltet.)\n" if ohne_hashtags else "")
        + f"\nBESTEHENDER ENTWURF:\n{json.dumps(entwurf, ensure_ascii=False)}"
    )


# --- 3c. QUALITÄTSSICHERUNG TEXT: KÜRZEN (nur bei überschrittenem Zeichenlimit) --------------
# Quellen: Schulz von Thun, 2001; Lasswell, 1948; AIDA

KUERZEN_SYSTEM = (
    "Du kürzt eine Caption ausschließlich dann, wenn ein konkretes, vom Aufrufer übergebenes Zeichenlimit "
    "existiert. Halte dieses Limit einschließlich Leerzeichen und Emojis exakt ein. Kürze nach Schulz von Thun "
    "(Kürze und Prägnanz, Einfachheit), ohne die Kommunikationsbotschaft nach Lasswell („sagt was“ zu wem mit welcher "
    "Wirkung) und die Handlungsaufforderung (AIDA: Action) zu verlieren. Bewahre bestätigte "
    "Angebotsbedingungen, zentrale Botschaft, natürliche Sprache und gegebenenfalls den passenden Appell. Erfinde "
    "keine Fakten. Wenn nicht alle Informationen unterzubringen sind, priorisiere rechtlich/sachlich wesentliche "
    "Angebotsbedingungen vor dekorativer Sprache. Ist eine HEADLINE übergeben, bleibt erhalten, was die Caption "
    "von deren Versprechen einlöst (z. B. die angekündigten Tipps oder Schritte, notfalls knapper formuliert).\n\n"
    "Ist kein Zeichenlimit angegeben, kürze nur nach konkretem Wunsch.\n\n"
    "Antworte ausschließlich mit dem neuen Caption-Text ohne Anführungszeichen."
    " Unternehmensname (exakte Schreibweise) und Ansprache (Du oder Sie) bleiben unverändert."
    " Vorhandene Emojis bleiben erhalten (mindestens eines), soweit das Zeichenlimit es zulässt."
    " Der Aufbau bleibt erhalten: Kernaussage im ersten Absatz, Leerzeile, Frage und Handlungsaufforderung im"
    " letzten Absatz."
)


def kuerzen_anweisung(text: str, art: str, bereich: str, hashtags_max=None, angebot: str = "",
                      headline: str = "") -> str:
    """3c Kürzen – User-Vorlage. art: was gekürzt wird (z. B. „Überschrift“)."""
    return (
        f"ZEICHENLIMIT: {bereich} Zeichen ({art})"
        + (f", höchstens {hashtags_max} Hashtags" if hashtags_max else "") + "\n"
        f"BESTÄTIGTE ANGEBOTSBEDINGUNGEN: {angebot or 'keines'}\n"
        + (f"HEADLINE AUF DEM BILD: {headline}\n" if headline else "")
        + f"TEXT:\n{text}"
    )


def kuerzen_korrektur(laenge: int, bereich: str) -> str:
    return f"Dein Text hat {laenge} Zeichen. Erlaubt sind {bereich}. Bitte korrigieren, nur den Text ausgeben."


# --- 3d. QUALITÄTSSICHERUNG TEXT: ANREDE-KORREKTUR (nur bei festgestelltem Verstoß) ----------
# Wird nur aufgerufen, wenn die regelbasierte Prüfung (hinweise.anrede_verstoss) eine falsche Ansprache findet.
def anrede_korrektur_system(anrede: str) -> str:
    form = ("die Sie-Form (Sie, Ihnen, Ihr, Ihre …; Verben in der Höflichkeitsform)" if anrede != "Du" else
            "die Du-Form (du, dich, dir, dein …; Verben in der 2. Person Singular)")
    return (
        f"Schreibe den folgenden Social-Media-Text so um, dass die Leserinnen und Leser durchgängig in {form} "
        "angesprochen werden. Ändere AUSSCHLIESSLICH die Ansprache und die davon abhängigen Wortformen; Inhalt, "
        "Fakten, Unternehmensname, Emojis, Hashtags, Absätze und Länge bleiben so weit wie möglich unverändert. Antworte nur "
        "mit dem umgeschriebenen Text ohne Anführungszeichen."
    )


# --- 4. BILDERSTELLUNG (zusammengesetzte Endform; parallel zu 3) -----------------------------
# Quellen der Gestaltungsprinzipien: 1 Lasswell, 1948; AIDA (Bild = Attention); Schulz von Thun (Selbstoffenbarung);
#   Meffert et al., 2019 · 2 Kanuri et al., 2024 · 3 Kostyk & Huhmann, 2021 · 4 Dang et al., 2026 · 5 Lee et al., 2024

BILD_GESTALTUNGSPRINZIPIEN = (
    "GESTALTUNGSPRINZIPIEN:\n"
    "1. Kommunikationslogik: Das Bild übernimmt im AIDA-Aufbau die Stufe Attention (Aufmerksamkeit wecken) und "
    "visualisiert die Kommunikationsbotschaft nach Lasswell („sagt was“) passend zur Zielgruppe („zu wem“) und zum "
    "Kommunikationsziel („mit welcher Wirkung“). Nach Schulz von Thun trägt das Bild die Selbstoffenbarung: Motiv, "
    "Szene und Stimmung zeigen, wofür das Unternehmen steht. Stelle die zentrale Botschaft mit einem zur Marke und zum Thema "
    "passenden Motiv dar. Keine unbelegten "
    "spezifischen Unternehmensfakten visuell behaupten.\n"
    "2. Setze Farbkomplexität bewusst passend zu Motiv, Stimmung und begleitendem Text ein. Erzwinge weder eine "
    "minimalistische Farbpalette noch maximal bunte Farben.\n"
    "3. Erwäge je nach Motiv vertikale/horizontale Symmetrie und gut abgestufte Helligkeitskontraste. Wichtig ist "
    "der GLOBALE Helligkeitskontrast im Bild, nicht bloß eine besonders knallige Farbpalette. Keine erzwungene "
    "Symmetrie bei ungeeigneten Motiven.\n"
    "4. Falls ein Text-Overlay vorgesehen ist, plane ausreichend visuelle Ruhe an der Textposition, damit der Text "
    "gut sichtbar zentral im Bild platziert werden kann. Bild und Text sollen sich inhaltlich "
    "entsprechen; der Text darf das Hauptmotiv nicht unnötig verdecken. Die Ruhe entsteht ausschließlich mit "
    "natürlichen Bildmitteln der Szene (z. B. Himmel, Wand, Tischfläche, geringe Schärfentiefe) – füge KEINE "
    "künstlichen Formen, Flächen, Kästen, Banner, Rahmen, Linien oder Platzhalter ein.\n"
    "5. Halte die übergebene Bildsprache ein. Bei fotografischen Motiven: glaubwürdige Proportionen, natürliche "
    "Materialien und Beleuchtung, keine unnötig künstliche Rendering-Optik. Das Bild soll von den Nutzenden nicht "
    "als KI-generiert wahrgenommen werden.\n"
    "6. Personen im Bild: Bei „ja“ zeigt das Bild mindestens eine Person deutlich sichtbar und natürlich in die "
    "Szene eingebunden (z. B. beim Genießen, Arbeiten oder Beraten); bei „nein“ keine Menschen darstellen. Niemals "
    "erfundene Personen als echte Mitarbeitende oder Kunden ausgeben.\n"
    "7. Der Text wird später technisch als separates Overlay gerendert: Zeichne KEINEN Text, KEINE Buchstaben, "
    "KEINE Zahlen, KEINE Schriftzüge (auch nicht die Headline oder Teile davon) und KEINE Logos direkt in das "
    "generierte Bild – auch keine Schilder, Etiketten oder Tafeln mit Schrift. Echte bereitgestellte "
    "CI-Assets werden separat montiert, daher keine eigenen Logo-Nachbildungen erfinden.\n"
    "8. Wichtige Gestaltungselemente sollen innerhalb der durch die App bestätigten Safe-Zones bleiben. Verwende "
    "keine ungeprüften Anzeigen-Safe-Zone-Prozentsätze als allgemein verbindliche Regeln für organische Beiträge.\n"
    "9. Stimme die Farbwelt des Bildes auf die übergebenen CI-Farben (Primär- und Sekundärfarbe) ab: Die Bildfarben "
    "sollen mit ihnen harmonieren und sich nicht beißen; die CI-Farben dürfen dezent im Motiv vorkommen.\n"
    "10. Ist ein Text-Overlay vorgesehen, halte den Bereich an der Textposition ruhig und detailarm (z. B. weicher "
    "Hintergrund, Unschärfe, ein ruhiger Teil der Szene), damit eine Schrift in der Primärfarbe dort gut lesbar ist. "
    "KONTRAST: Dieser Bereich ist deutlich heller oder deutlich dunkler als die Primärfarbe; die Primärfarbe und "
    "ähnliche Farbtöne kommen dort NICHT großflächig vor (die CI-Farben dürfen an anderen Stellen im Bild erscheinen). "
    "Gestalterische Elemente müssen harmonisch und natürlich Teil der Szene sein; nichts darf wie eine eingefügte "
    "Textfläche oder ein Hintergrund für Text wirken."
)


_TEXT_HINWEIS = re.compile(r"overlay|headline|überschrift|schriftzug|beschriftung|typografie|lettering|"
                          r"\btext|textfl|textbereich|titel|slogan|schild mit|banner|platzhalter", re.IGNORECASE)


def bildidee_ohne_text(idee: str) -> str:
    """Entfernt aus der Bildidee der Beitragsgenerierung alle Sätze, die Text im Bild beschreiben, sowie zitierte
    Wortlaute. Sonst zeichnet das Bildmodell die Headline selbst ins Bild und die App setzt sie ein zweites Mal
    darüber."""
    idee = re.sub(r"[„\"“»«'‚‘][^„\"“”»«'‚‘’]{1,120}[“”\"«»'‘’]", "", idee or "")
    saetze = re.split(r"(?<=[.!?;])\s+", idee)
    return " ".join(s for s in saetze if s.strip() and not _TEXT_HINWEIS.search(s)).strip()


def bild_prompt(profile: dict, context: dict, post: dict, aenderungswunsch: str = "",
                overlay_modus: str = "optional") -> str:
    """Bild-Prompt – zusammengesetzte Endform. Die Bildidee stammt aus 'bild_prompt' der Beitragsgenerierung;
    fehlt sie, wird sie aus Profil und Kontext beschrieben."""
    idee = bildidee_ohne_text((post.get("bild_prompt") or "").strip())
    if not idee:
        idee = (f"Motiv zum Thema „{context.get('thema', '')}“, Unteraspekt „{post.get('aspekt', '')}“, "
                f"für {profile.get('unternehmensname') or 'ein kleines Unternehmen'}"
                + (f" ({profile['branche']})" if profile.get("branche") else "") + ".")
    if post.get("serie"):
        # Karussell: Bildserie – gleicher Stil wie Karte 1, aber eigenes Motiv
        idee += ("\nKARUSSELL-SERIE: " + post["serie"].get("position", "") + ". Dieses Bild gehört zu einer Serie "
                 "und muss im gleichen Bildstil, mit gleicher Farbwelt und gleichem Licht wie die übrigen Karten "
                 "entstehen, zeigt aber ein EIGENES Motiv. Stil-Referenz (Karte 1): "
                 + bildidee_ohne_text(post["serie"].get("referenz", "")))
    if (aenderungswunsch or "").strip():
        # Änderungswunsch aus „Beitrag überarbeiten“ hat Vorrang
        idee += f"\nÄNDERUNGSWUNSCH DER NUTZENDEN (vorrangig umsetzen): {aenderungswunsch.strip()}"
    plattform = str(context.get("plattform_format") or "ig_feed")
    vorgaben = context.get("plattform_vorgaben") or {}
    ci = []
    if profile.get("ci_farbe"):
        ci.append(f"Primärfarbe {profile['ci_farbe']}")
    if profile.get("ci_farbe_sekundaer"):
        ci.append(f"Sekundärfarbe {profile['ci_farbe_sekundaer']}")
    if profile.get("ci_besonderheiten"):
        ci.append(profile["ci_besonderheiten"])
    if profile.get("logo_path"):
        ci.append("Logo vorhanden (wird separat montiert)")
    unternehmen = " | ".join(x for x in (profile.get("unternehmensname"), profile.get("branche"),
                                         profile.get("positionierung")) if x) or "unbekannt"
    # Kein Wort „Headline/Overlay-Text“ an das Bildmodell – sonst zeichnet es selbst Text
    overlay = ("keine Freifläche nötig" if overlay_modus == "kein_text" else
               f"ruhiger, natürlicher Bereich in der {overlay_position(plattform)} (die App setzt dort später "
               "selbst Text; im Bild selbst KEINEN Text und KEINE Fläche dafür zeichnen)")
    return (
        "ERSTELLE EIN BILD FÜR EINEN ORGANISCHEN SOCIAL-MEDIA-BEITRAG.\n\n"
        f"BILDIDEE AUS DER BEITRAGSGENERIERUNG:\n{idee}\n\n"
        "KONTEXT:\n"
        f"Unternehmen: {unternehmen}\n"
        f"Kommunikationsziel: {context.get('kommunikationsziel') or 'Bekanntheit'}\n"
        f"Zielgruppe: {profile.get('zielgruppe') or 'allgemein (keine näheren Angaben)'}\n"
        f"Plattform/Format: {plattform}\n"
        f"Verifiziertes Bildformat/Seitenverhältnis: {vorgaben.get('verhaeltnis') or ('9:16' if plattform.endswith('_story') else '4:5')}\n"
        f"Corporate Identity: {'; '.join(ci) if ci else 'unbekannt'}\n"
        f"Freiraum im Bild: {overlay}\n"
        f"Personen im Bild: {_personen_parameter(context)}\n"
        f"Bildsprache: {context.get('bildstil') or 'realistisches, glaubwürdiges Foto'}\n\n"
        + BILD_GESTALTUNGSPRINZIPIEN
    )


# --- 5. FAKTENCHECK – vor der Freigabe, nur gegen das Profil ---------------------------------

FAKTENCHECK_SYSTEM = (
    "Du prüfst einen organischen Social-Media-Beitrag auf KONKRETE sachliche Widersprüche gegenüber übergebenen "
    "Unternehmensquellen.\n\n"
    "QUELLE: PROFIL – direkt vom Unternehmen gepflegte und für den konkreten Beitrag bestätigte Daten. Ebenfalls "
    "belegt sind „angebot“ und „bestaetigte_angaben_aus_dialog“ im BEITRAG (vom Unternehmen im Dialog bestätigt).\n\n"
    "Prüfe überprüfbare Aussagen zu Produkten, Öffnungszeiten, Kontaktangaben, Preisen, Rabattwerten, Zeiträumen, "
    "Bedingungen und Standorten. Ignoriere rein stilistische Fragen und Geschmacksurteile.\n\n"
    "MELDUNGSTYPEN:\n"
    "- „widerspruch“: konkrete Aussage des Beitrags widerspricht einem höher priorisierten belegten Fakt.\n"
    "- „unbelegt“: risikoreiche konkrete Aussage (z. B. Preis, Aktionszeitraum, Öffnungszeiten) steht in keiner "
    "übergebenen Quelle.\n\n"
    "Erfinde keine Tatsachen. Nicht belegte allgemeine Werbesprache ist nicht automatisch ein Faktenwiderspruch. "
    "Auch aus allgemeinen Brancheninformationen keine konkreten Unternehmensangaben ableiten.\n\n"
    "Antworte ausschließlich als valides JSON mit exakt dieser Struktur:\n"
    '{"befunde":[{"typ":"widerspruch","textstelle":"wörtliches Zitat aus dem Beitrag oder leer",'
    '"hinweis":"ein präziser Satz","quelle":"Profil"}]}\n'
    "Erlaubte Werte von „typ“: „widerspruch“, „unbelegt“; von „quelle“: „Profil“, „keine“. "
    "Bei keinen Befunden exakt {\"befunde\":[]}."
)


# --- B. VERGLEICHSVARIANTE FÜR DEN A/B-TEST -----------------------------------------------------
# Variante B = „nackter“ Prompt OHNE Prompt-Architektur: kein Unternehmensprofil (kein Name, keine Zielgruppe,
# Tonalität, CI oder Fakten), keine Theorien, keine Gestaltungsregeln, keine Selbstprüfung. B bekommt dieselben
# Dialogangaben wie A (Thema, Wirkung, Art des Beitrags, Plattform/Format, Angebot, Rückfragen, Personen) als schlichte
# Stichpunkte. Gleich in A und B (technische Konstanten): Modelle, Bildqualität, JSON-Antwortformat, Bildformat,
# „kein Text im Bild“ (die App setzt die Headline selbst) und die Kennzeichnung „KI-generiert“.
BASIS_TEMPERATUR = 0.8


def basis_angaben(p: dict) -> str:
    """Die Dialogangaben als Stichpunkte – ohne Definitionen, Regeln oder Unternehmensdaten."""
    v = p.get("plattform_vorgaben") or {}
    personen = p.get("personen_im_bild")
    zeilen = [
        ("Plattform", f"{v.get('plattform', 'Instagram')} {v.get('art', 'Feed')}"
                      + (f", Seitenverhältnis {v['verhaeltnis']}" if v.get("verhaeltnis") else "")),
        ("Thema", p.get("thema")),
        ("Ziel", p.get("wirkung_label")),
        ("Art des Beitrags", p.get("contenttyp_label")),
        ("Angebot", p.get("angebot")),
        ("Weitere Angaben", klaerungen_text(p) if p.get("klaerungen") else ""),
        ("Personen im Bild", "ja" if personen is True else "nein" if personen is False else ""),
    ]
    return "\n".join(f"{k}: {w}" for k, w in zeilen if w)


def basis_text_prompt(p: dict) -> str:
    karussell = str(p.get("plattform_format") or "").endswith("_karussell")
    return (
        "Erstelle drei unterschiedliche Social-Media-Beiträge.\n" + basis_angaben(p) + "\n\n"
        "Technisches Antwortformat: ausschließlich JSON "
        '{"posts":[{"headline":"kurzer Text für das Bild","post_text":"Caption","hashtags":["#..."]'
        + (',"slides":[{"headline":"Text der Karte","text":"..."}]' if karussell else "") + "}]}"
    )


def basis_bild_prompt(p: dict, karte: str = "") -> str:
    return ("Erstelle ein Bild für einen Social-Media-Beitrag.\n" + basis_angaben(p)
            + (f"\n{karte}" if karte else "") + "\nOhne Text im Bild.")
