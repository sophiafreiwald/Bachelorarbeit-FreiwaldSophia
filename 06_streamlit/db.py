"""SQLite-Datenschicht für den Content-Engine-Prototyp.

Bewusst einfach gehalten (ein lokales SQLite-File, kein externer Dienst) –
passend zum aktuellen Entwicklungsstand als Bachelorarbeit-Prototyp.
Das Passwort-Hashing (hashlib + Salt) ist für einen Testbetrieb mit den
7 Interviewpartnern ausreichend, aber NICHT production-grade (kein bcrypt/
argon2) – das sollte in den Limitationen erwähnt werden, falls die
Anwendung über den Evaluationszeitraum hinaus eingesetzt werden soll.
"""

import datetime
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("CONTENT_ENGINE_DB", "content_engine.db")


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
    # Unternehmenswissen (Produkte, Fakten, Stil)
    "produkte": "TEXT DEFAULT ''",
    "fakten": "TEXT DEFAULT ''",
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


def _migrate_profiles_schema(conn):
    vorhandene_spalten = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
    for spalte, definition in _NEUE_PROFIL_SPALTEN.items():
        if spalte not in vorhandene_spalten:
            conn.execute(f"ALTER TABLE profiles ADD COLUMN {spalte} {definition}")


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
        return dict(row) if row else {}


def save_profile(user_id: int, **fields):
    allowed = {
        "unternehmensname", "branche", "standort", "tone_of_voice",
        "ci_farbe", "ci_farbe_sekundaer", "ci_schrift", "ci_besonderheiten",
        "strategie", "instagram_account_id", "logo_path",
        "zielgruppe", "anrede", "onboarding_done", "ci_schrift_datei",
        "produkte", "fakten", "stilbeispiel", "profil_aktualisiert",
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
