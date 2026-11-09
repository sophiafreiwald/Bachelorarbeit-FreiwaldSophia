import datetime
import hashlib
import os
import secrets
import shutil
import sqlite3
from contextlib import contextmanager

# Speicherort der Daten (Datenbank + uploads):
#   - lokal und auf Streamlit Community Cloud: neben db.py (wie bisher)
#   - auf Railway mit Volume: im Volume (RAILWAY_VOLUME_MOUNT_PATH setzt Railway automatisch) oder in einem
#     frei gewählten Ordner (DATEN_VERZEICHNIS). Dort bleiben die Daten bei Neustarts und neuen Commits erhalten.
APP_VERZEICHNIS = os.path.dirname(os.path.abspath(__file__))
DATEN_VERZEICHNIS = os.path.abspath(os.environ.get("DATEN_VERZEICHNIS") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
                                    or APP_VERZEICHNIS)
UPLOADS_VERZEICHNIS = os.path.join(DATEN_VERZEICHNIS, "uploads")
DB_PATH = os.environ.get("CONTENT_ENGINE_DB", os.path.join(DATEN_VERZEICHNIS, "content_engine.db"))


def _startdaten_kopieren():
    """Liegen die Daten außerhalb des App-Ordners (Volume), wird beim allerersten Start die Datenbank aus dem
    Repository als Startstand übernommen. Danach wird sie NIE überschrieben – neue Commits ändern die Daten
    also nicht. Dateien aus uploads/ (Logos, Schriften, Bilder der Testprofile) werden nur ergänzt, wenn sie
    im Volume noch fehlen."""
    if DATEN_VERZEICHNIS == APP_VERZEICHNIS:
        return
    os.makedirs(DATEN_VERZEICHNIS, exist_ok=True)
    start_db = os.path.join(APP_VERZEICHNIS, "content_engine.db")
    if not os.path.exists(DB_PATH) and os.path.exists(start_db):
        shutil.copy2(start_db, DB_PATH)
    quelle = os.path.join(APP_VERZEICHNIS, "uploads")
    for ordner, _, dateien in os.walk(quelle):
        ziel_ordner = os.path.join(UPLOADS_VERZEICHNIS, os.path.relpath(ordner, quelle))
        os.makedirs(ziel_ordner, exist_ok=True)
        for name in dateien:
            ziel = os.path.join(ziel_ordner, name)
            if not os.path.exists(ziel):
                shutil.copy2(os.path.join(ordner, name), ziel)


_startdaten_kopieren()


def portabler_pfad(pfad: str) -> str:
    """Gespeicherte absolute Dateipfade (z. B. vom eigenen Mac: …/06_streamlit/uploads/logos/4.png) auf den
    uploads-Ordner dieser Installation abbilden, damit Logos, Schriften und Bilder auch online gefunden
    werden. Existiert die Datei, bleibt der Pfad unverändert."""
    if not pfad or os.path.exists(pfad) or pfad.startswith(("http://", "https://")):
        return pfad
    teile = pfad.replace("\\", "/").split("/uploads/", 1)
    if len(teile) == 2:
        for basis in (UPLOADS_VERZEICHNIS, os.path.join(APP_VERZEICHNIS, "uploads")):
            kandidat = os.path.join(basis, *teile[1].split("/"))
            if os.path.exists(kandidat):
                return kandidat
    return pfad


def daten_export_zip() -> bytes:
    """ZIP mit einer konsistenten Kopie der Datenbank und dem uploads-Ordner (für die Auswertung)."""
    import io
    import tempfile
    import zipfile
    puffer = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        kopie = os.path.join(tmp, "content_engine.db")
        quelle, ziel = sqlite3.connect(DB_PATH), sqlite3.connect(kopie)
        try:
            quelle.backup(ziel)
        finally:
            quelle.close()
            ziel.close()
        with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(kopie, "content_engine.db")
            for ordner, _, dateien in os.walk(UPLOADS_VERZEICHNIS):
                for name in dateien:
                    pfad = os.path.join(ordner, name)
                    z.write(pfad, os.path.join("uploads", os.path.relpath(pfad, UPLOADS_VERZEICHNIS)))
    return puffer.getvalue()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                unternehmensname TEXT DEFAULT '',
                branche TEXT DEFAULT '',
                standort TEXT DEFAULT '',
                tone_of_voice TEXT DEFAULT 'Professionell',
                ci_farbe TEXT DEFAULT '#000000',
                ci_schrift TEXT DEFAULT '',
                strategie TEXT DEFAULT '',
                instagram_account_id TEXT DEFAULT '',
                logo_path TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                aspekt TEXT DEFAULT '',
                caption TEXT NOT NULL,
                image_url TEXT DEFAULT '',
                bild_quelle TEXT DEFAULT 'upload',
                status TEXT NOT NULL DEFAULT 'geplant',
                scheduled_date TEXT NOT NULL,
                scheduled_time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        _migrate_profiles_schema(conn)
        spalten = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_posts)")}
        if "plattform_format" not in spalten:
            conn.execute("ALTER TABLE scheduled_posts ADD COLUMN plattform_format TEXT DEFAULT ''")
        if "video_url" in spalten:  # nicht mehr genutzt (Animationen entfernt)
            conn.execute("ALTER TABLE scheduled_posts DROP COLUMN video_url")
        if "slide_urls" not in spalten:  # Karussell: Pfade aller Kartenbilder als JSON-Liste (image_url = Karte 1)
            conn.execute("ALTER TABLE scheduled_posts ADD COLUMN slide_urls TEXT DEFAULT ''")
        if "seitenverhaeltnis" not in spalten:  # Feed/Karussell: 4:5 oder 1:1
            conn.execute("ALTER TABLE scheduled_posts ADD COLUMN seitenverhaeltnis TEXT DEFAULT ''")


# Spalten, die nach dem ersten Release von profiles hinzugekommen sind.
# ALTER TABLE ADD COLUMN, damit bestehende .db-Dateien nicht neu angelegt werden müssen.
_NEUE_PROFIL_SPALTEN = {
    "zielgruppe": "TEXT DEFAULT ''",
    "anrede": "TEXT DEFAULT 'Sie'",
    "ci_farbe_sekundaer": "TEXT DEFAULT ''",
    "ci_besonderheiten": "TEXT DEFAULT ''",
    "onboarding_done": "INTEGER DEFAULT 0",
    "ci_schrift_datei": "TEXT DEFAULT ''",
    # Unternehmenswissen (Stil); „produkte“ und „fakten“ entfallen, siehe _ENTFERNTE_PROFIL_SPALTEN
    "stilbeispiel": "TEXT DEFAULT ''",
    # für den Hinweis „Sind die Angaben noch aktuell?“
    "profil_aktualisiert": "TEXT DEFAULT ''",
    # Website: URL, Altfeld website_fakten (ungenutzt) und Abrufzeitpunkt
    "website": "TEXT DEFAULT ''",
    "website_fakten": "TEXT DEFAULT ''",
    "website_abgerufen": "TEXT DEFAULT ''",
    # aus der Website grob ausgelesene Tonalität
    "website_tonalitaet": "TEXT DEFAULT ''",
    # Veröffentlichung auf Facebook (Seiten-ID); Instagram nutzt instagram_account_id
    "facebook_page_id": "TEXT DEFAULT ''",
    # Getrennte Profilangaben (statt eines Sammelfelds „Strategie / Positionierung“ bzw. „Fakten“)
    "positionierung": "TEXT DEFAULT ''",   # Abgrenzung zur Konkurrenz; "strategie" = Ziele
    "strasse": "TEXT DEFAULT ''",
    "plz": "TEXT DEFAULT ''",
    "ort": "TEXT DEFAULT ''",              # ersetzt in der Oberfläche "standort" (wird beim Speichern übernommen)
    "telefon": "TEXT DEFAULT ''",
    "kontakt_email": "TEXT DEFAULT ''",
    "oeffnungszeiten": "TEXT DEFAULT ''",  # JSON: {"Mo": "06:00–18:00", …, "So": "geschlossen"}
}


# Spalten, die nicht mehr abgefragt werden: „Produkte & Leistungen“ steckt jetzt in der Positionierung
# („Was bietet ihr an?“), „Weitere Fakten“ entfällt. Sie werden aus bestehenden .db-Dateien entfernt.
_ENTFERNTE_PROFIL_SPALTEN = ("produkte", "fakten")


def _produkte_in_positionierung(conn):
    """Einmalig vor dem Entfernen der Spalte „produkte“: bisherige Angaben nicht verlieren.
    Produkte/Leistungen → „Angebot“, alter Freitext der Positionierung (Abgrenzung zur Konkurrenz)
    → „Besonderheit“ – im Format „Etikett: Wert; …“ der Leitfragen."""
    def bereinigt(text):
        return " ".join((text or "").replace(";", ",").split())

    for row in conn.execute("SELECT user_id, produkte, positionierung FROM profiles").fetchall():
        produkte, positionierung = bereinigt(row["produkte"]), (row["positionierung"] or "").strip()
        if not produkte or positionierung.startswith("Angebot:"):
            continue  # nichts zu übernehmen bzw. schon im neuen Format
        teile = [f"Angebot: {produkte}"]
        if positionierung:
            teile.append(f"Besonderheit: {bereinigt(positionierung)}")
        conn.execute("UPDATE profiles SET positionierung = ? WHERE user_id = ?", ("; ".join(teile), row["user_id"]))


def _fakten_in_ci_besonderheiten(conn):
    """Einmalig vor dem Entfernen der Spalte „fakten“: bisherige „Weitere Fakten“ an die
    „Sonstigen CI-Besonderheiten“ anhängen, damit nichts verloren geht."""
    for row in conn.execute("SELECT user_id, fakten, ci_besonderheiten FROM profiles").fetchall():
        fakten = (row["fakten"] or "").strip()
        if not fakten:
            continue
        bisher = (row["ci_besonderheiten"] or "").strip()
        neu = f"{bisher}\n{fakten}" if bisher else fakten
        conn.execute("UPDATE profiles SET ci_besonderheiten = ? WHERE user_id = ?", (neu, row["user_id"]))


def _migrate_profiles_schema(conn):
    vorhandene_spalten = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
    for spalte, definition in _NEUE_PROFIL_SPALTEN.items():
        if spalte not in vorhandene_spalten:
            conn.execute(f"ALTER TABLE profiles ADD COLUMN {spalte} {definition}")
    if "produkte" in vorhandene_spalten:
        _produkte_in_positionierung(conn)
    if "fakten" in vorhandene_spalten:
        _fakten_in_ci_besonderheiten(conn)
    for spalte in _ENTFERNTE_PROFIL_SPALTEN:
        if spalte in vorhandene_spalten:
            conn.execute(f"ALTER TABLE profiles DROP COLUMN {spalte}")


# --- Nutzer & Login -----------------------------------------------------

def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def create_user(email: str, password: str) -> int:
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (email.strip().lower(), password_hash, salt, datetime.datetime.utcnow().isoformat()),
        )
        user_id = cur.lastrowid
        conn.execute("INSERT INTO profiles (user_id) VALUES (?)", (user_id,))
        return user_id


def email_exists(email: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        return row is not None


def verify_login(email: str, password: str):
    """Gibt die user_id zurück, wenn Login korrekt ist, sonst None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash, salt FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        if row is None:
            return None
        if _hash_password(password, row["salt"]) == row["password_hash"]:
            return row["id"]
        return None


# --- Unternehmensprofil ---------------------------------------------------

def get_profile(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {}
        profil = dict(row)
        for feld in ("logo_path", "ci_schrift_datei"):  # Pfade vom eigenen Rechner auch online auflösen
            if profil.get(feld):
                profil[feld] = portabler_pfad(profil[feld])
        return profil


def save_profile(user_id: int, **fields):
    allowed = {
        "unternehmensname", "branche", "standort", "tone_of_voice",
        "ci_farbe", "ci_farbe_sekundaer", "ci_schrift", "ci_besonderheiten",
        "strategie", "instagram_account_id", "logo_path",
        "zielgruppe", "anrede", "onboarding_done", "ci_schrift_datei",
        "stilbeispiel", "profil_aktualisiert",
        "website", "website_fakten", "website_abgerufen", "website_tonalitaet", "facebook_page_id",
        "positionierung", "strasse", "plz", "ort", "telefon", "kontakt_email", "oeffnungszeiten",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE profiles SET {set_clause} WHERE user_id = ?",
            (*updates.values(), user_id),
        )


# --- Kalender / geplante Beiträge ----------------------------------------

def add_scheduled_post(user_id: int, aspekt: str, caption: str, image_url: str,
                        bild_quelle: str, scheduled_date: str, scheduled_time: str,
                        status: str = "geplant", plattform_format: str = "", slide_urls: str = "", seitenverhaeltnis: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO scheduled_posts
               (user_id, aspekt, caption, image_url, bild_quelle, status,
                scheduled_date, scheduled_time, created_at, plattform_format, slide_urls,
                seitenverhaeltnis)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, aspekt, caption, image_url, bild_quelle, status,
             scheduled_date, scheduled_time, datetime.datetime.utcnow().isoformat(), plattform_format,
             slide_urls, seitenverhaeltnis),
        )
        return cur.lastrowid


def list_scheduled_posts(user_id: int, year: int = None, month: int = None):
    with get_conn() as conn:
        if year is not None and month is not None:
            prefix = f"{year:04d}-{month:02d}"
            rows = conn.execute(
                "SELECT * FROM scheduled_posts WHERE user_id = ? AND scheduled_date LIKE ? ORDER BY scheduled_date, scheduled_time",
                (user_id, f"{prefix}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_posts WHERE user_id = ? ORDER BY scheduled_date, scheduled_time",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def mark_post_published(user_id: int, post_id: int) -> bool:
    """Markiert einen geplanten Beitrag als veröffentlicht und verschiebt ihn im Kalender auf den
    TATSÄCHLICHEN Veröffentlichungszeitpunkt (sonst stünde er weiter am ursprünglich geplanten Tag)."""
    jetzt = datetime.datetime.now()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE scheduled_posts SET status = 'veroeffentlicht', scheduled_date = ?, scheduled_time = ?
               WHERE id = ? AND user_id = ? AND status = 'geplant'""",
            (jetzt.date().isoformat(), jetzt.strftime("%H:%M"), post_id, user_id),
        )
        return cur.rowcount > 0


def get_scheduled_post(user_id: int, post_id: int):
    """Einzelner Beitrag – nur wenn er zu diesem Konto gehört (sonst None)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM scheduled_posts WHERE id = ? AND user_id = ?", (post_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def update_scheduled_post(user_id: int, post_id: int, caption: str, scheduled_date: str, scheduled_time: str) -> bool:
    """Geplanten Beitrag nachträglich ändern (Human-in-the-loop: bis zur Veröffentlichung bearbeitbar).
    Veröffentlichte Beiträge bleiben unverändert. Gibt True zurück, wenn etwas geändert wurde."""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE scheduled_posts SET caption = ?, scheduled_date = ?, scheduled_time = ?
               WHERE id = ? AND user_id = ? AND status = 'geplant'""",
            (caption, scheduled_date, scheduled_time, post_id, user_id),
        )
        return cur.rowcount > 0


def delete_scheduled_post(user_id: int, post_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM scheduled_posts WHERE id = ? AND user_id = ?", (post_id, user_id))
        return cur.rowcount > 0
