# Bereitstellung der Content-Engine (z. B. auf Railway). Die App liegt in 06_streamlit.
FROM python:3.12-slim

# Standardschriften für die Werbebilder (Fallback, falls keine CI-Schrift gefunden wird)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY 06_streamlit/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY 06_streamlit/ .

# Railway gibt den Port über $PORT vor
CMD streamlit run app.py --server.port ${PORT:-8501} --server.address 0.0.0.0 --server.headless true
