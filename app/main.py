"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Conventions (see DATA_MODEL.md / openapi.yaml, kept locally):
  * Instants are stored as tz-aware UTC BSON dates and exposed as integer epoch milliseconds.
  * Calendar dates ("YYYY-MM-DD") and shift times ("HH:MM") are plain strings in IST.
  * Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are computed here and stored.
"""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False by default: real environment variables take priority

log = logging.getLogger("attendance")

# tz_aware=True -> PyMongo returns aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


# --------------------------------------------------------------------------- #
# Time handling and business rules R1 - R5 (standard library only)
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_EPOCH_MS = 100_000_000_000      # anything smaller looks like epoch *seconds*
MAX_EPOCH_MS = 4_102_444_800_000    # 2100-01-01
GRACE_SECONDS = 10 * 60             # R2
MIN_OVERTIME_MINUTES = 30           # R3
HALF_DAY_HOURS = 4.5                # R5
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Stored datetime -> epoch milliseconds (integer arithmetic, no float rounding)."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: naive values coming from Mongo are UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def round_half_up(value, places: int = 2) -> float:
    """Round half-up (not banker's, not binary-float rounding). Used for R4 and R8."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to.

    Overnight shift (shift_end <= shift_start): a punch-in earlier than shift_end (IST)
    belongs to the shift that started on the previous day.
    """
    local = punch_in.astimezone(IST)
    start, end = parse_hhmm(shift_start), parse_hhmm(shift_end)
    day = local.date()
    if end <= start and local.time() < end:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants (aware, IST) of the shift that belongs to attendance date `date_str`."""
    day = date.fromisoformat(date_str)
    start = datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(day, parse_hhmm(shift_end), tzinfo=IST)
    if end <= start:  # overnight: the shift ends on the next calendar day
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_at: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((punch_in - shift_start_at).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_end_at: datetime) -> int:
    """R3: floor minutes after shift end, counted only if at least 30."""
    seconds = int((punch_out - shift_end_at).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: the *rounded* work_hours is below 4.50."""
    return work_hours is not None and work_hours < HALF_DAY_HOURS


def derive_fields(emp: dict, status: str, att_date: str,
                  punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All stored derived fields for one record (table in DATA_MODEL.md section 3)."""
    fields = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES or punch_in is None:
        return fields
    start_at, end_at = shift_bounds(att_date, emp["shift_start"], emp["shift_end"])
    fields["late_minutes"] = compute_late_minutes(punch_in, start_at)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        fields.update(
            work_hours=hours,
            overtime_minutes=compute_overtime(punch_out, end_at),
            half_day=is_half_day(hours),
        )
    return fields


class RuleViolation(Exception):
    """A business-rule failure; endpoints turn it into a 422."""


def check_punch_pair(punch_in: datetime, punch_out: datetime) -> None:
    """punch_out must be after punch_in and at most 24 hours later."""
    if punch_out <= punch_in:
        raise RuleViolation("punch_out must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise RuleViolation("punch_out must be within 24 hours of punch_in")


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours",
                  "late_minutes", "overtime_minutes", "half_day")


def stored_state(rec: dict) -> dict:
    """The tracked fields of a stored record, with legacy defaults for missing ones."""
    return {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }


def plan_regularization(emp: dict, rec: dict, status: Optional[str],
                        punch_in: Optional[datetime], punch_out: Optional[datetime]) -> tuple[dict, dict]:
    """Apply a correction to `rec` in memory. Returns (new tracked state, changes for the history entry).

    status / punch_in / punch_out are the supplied values, or None when the client omitted them.
    """
    old = stored_state(rec)
    new_status = status or old["status"]
    if new_status in PRESENCE_STATUSES:
        pin = punch_in or old["punch_in"]
        pout = punch_out or old["punch_out"]
        if pin is None:
            raise RuleViolation("a presence status requires a punch_in")
        if attendance_date(pin, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise RuleViolation("punch_in must stay on the record's attendance date")
        if pout is not None:
            check_punch_pair(pin, pout)
    else:  # ABSENT / LEAVE clear both punch times, so supplying one is contradictory
        if punch_in is not None or punch_out is not None:
            raise RuleViolation("punch_in / punch_out cannot be supplied with status ABSENT or LEAVE")
        pin = pout = None
    new = {"status": new_status, "punch_in": pin, "punch_out": pout,
           **derive_fields(emp, new_status, rec["date"], pin, pout)}
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]}
    if not changes:
        raise RuleViolation("the request changes nothing")
    return new, changes

# --------------------------------------------------------------------------- #
# Request models and validated types
# --------------------------------------------------------------------------- #
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _check_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid calendar date, expected YYYY-MM-DD")
    return value


DateStr = Annotated[str, Field(pattern=DATE_PATTERN), AfterValidator(_check_date)]
HHMM = Annotated[str, Field(pattern=HHMM_PATTERN)]
# strict=True: only real JSON integers; floats ("1.5e12", 1.0) and strings are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # "= None" means "omitted -> unchanged". Pydantic does not validate defaults, so an explicit
    # null is still rejected with 422 (the contract does not allow null here).
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


_DATE_RE = re.compile(DATE_PATTERN, re.ASCII)


def require_date(value: str) -> str:
    """Validate a YYYY-MM-DD path parameter by hand (the route param must be called `date`)."""
    try:
        if not _DATE_RE.fullmatch(value):
            raise ValueError
        _check_date(value)
    except ValueError:
        raise HTTPException(422, "date must be a valid calendar date, YYYY-MM-DD")
    return value


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$", re.ASCII)


def require_month(value: str) -> str:
    """Validate a YYYY-MM query parameter by hand."""
    if not _MONTH_RE.fullmatch(value) or int(value[:4]) < 1:
        raise HTTPException(422, "month must be YYYY-MM")
    return value


def month_range(month: str) -> tuple[str, str]:
    """First and last calendar day of 'YYYY-MM' as 'YYYY-MM-DD' strings (string order == date order)."""
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] inclusive (R7: no holiday calendar). At most 31 iterations."""
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# Serialisation: Mongo document -> API JSON (no _id, instants as epoch ms)
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):  # BSON dates nested inside the object
            changes[field] = {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
        else:
            changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"),
            "reason": entry.get("reason"), "changes": changes}


def serialize_record(doc: dict) -> dict:
    """Missing legacy fields default as DATA_MODEL.md says (late/overtime 0, half_day false, history [])."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    # employees: uniqueness of the business key + department filter sorted by emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    # headcount (R9): employees who joined on or before the end of a month, optionally within a department
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs: one record per (emp_code, date) - this is what makes punch-in race-safe;
    # it also serves per-employee lists/analytics (equality on emp_code, range/sort on date)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uq_emp_code_date")
    # punch-out: latest record of an employee with punch_in <= t
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)],
                                    name="emp_code_punch_in_desc")
    # global list: sort "date desc, emp_code asc" with optional date range
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    # list filtered by status, same sort
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="status_date_emp_code")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB not reachable yet: /health will report 503 and retry
        log.warning("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides, not a prior find_one
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_employee(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, body.status, att_date, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # the employee's most recent record whose punch_in is at or before the punch-out (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    try:
        check_punch_pair(rec["punch_in"], ts)
    except RuleViolation as exc:
        raise HTTPException(422, str(exc))
    derived = derive_fields(emp, rec["status"], rec["date"], rec["punch_in"], ts)
    # Conditional update: only the request that still sees punch_out == null (and the same punch_in) wins.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        require_date(date_from)
    if date_to:
        require_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_record(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


MAX_PATCH_ATTEMPTS = 5


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    require_date(date)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    supplied_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    supplied_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    # Optimistic concurrency: read, plan in memory, write only if the record is still exactly as read.
    for _ in range(MAX_PATCH_ATTEMPTS):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        try:
            new, changes = plan_regularization(emp, rec, body.status, supplied_in, supplied_out)
        except RuleViolation as exc:
            raise HTTPException(422, str(exc))
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        history = rec.get("history")
        guard = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            # a concurrent correction (or punch-out) changes one of these, so our write would match nothing
            "history": {"$size": len(history)} if isinstance(history, list) else None,
        }
        updated = db.attendance_logs.find_one_and_update(
            guard,
            {"$set": new, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        if updated is not None:
            return serialize_record(updated)
        # somebody changed the record under us: re-read and re-plan (the result may now be 422/404)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Analytics: aggregation pipelines (built by functions so /admin/explain can reuse them)
# --------------------------------------------------------------------------- #
_PRESENCE = list(PRESENCE_STATUSES)
# `date` is an IST calendar-date string; parsing it as a UTC date gives that calendar date's weekday (1=Mon..7=Sun).
_WEEKDAY = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}
# Stored derived values are authoritative: read them, never recompute from punch times.
# Present-day credit of one record: 1 (0.5 for a half day) if presence status and Mon-Fri, else 0 (R5-R7).
_PRESENT_CREDIT = {"$cond": [
    {"$and": [{"$in": ["$status", _PRESENCE]}, {"$lte": [_WEEKDAY, 5]}]},
    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
    0,
]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},  # (emp_code, date) index
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _PRESENT_CREDIT},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    """Starts from `employees` (so people with zero logs still count) and joins each one's month of logs."""
    first, last = month_range(month)
    emp_match: dict = {"joined_on": {"$lte": last}}  # R9: not-yet-joined employees are excluded
    if department:
        emp_match["department"] = department
    wh_ok = {"$and": [{"$in": ["$logs.status", _PRESENCE]}, {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {
                    "_id": 0,
                    "status": 1,
                    "work_hours": 1,
                    "late": {"$ifNull": ["$late_minutes", 0]},
                    "credit": _PRESENT_CREDIT,
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},  # keep employees with no logs
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},  # unwind repeats an employee once per log
            "present_days": {"$sum": "$logs.credit"},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            # average over RECORDS: sum and count of work_hours (in whole hundredths, so the sum is exact)
            "work_cents": {"$sum": {"$cond": [wh_ok, {"$round": [{"$multiply": ["$logs.work_hours", 100]}, 0]}, 0]}},
            "work_records": {"$sum": {"$cond": [wh_ok, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    require_month(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_range(month)
    # R7: working days are counted from joined_on for a mid-month joiner (0 if not joined yet)
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working_days = count_weekdays(start, date.fromisoformat(last))
    agg = next(iter(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month))), None) or {}
    present_days = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": (round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 2)
                           if working_days else None),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    require_month(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department)):
        records = int(row["work_records"])
        avg = None
        if records:  # exact: whole hundredths / records / 100, rounded half-up (R8)
            avg = round_half_up(Decimal(int(row["work_cents"])) / Decimal(records) / Decimal(100), 2)
        items.append({
            "department": row["_id"],
            "headcount": len(row["employees"]),
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list:
    """Standard competition ranking (1, 2, 2, 4) via $setWindowFields/$rank; limit is applied AFTER ranking."""
    first, last = month_range(month)
    pipeline: list = [
        # only late records matter: the total of an employee with none would be 0 and is excluded anyway
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department:  # filter BEFORE ranking, so ranks are inside the department
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


_DAY_FMT = "%Y-%m-%d"


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    """One row per calendar day in [start, end]: $densify fills the gaps, $setWindowFields does the running maths.

    Rates are carried as integer ten-thousandths ("rate_int") so that half-up rounding and the moving average are exact.
    """
    from_dt = datetime.combine(start, time.min, tzinfo=UTC)
    to_dt = datetime.combine(end, time.min, tzinfo=UTC)
    joined_day = {"$dateFromString": {"dateString": "$joined_on", "format": _DAY_FMT}}
    # One "join event" per employee. Employees who joined before the range count from its first day;
    # employees who join after the range still emit an (uncounted) event so the stream is never empty.
    join_event = {"$let": {
        "vars": {"j": joined_day},
        "in": {
            "day": {"$cond": [{"$and": [{"$gt": ["$$j", from_dt]}, {"$lte": ["$$j", to_dt]}]}, "$$j", from_dt]},
            "joined": {"$cond": [{"$lte": ["$$j", to_dt]}, 1, 0]},
            "credit": 0,
            "late": 0,
        },
    }}
    # present_count: any weekday/weekend, half day = 0.5 (unlike the monthly summary, no Mon-Fri restriction)
    credit = {"$cond": [{"$in": ["$status", _PRESENCE]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}
    window_7 = {"documents": [-6, 0]}  # current row + up to 6 preceding rows, all inside the range
    return [
        {"$match": {"department": department}},  # (department, emp_code) index
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$project": {"_id": 0,
                              "day": {"$dateFromString": {"dateString": "$date", "format": _DAY_FMT}},
                              "credit": credit, "late": {"$ifNull": ["$late_minutes", 0]}}},
            ],
            "as": "logs",
        }},
        {"$project": {"_id": 0, "events": {"$concatArrays": [[join_event], "$logs"]}}},
        {"$unwind": "$events"},
        {"$replaceRoot": {"newRoot": "$events"}},
        {"$group": {"_id": "$day",
                    "joined": {"$sum": "$joined"},
                    "present_count": {"$sum": "$credit"},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "day": "$_id", "joined": 1, "present_count": 1, "late_count": 1}},
        {"$sort": {"day": 1}},
        # gap filling: a row for every day with no data (weekends, holidays, missing weekdays)
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day",
                                                "bounds": [from_dt, to_dt + timedelta(days=1)]}}},
        {"$set": {"joined": {"$ifNull": ["$joined", 0]},
                  "present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]},
                  "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        # R9: headcount = running total of joins up to and including that day
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}}}},
        # rate in whole ten-thousandths, half-up: floor(present * 10000 / headcount + 0.5); null off working days
        {"$set": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$add": [{"$divide": [{"$multiply": ["$present_count", 10000]}, "$headcount"]}, 0.5]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "rate_sum": {"$sum": "$rate_int", "window": window_7},  # $sum skips nulls
            "rate_n": {"$sum": {"$cond": [{"$eq": ["$rate_int", None]}, 0, 1]}, "window": window_7},
        }}},
        {"$set": {"moving_int": {"$cond": [{"$gt": ["$rate_n", 0]},
                                           {"$floor": {"$add": [{"$divide": ["$rate_sum", "$rate_n"]}, 0.5]}},
                                           None]}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": _DAY_FMT, "date": "$day"}},
            "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_int", None]}, None, {"$divide": ["$moving_int", 10000]}]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    require_month(month)
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department))
    ]
    return {"month": month, "items": items}


MAX_TREND_DAYS = 92


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(..., alias="from"),
                     date_to: str = Query(..., alias="to")):
    require_date(date_from)
    require_date(date_to)
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = list(db.employees.aggregate(department_trend_pipeline(department, start, end)))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract:
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
