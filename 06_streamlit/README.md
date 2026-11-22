# Content-Engine – Bachelorarbeit Sophia Freiwald

KI-gestützte Content-Engine für organische Instagram- und Facebook-Beiträge von KMU (Streamlit-App).

## Lokal starten

Im Ordner `06_streamlit`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
streamlit run app.py
```

Der OpenAI-Schlüssel steht in einer Datei `.env` neben `app.py` (wird nicht hochgeladen):

```
API_CHATGPT=sk-...
```

## Online bereitstellen (Railway, mit dauerhaftem Speicher)

1. Auf railway.com mit GitHub anmelden → **New Project** → **Deploy from GitHub repo** → dieses Repository.
   Railway baut die App über das `Dockerfile` im Hauptordner.
2. Im Service ein **Volume** hinzufügen (Mount-Pfad z. B. `/data`).
3. Unter **Variables** eintragen: `API_CHATGPT` (OpenAI-Schlüssel) und `ADMIN_PASSWORT` (frei wählbar, für den Export).
4. Unter **Settings → Networking** eine öffentliche Domain erzeugen und den Link teilen
   (A/B-Test: Link mit `?evaluation=1`).

Hinweise:
- Datenbank und `uploads/` liegen im Volume. Beim allerersten Start wird `content_engine.db` aus dem Repository
  als Startstand kopiert, danach nie wieder überschrieben – neue Commits ändern die Daten nicht.
- Datenexport: Link mit `?export=1` öffnen → „Datenexport (Auswertung)“ → Admin-Passwort → ZIP mit Datenbank
  und Bildern.
