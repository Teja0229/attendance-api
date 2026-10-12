"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Everything lives in this one file:
  1. configuration and Mongo connection
  2. time helpers and business rules R1-R5
  3. request models and validation helpers
  4. query / pipeline builders (shared by the endpoints AND /admin/explain)
  5. endpoints
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError, OperationFailure

# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------
load_dotenv(override=False)  # a real environment variable always wins over .env

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes, never naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUS = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME = 30
HALF_DAY_BELOW = Decimal("4.50")

# Index keys (lists, for create_index). Queries pass them as dicts: PyMongo's aggregate() does not
# convert a list-of-tuples hint, MongoDB then answers "Hint must be a string or an object".
IDX_EMP_EMPCODE = [("emp_code", 1)]
IDX_EMP_DEPT_CODE = [("department", 1), ("emp_code", 1)]
IDX_EMP_DEPT_JOINED = [("department", 1), ("joined_on", 1)]
IDX_EMP_JOINED = [("joined_on", 1)]
IDX_ATT_EMP_DATE = [("emp_code", 1), ("date", 1)]
IDX_ATT_DATE_EMP = [("date", -1), ("emp_code", 1)]
IDX_ATT_STATUS_DATE = [("status", 1), ("date", -1), ("emp_code", 1)]
IDX_ATT_LATE = [("date", 1), ("late_minutes", 1), ("emp_code", 1)]
IDX_ATT_EMP_PUNCHIN = [("emp_code", 1), ("punch_in", -1)]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the index already exists."""
    global _indexes_ready
    db.employees.create_index(IDX_EMP_EMPCODE, unique=True)
    db.employees.create_index(IDX_EMP_DEPT_CODE)
    db.employees.create_index(IDX_EMP_DEPT_JOINED)
    db.employees.create_index(IDX_EMP_JOINED)
    db.attendance_logs.create_index(IDX_ATT_EMP_DATE, unique=True)
    db.attendance_logs.create_index(IDX_ATT_DATE_EMP)
    db.attendance_logs.create_index(IDX_ATT_STATUS_DATE)
    db.attendance_logs.create_index(IDX_ATT_LATE)
    db.attendance_logs.create_index(IDX_ATT_EMP_PUNCHIN)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # stay up; /health reports 503 and retries index creation
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# 2. Time helpers and business rules (R1 - R5)
# ---------------------------------------------------------------------------
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: seeded naive values are UTC by contract
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts that began yesterday roll back a day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < hm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_dt(day: str, shift_start: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), hm(shift_start), tzinfo=IST)


def shift_end_dt(day: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(date.fromisoformat(day), hm(shift_end), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, day: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    secs = int((punch_in - shift_start_dt(day, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, only when at least 30."""
    secs = int((punch_out - shift_end_dt(day, shift_start, shift_end)).total_seconds())
    mins = secs // 60
    return mins if mins >= MIN_OVERTIME else 0


def round_half_up(value: Decimal | float | int | str, places: int) -> Decimal:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals, half-up (exact Decimal arithmetic, no float error)."""
    secs = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(secs) / Decimal(3600), 2)


def derive(emp: dict, status: str, day: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four stored derived fields for a final (status, punch_in, punch_out)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = float(wh)
        out["overtime_minutes"] = compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = wh < HALF_DAY_BELOW
    return out


# ---------------------------------------------------------------------------
# 3. Models and validation helpers
# ---------------------------------------------------------------------------
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]  # rejects seconds/floats/strings
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


def bad(loc: str, msg: str, where: str = "query") -> RequestValidationError:
    """A 422 with FastAPI's standard body shape."""
    return RequestValidationError([{"type": "value_error", "loc": (where, loc), "msg": msg, "input": None}])


def check_date(value: str, loc: str, where: str = "query") -> str:
    if not DATE_RE.match(value):
        raise bad(loc, "date must be YYYY-MM-DD", where)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise bad(loc, "not a real calendar date", where)
    return value


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(first: str, last: str) -> int:
    """Mon-Fri days in [first, last] inclusive (R7). Pure calendar arithmetic, no holidays."""
    d, end, n = date.fromisoformat(first), date.fromisoformat(last), 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Serialisation: Mongo documents -> API shapes (never expose _id, instants as epoch ms)
# ---------------------------------------------------------------------------
def employee_out(doc: dict) -> dict:
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


def _history_value(field: str, value: Any) -> Any:
    return dt_to_ms(value) if field in ("punch_in", "punch_out") else value


def attendance_out(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        history.append(
            {
                "at": dt_to_ms(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {"from": _history_value(k, v.get("from")), "to": _history_value(k, v.get("to"))}
                    for k, v in (h.get("changes") or {}).items()
                },
            }
        )
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
        "history": history,
    }


def page_of(items: list, total: int, page: int, page_size: int) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN)]


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# ---------------------------------------------------------------------------
# 4. Query / pipeline builders (shared by the endpoints and /admin/explain)
# ---------------------------------------------------------------------------
def build_attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str],
    page: int, page_size: int,
) -> dict:
    if date_from:
        check_date(date_from, "date_from")
    if date_to:
        check_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise bad("date_from", "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    rng = {}
    if date_from:
        rng["$gte"] = date_from
    if date_to:
        rng["$lte"] = date_to
    if rng:
        q["date"] = rng
    if status:
        q["status"] = status
    if emp_code:
        hint = dict(IDX_ATT_EMP_DATE)
    elif status:
        hint = dict(IDX_ATT_STATUS_DATE)
    else:
        hint = dict(IDX_ATT_DATE_EMP)
    return {
        "filter": q,
        "sort": [("date", -1), ("emp_code", 1)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": hint,
    }


def _present(var: str) -> dict:
    return {"$in": [f"${var}.status", PRESENCE]}


def _weekday(var: str) -> dict:
    # $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6
    parsed = {"$dateFromString": {"dateString": f"${var}.date", "format": "%Y-%m-%d", "onError": None}}
    return {"$in": [{"$dayOfWeek": parsed}, [2, 3, 4, 5, 6]]}


def _presence_weight(var: str) -> dict:
    return {"$cond": [{"$eq": [{"$ifNull": [f"${var}.half_day", False]}, True]}, 0.5, 1]}


def build_monthly_pipeline(emp_code: str, month: str) -> tuple[str, list, dict]:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_present("$ROOT"), _weekday("$ROOT")]}, _presence_weight("$ROOT"), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return "attendance_logs", pipeline, dict(IDX_ATT_EMP_DATE)


def build_department_summary_pipeline(month: str, department: Optional[str]) -> tuple[str, list, dict]:
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}  # R9: joined on or before the end of the month
    if department:
        match["department"] = department
    hint = dict(IDX_EMP_DEPT_JOINED if department else IDX_EMP_JOINED)
    L = "l"  # loop variable name inside $map / $filter
    logs_present = {"$and": [_present(f"${L}"), {"$isNumber": f"$${L}.work_hours"}]}
    pipeline = [
        {"$match": match},
        # Driven from employees so people with zero logs still produce a row (and count in headcount).
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": L, "in": {
                "$cond": [{"$and": [_present(f"${L}"), _weekday(f"${L}")]}, _presence_weight(f"${L}"), 0]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": L, "cond": {
                "$gt": [{"$ifNull": [f"$${L}.late_minutes", 0]}, 0]}}}},
            "late_minutes": {"$sum": {"$map": {"input": "$logs", "as": L, "in": {
                "$cond": [{"$gt": [{"$ifNull": [f"$${L}.late_minutes", 0]}, 0]}, f"$${L}.late_minutes", 0]}}}},
            "leave_count": {"$size": {"$filter": {"input": "$logs", "as": L, "cond": {"$eq": [f"$${L}.status", "LEAVE"]}}}},
            "on_duty_count": {"$size": {"$filter": {"input": "$logs", "as": L, "cond": {"$eq": [f"$${L}.status", "ON_DUTY"]}}}},
            "wh_count": {"$size": {"$filter": {"input": "$logs", "as": L, "cond": logs_present}}},
            "wh_sum": {"$sum": {"$map": {"input": "$logs", "as": L, "in": {
                "$cond": [logs_present, {"$toDecimal": f"$${L}.work_hours"}, {"$toDecimal": 0}]}}}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "wh_count": {"$sum": "$wh_count"},
            "wh_sum": {"$sum": "$wh_sum"},
        }},
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline, hint


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> tuple[str, list, dict]:
    first, last = month_bounds(month)
    pipeline: list = [
        # Covered by IDX_ATT_LATE: date range + late_minutes > 0, only emp_code / late_minutes are read.
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return "attendance_logs", pipeline, dict(IDX_ATT_LATE)


def build_trend_pipeline(department: str, d_from: str, d_to: str) -> tuple[str, list, dict]:
    n_days = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1
    present_w = {"$cond": [_present("logs"), _presence_weight("logs"), 0]}
    pipeline = [
        {"$match": {"department": department}},
        {"$facet": {
            # headcount increments: how many employees joined on each date
            "hc": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            # per-day stats from the logs of this department's employees (index emp_code+date)
            "days": [
                {"$lookup": {
                    "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                        {"$project": {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1}},
                    ],
                    "as": "logs",
                }},
                {"$unwind": "$logs"},
                {"$group": {
                    "_id": "$logs.date",
                    "present": {"$sum": present_w},
                    "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
        }},
        # Gap filling in the database: one row per calendar day in [from, to], with or without records.
        {"$project": {"rows": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$let": {
                "vars": {"dt": {"$dateAdd": {
                    "startDate": {"$dateFromString": {"dateString": d_from, "format": "%Y-%m-%d"}},
                    "unit": "day", "amount": "$$i"}}},
                "in": {"$let": {
                    "vars": {
                        "ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}},
                        "dow": {"$dayOfWeek": "$$dt"},
                    },
                    "in": {"$let": {
                        "vars": {"hit": {"$first": {"$filter": {
                            "input": "$days", "as": "x", "cond": {"$eq": ["$$x._id", "$$ds"]}}}}},
                        "in": {
                            "date": "$$ds",
                            "is_working_day": {"$in": ["$$dow", [2, 3, 4, 5, 6]]},
                            "headcount": {"$sum": {"$map": {
                                "input": {"$filter": {"input": "$hc", "as": "h", "cond": {"$lte": ["$$h._id", "$$ds"]}}},
                                "as": "h", "in": "$$h.n"}}},
                            "present_count": {"$ifNull": ["$$hit.present", 0]},
                            "late_count": {"$ifNull": ["$$hit.late", 0]},
                        },
                    }},
                }},
            }},
        }}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # 7-row window (current + 6 preceding) inside the range; $avg ignores nulls.
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, dict(IDX_EMP_DEPT_JOINED)


def _num(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value or 0))


# ---------------------------------------------------------------------------
# 5. Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the only duplicate check
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return page_of([employee_out(d) for d in cursor], total, page, page_size)


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = rec["punch_in"]
    if p_in.tzinfo is None:
        p_in = p_in.replace(tzinfo=UTC)
    if ts <= p_in:
        raise bad("punched_at", "punched_at must be after punch_in", "body")
    if ts - p_in > timedelta(hours=24):
        raise bad("punched_at", "punch-out is more than 24 hours after punch-in", "body")
    fields = derive(emp, rec["status"], rec["date"], p_in, ts)
    fields["punch_out"] = ts
    # The filter punch_out: None makes the write itself the arbiter: only one concurrent request matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=True
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    total = (
        coll.count_documents(spec["filter"], hint=spec["hint"]) if spec["filter"] else coll.estimated_document_count()
    )
    cursor = (
        coll.find(spec["filter"], {"_id": 0})
        .hint(spec["hint"]).sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])
    )
    return page_of([attendance_out(d) for d in cursor], total, page, page_size)


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    check_date(day, "date", "path")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise bad("punch_in", f"{new_status} clears punch times; do not supply them", "body")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise bad("punch_in", "a presence status requires a punch_in", "body")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            raise bad("punch_in", "punch_in must stay on the record's attendance date", "body")
        if (body.punch_in is not None or body.punch_out is not None) and new_out is not None:
            if new_out <= new_in:
                raise bad("punch_out", "punch_out must be after punch_in", "body")
            if new_out - new_in > timedelta(hours=24):
                raise bad("punch_out", "punch_out must be within 24 hours of punch_in", "body")

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise bad("body", "the request changes nothing", "body")

    new_fields = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new_fields.update(derive(emp, new_status, day, new_in, new_out))
    old_fields = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_fields[k], "to": v} for k, v in new_fields.items() if old_fields[k] != v}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only applies if the record is still exactly as we read it
    # (same history length, same status and punches). A concurrent edit makes this match nothing -> 409,
    # so no history entry is ever lost or overwritten.
    guard: dict = {"_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out}
    if "history" in rec and rec["history"] is not None:
        guard["history"] = {"$size": len(rec["history"])}
    else:
        guard["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_fields, "$push": {"history": entry}}, return_document=True
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return attendance_out(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    emp = get_employee(emp_code)
    coll, pipeline, hint = build_monthly_pipeline(emp_code, month)
    row = next(iter(db[coll].aggregate(pipeline, hint=hint)), None) or {}
    first, last = month_bounds(month)
    start = max(first, emp["joined_on"])  # R7: count from joined_on for mid-month joiners
    working_days = count_working_days(start, last) if start <= last else 0
    present = _num(row.get("present_days", 0))
    pct = round_half_up(present / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(round_half_up(present, 2)),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": float(pct) if pct is not None else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    coll, pipeline, hint = build_department_summary_pipeline(month, department)
    items = []
    for r in db[coll].aggregate(pipeline, hint=hint):
        avg = None
        if r["wh_count"]:
            avg = float(round_half_up(_num(r["wh_sum"]) / Decimal(r["wh_count"]), 2))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(round_half_up(_num(r["present_days"]), 2)),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthQ,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    coll, pipeline, hint = build_leaderboard_pipeline(month, limit, department)
    items = [
        {
            "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
        }
        for r in db[coll].aggregate(pipeline, hint=hint)
    ]
    return {"month": month, "items": items}


def validate_trend_range(d_from: str, d_to: str) -> None:
    check_date(d_from, "from")
    check_date(d_to, "to")
    if d_to < d_from:
        raise bad("to", "to must not be before from")
    if (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1 > 92:
        raise bad("to", "range must not exceed 92 days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    d_from: Annotated[str, Query(alias="from")],
    d_to: Annotated[str, Query(alias="to")],
):
    validate_trend_range(d_from, d_to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    coll, pipeline, hint = build_trend_pipeline(department, d_from, d_to)
    items = []
    for r in db[coll].aggregate(pipeline, hint=hint):
        rate = r.get("attendance_rate")
        if rate is not None:  # recompute from exact counts so half-up rounding is exact
            rate = float(round_half_up(_num(r["present_count"]) / Decimal(r["headcount"]), 4))
        ma = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": float(round_half_up(ma, 4)) if ma is not None else None,
        })
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    d_from: Annotated[Optional[str], Query(alias="from")] = None,
    d_to: Annotated[Optional[str], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    def need(value, name):
        if value is None or value == "":
            raise bad(name, "field required")
        return value

    if endpoint == "attendance_list":
        spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll_name = "attendance_logs"
        inner = {
            "find": coll_name, "filter": spec["filter"], "sort": dict(spec["sort"]),
            "skip": spec["skip"], "limit": spec["limit"], "hint": spec["hint"],
        }
    else:
        if endpoint == "employee_monthly":
            coll_name, pipeline, hint = build_monthly_pipeline(need(emp_code, "emp_code"), need(month, "month"))
        elif endpoint == "department_summary":
            coll_name, pipeline, hint = build_department_summary_pipeline(need(month, "month"), department)
        elif endpoint == "late_leaderboard":
            coll_name, pipeline, hint = build_leaderboard_pipeline(need(month, "month"), limit, department)
        else:
            need(department, "department")
            validate_trend_range(need(d_from, "from"), need(d_to, "to"))
            coll_name, pipeline, hint = build_trend_pipeline(department, d_from, d_to)
        inner = {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}, "hint": hint}
    try:
        raw = db.command({"explain": inner, "verbosity": "executionStats"})
    except OperationFailure as exc:
        raise HTTPException(500, f"explain failed: {exc}")
    # Raw explain output contains BSON types (dates, Int64, Decimal128): make it plain JSON.
    plain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll_name, "explain": plain} 