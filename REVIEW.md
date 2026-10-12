# REVIEW.md

Defects found in the starter `app/main.py`, in file order. "Rule" refers to R1-R10 in the contract.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace check is done on already-floored minutes (`int(seconds/60) > 10`), so a punch-in 1 second past the grace period is not late (R2). | Shift 09:30, punch 09:40:01: returns 0, expected 10. | Compare **seconds** (`> 600`), then floor to minutes. |
| 2 | `compute_late_minutes` | Shift start is built from the punch-in's own clock date, ignoring the attendance date and overnight shifts (R1). | Shift 22:00-06:00, punch 00:30 next day: start becomes 22:00 of the *same* day, difference is negative, returns 0, expected 150. | New `shift_bounds(date, start, end)` builds the shift start for the record's attendance date. |
| 3 | `compute_late_minutes`, `punch_in` | Instants are not truncated to whole seconds (R1). | `punched_at` ending in `...900` ms around the 09:40:00 boundary is judged late. | `ms_to_dt` truncates to whole seconds before any calculation; the truncated value is stored and returned. |
| 4 | `compute_work_hours` | Python `round()` on a float: banker's rounding plus binary error, not half-up (R4). | 17:07:30 - 09:00:00 = 8.125 h: returns 8.12, expected 8.13. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). | Shift end 18:30, punch-out 18:45: returns 15, expected 0. | Return minutes only when `>= 30`. |
| 6 | `compute_overtime` | Shift end is always on the record's own date, so overnight shifts break (R1/R3); it is also built with `tzinfo=IST` and compared with whatever datetime the caller passes, which raises `TypeError` for naive datetimes (500). | 22:00-06:00 shift, punch-out 06:40 next day: end is 06:00 *before* the shift started, overtime is about 18 h. | `shift_bounds` pushes an overnight end to the next day; all datetimes are timezone-aware. |
| 7 | whole file | `half_day` is never computed (R5); the field is always `False`. | Punch in/out with 3.5 h worked: `half_day` stays false. | `is_half_day()` on the rounded `work_hours`, applied in `derive_fields`. |
| 8 | `punch_in` | `datetime.fromtimestamp(ms/1000)` and `datetime.now()` are in the **server's local zone** and naive; PyMongo stores naive values as UTC, so stored instants are shifted by the server offset, and `date` is the server-local date, not the IST date (R1). | Run on a non-UTC host, or punch at 00:30 IST on a UTC server: wrong `date`, wrong lateness. | Aware UTC datetimes everywhere (`tz_aware=True` on the client), converted to IST only for the date and shift maths. |
| 9 | `punch_in` | Attendance date ignores the overnight rule (R1). | 22:00-06:00 shift, punch-in at 00:30: record gets the next day's date. | `attendance_date()` applies the overnight rule. |
| 10 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises `TypeError` -> 500. | `POST /attendance/punch-in {"emp_code": "NOPE"}`. | 404 when the employee does not exist. |
| 11 | `punch_in` | Check-then-insert (`find_one`, then `insert_one`) is a race: two simultaneous requests both pass the check. No unique index either. | Fire two parallel requests: two 201s and two records for one day. | Unique index on `(emp_code, date)`; `insert_one` and catch `DuplicateKeyError` -> 409. |
| 12 | `PunchInIn.punched_at`, `punch_in` | Plain `Optional[int]` accepts seconds, numeric strings and floats, and `if body.punched_at` treats `0` as "not supplied". | `punched_at: 1783312500` (seconds) is read as January 1970; `"1783312500000"` is accepted. | `EpochMs` type: strict integer between 1e11 and 4.1e12, else 422; `is not None` check. |
| 13 | `PunchInIn.status` | Any string is accepted, including `ABSENT`, `LEAVE` and garbage. | `status: "banana"` creates a record. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 14 | `punch_in`, `list_attendance` | Response leaks the Mongo `_id` as `id` and returns datetimes (serialized as ISO strings) instead of epoch milliseconds; legacy records lack `half_day` / `history` / `late_minutes`. | Compare a response with the `AttendanceRecord` schema. | `serialize_record()`: no id, instants as epoch ms, defaults for missing legacy fields, history times converted too. |
| 15 | `EmployeeIn`, `create_employee` | No validation: `emp_code` format, e-mail, `HH:MM` shifts, `shift_start == shift_end`, and `joined_on` is any string. | `shift_start: "99:99"` is accepted and later crashes punch-in with a 500. | Pydantic constraints and a model validator -> 422. |
| 16 | `create_employee` | Same check-then-insert race, with no unique index on `emp_code`. | Parallel creates of one code produce duplicates. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 17 | `create_employee` | `created_at` is a naive local `datetime.now()` and is returned as an ISO string. | Response does not match the `Employee` schema (epoch ms). | Aware UTC now, serialized as epoch ms. |
| 18 | `list_employees` | `skip = page * page_size`: page 1 skips the first page. | `GET /employees?page=1` misses the first 20 employees. | `(page - 1) * page_size`. |
| 19 | `list_employees` | `total` counts the whole collection, ignoring the `department` filter (R10). | `?department=X` returns 3 items and `total` 500. | `count_documents(q)` with the same filter. |
| 20 | `list_employees` | No sort, so pages are not deterministic. | Items repeat or disappear across pages. | `sort("emp_code", 1)`; backed by a `(department, emp_code)` index. |
| 21 | `list_employees`, `list_attendance` | `page` and `page_size` are unbounded (R10). | `page=0` gives a negative skip -> PyMongo error -> 500; `page_size=1000000` is accepted. | `Query(ge=1)` and `Query(ge=1, le=100)`. |
| 22 | `list_attendance` | Loads every matching document into memory, sorts and slices in Python; unusable at 100k records. | Slow or memory-heavy with 100k records; no index. | `sort` / `skip` / `limit` run in MongoDB, with indexes matching the filters and the sort. |
| 23 | `list_attendance` | Sorted by `date` only; no `emp_code` tie-break, so ordering inside a day is arbitrary. | Page boundaries inside one date shuffle. | Sort `date` desc, `emp_code` asc, served by an index. |
| 24 | `list_attendance` | No validation of `date_from`, `date_to`, `status`, no check that `date_from <= date_to`. | `date_from=banana` or `status=banana` returns 200 with garbage; reversed range returns empty instead of 422. | `DateStr` / `Status` types and an explicit 422. |
| 25 | `/health` | Always returns ok without touching MongoDB. | Stop MongoDB: `/health` still says ok. | `ping` the database; 503 on failure. |
| 26 | module level | No indexes are created anywhere, and `MongoClient` is naive with the default 30 s server-selection timeout. | `explain` shows `COLLSCAN`; a down database hangs requests for 30 s. | `ensure_indexes()` at startup (idempotent), `tz_aware=True`, 5 s timeout. |

## Looked at and decided it is not a defect

* **Plain `def` endpoints with the synchronous PyMongo client.** FastAPI runs them in a thread pool, so they do not block the event loop. `async def` with a blocking driver would be the bug.
* **`load_dotenv()` with defaults.** It does not override existing environment variables, which is what the contract asks for.
* **`doc.pop("_id", None)` after `insert_one`.** `insert_one` adds `_id` to the dict, so removing it before returning was needed (I now build the response explicitly instead).
* **Pydantic ignoring unknown fields.** A client that sends `work_hours` or `created_at` has it silently ignored, which is what the contract says for derived fields.
