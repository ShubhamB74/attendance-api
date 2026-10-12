"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Conventions (see DATA_MODEL.md / openapi.yaml, kept locally):
  * Instants are stored as tz-aware UTC BSON dates and exposed as integer epoch milliseconds.
  * Calendar dates ("YYYY-MM-DD") and shift times ("HH:MM") are plain strings in IST.
  * Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are computed here and stored.
"""
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
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
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
# TODO - the rest of the contract:
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
