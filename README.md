# Employee Attendance & Analytics API

A small attendance service built with **FastAPI** and **MongoDB**: punch-in / punch-out with business rules,
manual corrections with an audit trail, and analytics computed by MongoDB aggregation pipelines.

**Stack:** Python 3.11+, FastAPI, PyMongo (synchronous), MongoDB 6.0+.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # Windows: copy .env.example .env
uvicorn app.main:app --port 8000
```

`.env` holds `MONGO_URI` and `MONGO_DB` (real environment variables take priority over the file).
Indexes are created automatically at startup. Interactive API docs: <http://localhost:8000/docs>.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Pings MongoDB; 503 if it is unreachable |
| POST, GET | `/employees` | Create (409 on duplicate `emp_code`); paginated list sorted by `emp_code` |
| POST | `/attendance/punch-in` | One record per employee per day (unique index decides races) |
| POST | `/attendance/punch-out` | Closes the latest open record; works across midnight for overnight shifts |
| PATCH | `/attendance/{emp_code}/{date}` | Manual correction: recomputes derived fields, appends an audit entry |
| GET | `/attendance` | Filtered, paginated list (`date desc, emp_code asc`) |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Per-department monthly summary, including employees with no logs |
| GET | `/analytics/leaderboard/late` | Late-minutes ranking with ties |
| GET | `/analytics/departments/{department}/trend` | One row per day: headcount, attendance rate, 7-day moving average |
| GET | `/admin/explain/{endpoint}` | `explain` (executionStats) of an endpoint's main query |

## Design highlights

* **Time.** Instants are stored as timezone-aware UTC datetimes and exposed as epoch milliseconds.
  Calendar dates and shift times are IST strings. The IST date, grace period, overtime and rounding rules are small
  pure functions (`attendance_date`, `compute_late_minutes`, `compute_overtime`, `round_half_up`), which are easy to test.
* **Correctness under concurrency.** There is no "check, then insert". A unique `(emp_code, date)` index makes punch-in
  race-safe; punch-out is a conditional update (`punch_out == null`); a correction writes only if the record is
  unchanged since it was read, and re-plans a few times before answering 409. No history entry is ever lost.
* **Analytics in MongoDB.** Each endpoint is one aggregation pipeline built by a `*_pipeline()` function:
  `$group`, `$lookup` with a sub-pipeline, `$rank` for competition ranking, `$densify` to fill missing days and
  `$setWindowFields` for the running headcount and moving average. The explain endpoint reuses the same builders,
  so it shows the real plan.
* **Exact rounding.** MongoDB's `$round` rounds half to even, so half-up rounding is done with integer arithmetic
  (work hours summed in hundredths, rates in ten-thousandths) or with `Decimal`, never by rounding a float.
* **Indexes** (all created at startup; reasoning in `DECISIONS.md`):
  `employees`: `emp_code` (unique), `(department, emp_code)`, `joined_on`.
  `attendance_logs`: `(emp_code, date)` (unique), `(date desc, emp_code)`, `(status, date desc, emp_code)`,
  `(emp_code, punch_in desc)`.

## Testing

Everything below is optional and lives in `scripts/` (standard library plus PyMongo only).

```bash
# 1. Load test data into a scratch database (--wipe empties employees and attendance_logs)
python scripts/seed_test_data.py --wipe --bulk 100000

# 2. Start the API against the same database (see .env), then in another terminal:
python scripts/smoke_test.py
```

The seed data contains the awkward cases: employees with no logs, an employee who has not joined yet, a mid-month
joiner, an overnight shift, weekend records, a half day, a missing weekday, a tie on the leaderboard and a log of an
unknown employee. The smoke test checks the analytics numbers (worked out independently of the application code),
the validation errors, the concurrency rules (parallel punch-ins, punch-outs and corrections) and that every explain
plan uses `IXSCAN` with no `COLLSCAN`.

## Assumptions where the contract is silent

* The trend range limit is 92 days **inclusive** (92 is accepted, 93 returns 422).
* `moving_avg_7d` is the mean of the already-rounded `attendance_rate` values in the window.
* The trend returns 404 only when no employee has that department, whatever their join dates.
* Logs of employees that do not exist are ignored by analytics.
* A correction that loses a race is retried up to 5 times, then answered with 409.

## Repository layout

```
app/main.py          the whole API
requirements.txt
REVIEW.md            defects found in the starter code, and how they were fixed
DECISIONS.md         short answers to the design questions
scripts/             seed_test_data.py, smoke_test.py
.env.example
```

## Status

All endpoints are implemented. Known gaps: none.
