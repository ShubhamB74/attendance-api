# Employee Attendance & Analytics API

FastAPI + MongoDB (Python 3.11+, MongoDB 6.0+). All eleven endpoints of the contract are implemented; the analytics
endpoints are MongoDB aggregation pipelines and `GET /admin/explain/{endpoint}` returns `executionStats` for each.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup; no manual steps. Interactive docs: http://localhost:8000/docs

## Test it
```bash
MONGO_DB=attendance_test python scripts/seed_test_data.py --wipe --bulk 100000   # scratch DB! --wipe empties it
uvicorn app.main:app --port 8000                                                 # (with the same MONGO_DB)
python scripts/smoke_test.py                                                     # standard library only
```
The seed contains the awkward cases: employees with no logs, a not-yet-joined employee, a mid-month joiner, an
overnight shift, weekend records, a half day, a missing weekday, a tie on the leaderboard, a log of an unknown employee.
The smoke test also checks the concurrency rules and that every explain plan has `IXSCAN` and no `COLLSCAN`.

## Where things are
* `app/main.py`: everything (rules R1-R5 as small pure functions, pipelines built by `*_pipeline()` functions that the
  explain endpoint reuses).
* `REVIEW.md`: defects found in the starter. `DECISIONS.md`: design questions.

## Assumptions where the contract is silent
* Trend range limit: 92 days **inclusive** (92 is accepted, 93 gives 422).
* `moving_avg_7d` is the mean of the already-rounded (4 decimals) `attendance_rate` values in the window.
* Unknown department for the trend is 404 only when no employee has that department, regardless of join dates.
* Records of an employee who does not exist are ignored by analytics. Records dated before an employee's `joined_on`
  are still counted in `present_days` (only working days are counted from `joined_on`).
* `PATCH` retries up to 5 times when another correction wins the race, then answers 409.

## Status
Nothing known to be missing.
