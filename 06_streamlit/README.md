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

## Online bereitstellen (Streamlit Community Cloud)

1. Auf share.streamlit.io mit GitHub anmelden → **Create app**.
2. Repository wählen, Branch `main`, Hauptdatei `06_streamlit/app.py`.
3. **Advanced settings**: Python 3.12; unter **Secrets** eintragen:
   ```
   API_CHATGPT = "sk-..."
   ```
4. **Deploy**. Für den A/B-Test den Link mit `?evaluation=1` aufrufen.

Hinweise:
- Design (`.streamlit/config.toml`), Datenbank (`content_engine.db`) und Logos/Schriften (`uploads/`) werden aus
  diesem Ordner geladen.
- Das Dateisystem online ist nicht dauerhaft: Nach jedem Neustart (z. B. neuer Commit) gilt wieder der Stand der
  Datenbank aus dem Repository. Während einer Evaluationsphase daher keine Änderungen hochladen.
