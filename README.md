# Employee Attendance & Analytics API

FastAPI + MongoDB.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # MONGO_URI / MONGO_DB (real env vars take priority)
uvicorn app.main:app --port 8000
```
Indexes are created at startup; no manual steps.

## Status
Work in progress. Missing / known gaps: (to be updated at the end)
