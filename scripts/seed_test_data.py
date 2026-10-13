"""Builds MY OWN test data with the awkward cases the business rules describe (the 9-document sample is not enough).

    python scripts/seed_test_data.py --wipe                 # small hand-made dataset (used by smoke_test.py)
    python scripts/seed_test_data.py --wipe --bulk 100000   # + ~100k synthetic records (2025 dates) for speed / explain checks

WARNING: --wipe deletes ALL documents in `employees` and `attendance_logs` of the database named by MONGO_DB.
Use a scratch database, e.g.  MONGO_DB=attendance_test python scripts/seed_test_data.py --wipe

Derived fields are computed here with a small, independent implementation of R1-R5, so the seed does not just
echo the code under test.

Awkward cases in the hand-made data (month 2026-07; 2026-07-01 is a Wednesday):
  * EMP1006 and EMP1004: no logs at all; EMP1004 joins 2026-08-10, i.e. AFTER July (must not count in July headcount)
  * EMP1003: mid-month joiner (2026-07-15), no log on Fri 07-17 (missing weekday)
  * EMP1001 / EMP1007: weekend records (late/overtime count, present_days does not)
  * EMP1001: 09:40:00 (not late) and 09:40:01 (late = 10) on consecutive days
  * EMP1002: half day (3.5 h), LEAVE and ABSENT records
  * EMP1008: overnight shift 22:00-06:00, including a punch-in after midnight that belongs to the previous date
  * late totals 140 / 50 / 30 / 30 / 30 / 20  ->  ranks 1, 2, 3, 3, 3, 6 (a tie, and rank 4 and 5 do not exist)
  * a log for EMP9999, who is not an employee (must be ignored by analytics)
"""
import argparse
import os
import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

EMPLOYEES = [
    # emp_code, name, department, shift_start, shift_end, joined_on
    ("EMP1001", "Asha Rao", "Engineering", "09:30", "18:30", "2026-01-05"),
    ("EMP1002", "Bilal Khan", "Engineering", "09:30", "18:30", "2026-01-05"),
    ("EMP1003", "Chitra Iyer", "Engineering", "09:30", "18:30", "2026-07-15"),
    ("EMP1004", "Dev Patel", "Engineering", "09:30", "18:30", "2026-08-10"),
    ("EMP1005", "Esha Nair", "Sales", "09:30", "18:30", "2026-01-05"),
    ("EMP1006", "Farid Ali", "Sales", "09:30", "18:30", "2026-01-05"),
    ("EMP1007", "Gita Sen", "Sales", "09:30", "18:30", "2026-01-05"),
    ("EMP1008", "Hari Om", "Ops", "22:00", "06:00", "2026-01-05"),
    ("EMP1009", "Ira Das", "Ops", "09:30", "18:30", "2026-01-05"),
]

# emp_code, status, punch_in (IST), punch_out (IST) | for ABSENT / LEAVE: the date instead of the punches
LOGS = [
    ("EMP1001", "PRESENT", "2026-07-06 09:28:00", "2026-07-06 18:35:00"),
    ("EMP1001", "PRESENT", "2026-07-07 09:40:01", "2026-07-07 19:10:00"),
    ("EMP1001", "PRESENT", "2026-07-08 09:40:00", "2026-07-08 18:30:00"),
    ("EMP1001", "PRESENT", "2026-07-04 10:10:00", "2026-07-04 14:00:00"),      # Saturday
    ("EMP1002", "PRESENT", "2026-07-06 10:00:00", "2026-07-06 18:30:00"),
    ("EMP1002", "PRESENT", "2026-07-07 09:30:00", "2026-07-07 13:00:00"),      # half day
    ("EMP1002", "LEAVE", "2026-07-08", None),
    ("EMP1002", "ABSENT", "2026-07-09", None),
    ("EMP1003", "PRESENT", "2026-07-15 09:30:00", "2026-07-15 18:30:00"),
    ("EMP1003", "WFH", "2026-07-16 09:35:00", "2026-07-16 18:30:00"),
    ("EMP1005", "PRESENT", "2026-07-06 10:00:00", "2026-07-06 18:30:00"),
    ("EMP1005", "ON_DUTY", "2026-07-07 09:30:00", "2026-07-07 18:30:00"),
    ("EMP1007", "PRESENT", "2026-07-06 10:00:00", "2026-07-06 18:30:00"),
    ("EMP1007", "PRESENT", "2026-07-12 09:30:00", "2026-07-12 19:05:00"),      # Sunday, overtime 35
    ("EMP1008", "PRESENT", "2026-07-06 21:55:00", "2026-07-07 06:40:00"),      # overnight, date 07-06
    ("EMP1008", "PRESENT", "2026-07-08 00:20:00", "2026-07-08 06:00:00"),      # after midnight -> date 07-07
    ("EMP1009", "PRESENT", "2026-07-06 09:50:00", "2026-07-06 18:30:00"),
    ("EMP9999", "PRESENT", "2026-07-06 10:30:00", "2026-07-06 18:30:00"),      # unknown employee
]


def ist(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def derive(shift_start: str, shift_end: str, status: str, punch_in, punch_out, day=None) -> dict:
    """Independent implementation of R1-R5. Returns the stored attendance document (instants as aware UTC)."""
    doc = {"status": status, "punch_in": None, "punch_out": None, "work_hours": None,
           "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    if status not in PRESENCE:
        doc["date"] = day
        return doc
    start, end = hm(shift_start), hm(shift_end)
    attendance_day = punch_in.date()
    if end <= start and punch_in.time() < end:           # overnight shift, punched in after midnight
        attendance_day -= timedelta(days=1)
    shift_start_at = datetime.combine(attendance_day, start, tzinfo=IST)
    shift_end_at = datetime.combine(attendance_day, end, tzinfo=IST)
    if end <= start:
        shift_end_at += timedelta(days=1)
    late_seconds = int((punch_in - shift_start_at).total_seconds())
    doc.update(date=attendance_day.isoformat(), punch_in=punch_in.astimezone(UTC),
               late_minutes=late_seconds // 60 if late_seconds > 600 else 0)
    if punch_out is not None:
        hours = (Decimal(int((punch_out - punch_in).total_seconds())) / Decimal(3600)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        overtime = max(0, int((punch_out - shift_end_at).total_seconds())) // 60
        doc.update(punch_out=punch_out.astimezone(UTC), work_hours=float(hours),
                   overtime_minutes=overtime if overtime >= 30 else 0, half_day=hours < Decimal("4.50"))
    return doc


def build_small():
    shifts = {e[0]: (e[3], e[4]) for e in EMPLOYEES}
    created = datetime(2026, 1, 1, tzinfo=UTC)
    employees = [{"emp_code": c, "name": n, "email": f"{c.lower()}@example.com", "department": d,
                  "shift_start": s, "shift_end": e, "joined_on": j, "created_at": created}
                 for c, n, d, s, e, j in EMPLOYEES]
    logs = []
    for code, status, a, b in LOGS:
        shift_start, shift_end = shifts.get(code, ("09:30", "18:30"))
        if status in PRESENCE:
            doc = derive(shift_start, shift_end, status, ist(a), ist(b) if b else None)
        else:
            doc = derive(shift_start, shift_end, status, None, None, day=a)
        logs.append({"emp_code": code, **doc})
    return employees, logs


def build_bulk(target: int, seed: int = 7):
    """~target synthetic records on weekdays of 2025/early 2026 (never overlaps July 2026), departments Bulk-1..Bulk-5."""
    rng = random.Random(seed)
    n_emp = 400
    employees = [{"emp_code": f"EMP{20000 + i}", "name": f"Bulk {i}", "email": f"bulk{i}@example.com",
                  "department": f"Bulk-{i % 5 + 1}", "shift_start": "09:30", "shift_end": "18:30",
                  "joined_on": "2024-12-01", "created_at": datetime(2024, 12, 1, tzinfo=UTC)}
                 for i in range(n_emp)]
    logs, day = [], date(2025, 1, 1)
    while len(logs) < target:
        if day.weekday() < 5:
            for e in employees:
                if len(logs) >= target:
                    break
                roll = rng.random()
                if roll < 0.05:
                    doc = derive("09:30", "18:30", "LEAVE", None, None, day=day.isoformat())
                elif roll < 0.08:
                    doc = derive("09:30", "18:30", "ABSENT", None, None, day=day.isoformat())
                else:
                    pin = datetime.combine(day, time(9, 30), tzinfo=IST) + timedelta(seconds=rng.randint(-1800, 3600))
                    pout = pin + timedelta(seconds=rng.randint(3 * 3600, 11 * 3600))
                    doc = derive("09:30", "18:30", rng.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY"]), pin, pout)
                logs.append({"emp_code": e["emp_code"], **doc})
        day += timedelta(days=1)
    return employees, logs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wipe", action="store_true", help="required: delete all employees and attendance_logs first")
    ap.add_argument("--bulk", type=int, default=0, metavar="N", help="also insert ~N synthetic records")
    args = ap.parse_args()

    from dotenv import load_dotenv
    from pymongo import MongoClient
    load_dotenv()
    db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]
    if not args.wipe:
        raise SystemExit(f"Refusing to run without --wipe (it empties employees and attendance_logs in '{db.name}').")

    employees, logs = build_small()
    if args.bulk:
        bulk_employees, bulk_logs = build_bulk(args.bulk)
        employees += bulk_employees
        logs += bulk_logs
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many(employees)
    for i in range(0, len(logs), 5000):
        db.attendance_logs.insert_many(logs[i:i + 5000])
    print(f"'{db.name}': {len(employees)} employees, {len(logs)} attendance records inserted")
    print("Start the API once afterwards so the indexes exist (they are created at startup).")


if __name__ == "__main__":
    main()
