"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (reads from .env if present, environment variables take precedence)
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ensure_indexes():
    try:
        # employees collection
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        # attendance_logs collection
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    except Exception as e:
        print(f"Warning during index creation: {e}")

ensure_indexes()


# --------------------------------------------------------------------------- #
# Lifespan: Startup Index Creation & Shutdown
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers: Conversions & Date Calculations
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a Python datetime (UTC or naive) into integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def epoch_ms_to_dt(ms: int) -> datetime:
    """Converts epoch milliseconds into UTC datetime, truncated to whole seconds (R1)."""
    secs = ms // 1000
    return datetime.fromtimestamp(secs, tz=timezone.utc)


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Rounds floating-point numbers half-up (R8) using Python's Decimal."""
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("10") ** -decimals, rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Computes the IST attendance date ('YYYY-MM-DD').
    For overnight shifts (shift_end <= shift_start), an early-morning punch-in
    before shift_end belongs to the shift that started the previous day.
    """
    p_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    h_end, m_end = map(int, shift_end.split(":"))
    if is_overnight and p_ist.time() < time(h_end, m_end):
        return (p_ist.date() - timedelta(days=1)).isoformat()
    return p_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 mins. Late ONLY if arrival is strictly > 10:00 (600s) after shift_start.
    When late, late_minutes = whole minutes (floor) since shift_start.
    """
    p_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    d = datetime.fromisoformat(attendance_date).date()
    start_dt = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)
    diff_secs = (p_ist - start_dt).total_seconds()
    if diff_secs > 600:
        return int(diff_secs // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime = whole minutes between shift_end and punch_out, counted ONLY when >= 30 mins.
    Handles overnight shifts where shift_end falls on the next calendar day.
    """
    p_ist = punch_out_utc.astimezone(IST)
    d = datetime.fromisoformat(attendance_date).date()
    h_end, m_end = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        d = d + timedelta(days=1)
    end_dt = datetime(d.year, d.month, d.day, h_end, m_end, 0, tzinfo=IST)
    diff_secs = (p_ist - end_dt).total_seconds()
    if diff_secs <= 0:
        return 0
    minutes = int(diff_secs // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_secs = int((punch_out_utc - punch_in_utc).total_seconds())
    d = Decimal(diff_secs) / Decimal(3600)
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true if rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Counts Mon-Fri working days in the month from joined_on.
    """
    year, month = map(int, month_str.split("-"))
    start_of_month = date(year, month, 1)
    if month == 12:
        end_of_month = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_of_month = date(year, month + 1, 1) - timedelta(days=1)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > end_of_month:
        return 0

    start_date = max(start_of_month, joined_date)
    working_days = 0
    curr = start_date
    while curr <= end_of_month:
        if curr.weekday() < 5:  # Mon-Fri
            working_days += 1
        curr += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Document Serializers for API responses
# --------------------------------------------------------------------------- #
def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return {}
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return {}

    raw_history = doc.get("history") or []
    history = []
    for item in raw_history:
        at_val = dt_to_epoch_ms(item.get("at"))
        raw_changes = item.get("changes") or {}
        changes = {}
        for field, change in raw_changes.items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = dt_to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = dt_to_epoch_ms(to_val)
            changes[field] = {"from": from_val, "to": to_val}
        history.append({
            "at": at_val,
            "by": item.get("by"),
            "reason": item.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas with OpenAPI Validations
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employees Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc)
    # Truncate created_at to whole seconds
    now_utc = datetime.fromtimestamp(int(now_utc.timestamp()), tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_dt(body.punched_at)
    else:
        now_utc = datetime.now(timezone.utc)
        ts_utc = datetime.fromtimestamp(int(now_utc.timestamp()), tz=timezone.utc)

    attendance_date = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_dt(body.punched_at)
    else:
        now_utc = datetime.now(timezone.utc)
        ts_utc = datetime.fromtimestamp(int(now_utc.timestamp()), tz=timezone.utc)

    # Find the most recent record whose punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_utc}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    p_in = rec["punch_in"]
    if p_in.tzinfo is None:
        p_in = p_in.replace(tzinfo=timezone.utc)

    if ts_utc <= p_in:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if (ts_utc - p_in).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(p_in, ts_utc)
    overtime_minutes = compute_overtime(ts_utc, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="already punched out")

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=status.HTTP_200_OK)
def regularize_attendance(
    emp_code: str = Path(..., description="Employee business code"),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Attendance date YYYY-MM-DD"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for date")

    # ABSENT / LEAVE rule
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="Punch times cannot be supplied when status is ABSENT or LEAVE")

    target_status = body.status if body.status is not None else rec.get("status")
    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
    else:
        target_punch_in = epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        target_punch_out = epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on record's attendance date
        calc_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if target_punch_out is not None:
            if target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)
            if target_punch_out <= target_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        target_late = 0
        target_work_hours = None
        target_overtime = 0
        target_half_day = False
    else:
        target_late = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime = compute_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date)
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime = 0
            target_half_day = False

    # Check for actual changes
    field_pairs = [
        ("status", rec.get("status"), target_status),
        ("punch_in", normalize_dt(rec.get("punch_in")), target_punch_in),
        ("punch_out", normalize_dt(rec.get("punch_out")), target_punch_out),
        ("work_hours", rec.get("work_hours"), target_work_hours),
        ("late_minutes", rec.get("late_minutes", 0), target_late),
        ("overtime_minutes", rec.get("overtime_minutes", 0), target_overtime),
        ("half_day", rec.get("half_day", False), target_half_day),
    ]

    def are_different(v1, v2):
        if v1 is None and v2 is None:
            return False
        if v1 is None or v2 is None:
            return True
        if isinstance(v1, datetime) and isinstance(v2, datetime):
            return int(normalize_dt(v1).timestamp()) != int(normalize_dt(v2).timestamp())
        return v1 != v2

    changes = {}
    for fname, old_v, new_v in field_pairs:
        if are_different(old_v, new_v):
            changes[fname] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change any fields")

    now_utc = datetime.now(timezone.utc)
    now_utc = datetime.fromtimestamp(int(now_utc.timestamp()), tz=timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = rec.get("history") or []
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": current_history},
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late,
                "overtime_minutes": target_overtime,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(..., description="Employee code"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month YYYY-MM"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days(month, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        {
            "$addFields": {
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = data["present_days"]
        leave_days = data["leave_days"]
        late_count = data["late_count"]
        total_late = data["total_late_minutes"]
        total_ot = data["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, m = map(int, month.split("-"))
    if m == 12:
        end_date = f"{year}-12-31"
    else:
        end_date = (date(year, m + 1, 1) - timedelta(days=1)).isoformat()

    match_stage = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-31"]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "emp_present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$logs.day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "emp_late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "emp_total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "emp_leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "emp_on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "total_work_hours": {"$sum": "$work_hours_sum"},
                "total_work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        wh_cnt = r.get("total_work_hours_count", 0)
        wh_sum = r.get("total_work_hours", 0.0)
        avg_wh = round_half_up(wh_sum / wh_cnt, 2) if wh_cnt > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(..., description="Department name"),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (d_to - d_from).days > 91:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Get department employees
    dept_employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in dept_employees]

    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_bound_dt = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date"}}
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_bound_dt],
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
    ]

    day_stats = {d["date"]: d for d in db.attendance_logs.aggregate(pipeline)}

    # Construct the continuous day-by-day series
    items = []
    curr = d_from
    while curr <= d_to:
        ds = curr.isoformat()
        st = day_stats.get(ds, {"present_count": 0.0, "late_count": 0})
        is_working_day = curr.weekday() < 5
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= ds)

        present_count = st["present_count"]
        late_count = st["late_count"]

        if is_working_day and headcount > 0:
            rate = round_half_up(present_count / headcount, 4)
        else:
            rate = None

        items.append({
            "date": ds,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": None,
        })
        curr += timedelta(days=1)

    # Compute moving average 7d over non-null rates in window [max(0, i-6) .. i]
    for i, item in enumerate(items):
        window = items[max(0, i - 6) : i + 1]
        valid_rates = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if valid_rates:
            item["moving_avg_7d"] = round_half_up(sum(valid_rates) / len(valid_rates), 4)
        else:
            item["moving_avg_7d"] = None

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        plan = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
            {
                "$addFields": {
                    "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        match_stage = {"joined_on": {"$lte": f"{month}-31"}}
        if department:
            match_stage["department"] = department
        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"ecode": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$ecode"]},
                                        {"$gte": ["$date", f"{month}-01"]},
                                        {"$lte": ["$date", f"{month}-31"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {"$sum": 1},
                }
            },
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
