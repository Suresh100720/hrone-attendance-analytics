"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Environment: MONGO_URI, MONGO_DB (optional local .env)
"""
import os
import re
import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId, json_util

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = PRESENT_STATUSES + ("ABSENT", "LEAVE")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------- Validation, dates and serialization ----------

def fail422(message: str):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        fail422(f"{label} must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail422("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return start, end


def validate_clock(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        fail422(f"{field} must be HH:MM")
    return value


def epoch_to_datetime(value: Any, field: str = "punched_at") -> datetime:
    # Only integer epoch milliseconds are accepted; booleans are not integers here.
    if isinstance(value, bool) or not isinstance(value, int):
        fail422(f"{field} must be an integer epoch-milliseconds value")
    if value < 100_000_000_000:
        fail422(f"{field} must be epoch milliseconds, not seconds")
    try:
        dt = datetime.fromtimestamp(value / 1000, tz=UTC)
        if dt.year < 1970 or dt.year > 9998:
            raise ValueError
    except (OverflowError, OSError, ValueError):
        fail422(f"{field} is out of range")
    return dt.replace(microsecond=0)


def utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_utc_naive(value: datetime) -> datetime:
    return utc_aware(value).replace(tzinfo=None, microsecond=0)


def millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(utc_aware(value).timestamp() * 1000)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def whole_minutes(delta: timedelta) -> int:
    return math.floor(delta.total_seconds() / 60)


def shift_start_instant(attendance_day: date, shift_start: str) -> datetime:
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(attendance_day, time(h, m), IST).astimezone(UTC)


def shift_end_instant(attendance_day: date, shift_start: str, shift_end: str) -> datetime:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = datetime.combine(attendance_day, time(sh, sm), IST)
    end_day = attendance_day + timedelta(days=1) if (eh, em) <= (sh, sm) else attendance_day
    return datetime.combine(end_day, time(eh, em), IST).astimezone(UTC)


def attendance_day_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = utc_aware(punch_in).astimezone(IST)
    day = local.date()
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: date) -> int:
    start = shift_start_instant(attendance_day, shift_start)
    minutes = whole_minutes(utc_aware(punch_in) - start)
    return minutes if minutes > 10 else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: date) -> int:
    end = shift_end_instant(attendance_day, shift_start, shift_end)
    minutes = whole_minutes(utc_aware(punch_out) - end)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round_half_up((utc_aware(punch_out) - utc_aware(punch_in)).total_seconds() / 3600, 2)


def serialize(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return millis(value)
    if isinstance(value, list):
        return [serialize(x) for x in value]
    if isinstance(value, dict):
        return {("id" if k == "_id" else k): serialize(v) for k, v in value.items() if k != "_id"}
    return value


def attendance_response(doc: dict) -> dict:
    out = dict(doc)
    out.pop("_id", None)
    out.pop("id", None)
    out.setdefault("history", [])
    out.setdefault("half_day", False)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    for key in ("punch_in", "punch_out"):
        out[key] = millis(out.get(key))
    for entry in out["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = millis(entry["at"])
        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime):
                    change[key] = millis(change[key])
    return out


def working_days(start: date, end: date) -> int:
    # Small calendar-only calculation; attendance analytics themselves use MongoDB aggregation.
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def mongo_json(value: Any) -> Any:
    # Make raw explain BSON values safe for JSON responses.
    return json_util.loads(json_util.dumps(value))


# ---------- Models ----------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    regularized_by: str
    reason: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


# ---------- Startup and health ----------

@app.on_event("startup")
def create_indexes():
    # Idempotent indexes. The compound unique index is the concurrency guard for punch-in.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_late")
    db.attendance_logs.create_index([("punch_in", DESCENDING)], name="ix_attendance_punch_in")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


# ---------- Employees ----------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    validate_clock(body.shift_start, "shift_start")
    validate_clock(body.shift_end, "shift_end")
    if body.shift_start == body.shift_end:
        fail422("shift_start and shift_end must differ")
    joined = parse_date(body.joined_on, "joined_on")
    doc = body.model_dump()
    doc["joined_on"] = joined.isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize(x) for x in items], "total": total, "page": page, "page_size": page_size}


# ---------- Attendance ----------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    if body.status not in PRESENT_STATUSES:
        fail422("status must be PRESENT, WFH, or ON_DUTY")
    ts = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_day_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day.isoformat(), "status": body.status,
        "punch_in": as_utc_naive(ts), "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    doc["_id"] = result.inserted_id
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    ts_db = as_utc_naive(ts)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts_db}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")
    punch_in_dt = utc_aware(record["punch_in"])
    if ts <= punch_in_dt or ts - punch_in_dt > timedelta(hours=24):
        fail422("punched_at must be after punch_in and no more than 24 hours later")
    day = parse_date(record["date"])
    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], day)
    half_day = work_hours < 4.50
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts_db, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Already punched out")
    record.update({"punch_out": ts_db, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day})
    return attendance_response(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        fail422("date_from must be on or before date_to")
    if status and status not in ALL_STATUSES:
        fail422("invalid status")
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [attendance_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date).isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not old:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    if body.status is not None and body.status not in ALL_STATUSES:
        fail422("invalid status")
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        fail422("ABSENT and LEAVE cannot include punch times")

    status = body.status if body.status is not None else old["status"]
    pin = epoch_to_datetime(body.punch_in, "punch_in") if body.punch_in is not None else old.get("punch_in")
    pout = epoch_to_datetime(body.punch_out, "punch_out") if body.punch_out is not None else old.get("punch_out")
    if status in ("ABSENT", "LEAVE"):
        pin = pout = None
    else:
        if pin is None:
            fail422("presence status requires punch_in")
        local_day = utc_aware(pin).astimezone(IST).date()
        # For overnight shifts, attendance date is shift start date; a post-midnight punch-in belongs to previous day.
        if attendance_day_for(pin, emp["shift_start"], emp["shift_end"]).isoformat() != day:
            fail422("punch_in must belong to the attendance date")
        if pout is not None and (utc_aware(pout) <= utc_aware(pin) or utc_aware(pout) - utc_aware(pin) > timedelta(hours=24)):
            fail422("punch_out must be after punch_in and within 24 hours")

    derived = {
        "status": status, "punch_in": as_utc_naive(pin) if pin else None,
        "punch_out": as_utc_naive(pout) if pout else None,
        "work_hours": compute_work_hours(pin, pout) if pin and pout and status in PRESENT_STATUSES else None,
        "late_minutes": compute_late_minutes(pin, emp["shift_start"], parse_date(day)) if pin and status in PRESENT_STATUSES else 0,
        "overtime_minutes": compute_overtime(pout, emp["shift_start"], emp["shift_end"], parse_date(day)) if pin and pout and status in PRESENT_STATUSES else 0,
        "half_day": (compute_work_hours(pin, pout) < 4.50) if pin and pout and status in PRESENT_STATUSES else False,
    }
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in tracked:
        before = old.get(key, False if key == "half_day" else 0 if key in ("late_minutes", "overtime_minutes") else None)
        after = derived[key]
        if isinstance(before, datetime):
            before = as_utc_naive(before)
        if isinstance(after, datetime):
            after = as_utc_naive(after)
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes:
        fail422("request does not change the record")
    entry = {
    "at": datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None),
    "by": body.regularized_by,
    "reason": body.reason,
    "changes": changes,
}
    result = db.attendance_logs.update_one(
        {"_id": old["_id"], "history": old.get("history", [])},
        {"$set": derived, "$push": {"history": entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry")
    updated = db.attendance_logs.find_one({"_id": old["_id"]})
    return attendance_response(updated)


# ---------- MongoDB aggregation helpers ----------

def month_bounds_strings(month: str):
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat()


def present_fraction_expr():
    return {"$cond": [
        {"$in": ["$status", list(PRESENT_STATUSES)]},
        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
        0.0,
    ]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    actual_start = max(start, parse_date(emp["joined_on"]))
    wd = working_days(actual_start, end) if actual_start <= end else 0
    # Aggregate all record-based metrics in MongoDB. Calendar denominator is computed from the employee's join date.
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]},
                          {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                          {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    row = agg[0] if agg else {}
    pct = round_half_up(row.get("present_days", 0) / wd * 100, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd,
            "present_days": row.get("present_days", 0), "leave_days": row.get("leave_days", 0),
            "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0),
            "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    emp_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]},
                {"$lte": ["$date", end.isoformat()]}]}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]},
                                  {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                  {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
                    "hours_sum": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]},
                        "$work_hours", 0]}},
                    "hours_count": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }}],
            "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
            "hours_sum": {"$sum": {"$ifNull": ["$stats.hours_sum", 0]}},
            "hours_count": {"$sum": {"$ifNull": ["$stats.hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, {"$round": [{"$divide": ["$hours_sum", "$hours_count"]}, 2]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
    ]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        fail422("to must be on or after from")
    if (end - start).days + 1 > 92:
        fail422("date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")
    # Database-side daily aggregation and gap filling. Employee headcount is joined for each generated day.
    start_dt = datetime.combine(start, time.min, UTC).replace(tzinfo=None)
    end_dt = datetime.combine(
    end + timedelta(days=1),
    time.min,
    UTC
    ).replace(tzinfo=None)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [
            {"$in": ["$status", list(PRESENT_STATUSES)]},
            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]} }},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "count"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
        {"$sort": {"date": 1}},
    ]
    # $densify bounds use dates converted from calendar strings; the pipeline intentionally performs no Python day loop.
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "items": items}


# ---------- Explain plans ----------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        fail422("unknown endpoint")
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        explain = db.command("explain", {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats")
        collection = "attendance_logs"
    else:
        if not month and endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
            fail422("month is required")
        if endpoint == "employee_monthly" and not emp_code:
            fail422("emp_code is required")
        if endpoint == "department_trend" and (not department or not from_date or not to_date):
            fail422("department, from and to are required")
        if month:
            start, end = parse_month(month)
            lo, hi = start.isoformat(), end.isoformat()
        if endpoint == "employee_monthly":
            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$gte": lo, "$lte": hi},
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
                                            {"$in": ["$status", list(PRESENT_STATUSES)]},
                                            {
                                                "$gte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    2,
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    6,
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$half_day", True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        },
                        "leave_days": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$status", "LEAVE"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                        "total_late_minutes": {
                            "$sum": {"$ifNull": ["$late_minutes", 0]}
                        },
                        "total_overtime_minutes": {
                            "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                        },
                    }
                },
            ]
            collection = "attendance_logs"

        
        elif endpoint == "department_summary":
            q = {"joined_on": {"$lte": hi}}
            if department is not None:
                q["department"] = department

            pipeline = [
                {"$match": q},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$emp_code", "$$code"]},
                                            {"$gte": ["$date", lo]},
                                            {"$lte": ["$date", hi]},
                                        ]
                                    }
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
                                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                                                    ]
                                                },
                                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                                0,
                                            ]
                                        }
                                    },
                                    "hours_sum": {
                                        "$sum": {
                                            "$cond": [
                                                {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]},
                                                "$work_hours",
                                                0,
                                            ]
                                        }
                                    },
                                    "hours_count": {
                                        "$sum": {
                                            "$cond": [
                                                {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]},
                                                1,
                                                0,
                                            ]
                                        }
                                    },
                                    "late_count": {
                                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                                    },
                                    "total_late_minutes": {
                                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                                    },
                                    "leave_count": {
                                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                                    },
                                    "on_duty_count": {
                                        "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                                    },
                                }
                            },
                        ],
                        "as": "stats",
                    }
                },
                {
                    "$set": {
                        "stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}
                    }
                },
                {
                    "$group": {
                        "_id": "$department",
                        "headcount": {"$sum": 1},
                        "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
                        "hours_sum": {"$sum": {"$ifNull": ["$stats.hours_sum", 0]}},
                        "hours_count": {"$sum": {"$ifNull": ["$stats.hours_count", 0]}},
                        "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
                        "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
                        "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
                    }
                },
                {"$sort": {"_id": 1}},
                {
                    "$project": {
                        "_id": 0,
                        "department": "$_id",
                        "headcount": 1,
                        "present_days": 1,
                        "avg_work_hours": {
                            "$cond": [
                                {"$gt": ["$hours_count", 0]},
                                {"$round": [{"$divide": ["$hours_sum", "$hours_count"]}, 2]},
                                None,
                            ]
                        },
                        "late_count": 1,
                        "total_late_minutes": 1,
                        "leave_count": 1,
                        "on_duty_count": 1,
                    }
                },
            ]
            collection = "employees"


        elif endpoint == "late_leaderboard":
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": lo, "$lte": hi},
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
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
            ]

            if department is not None:
                pipeline.append(
                    {"$match": {"employee.department": department}}
                )

            pipeline.extend([
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$employee.name",
                        "department": "$employee.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
                {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            ])

            collection = "attendance_logs"

        else:
            start_d = parse_date(from_date, "from")
            end_d = parse_date(to_date, "to")

            if end_d < start_d:
                fail422("to must be on or after from")

            if (end_d - start_d).days + 1 > 92:
                fail422("date range cannot exceed 92 days")

            start_dt = datetime.combine(start_d, time.min, UTC).replace(tzinfo=None)
            end_dt = datetime.combine(
                end_d + timedelta(days=1), time.min, UTC
            ).replace(tzinfo=None)

            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": start_d.isoformat(),
                            "$lte": end_d.isoformat(),
                        }
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {
                    "$group": {
                        "_id": "$date",
                        "present_count": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {
                                        "$cond": [
                                            {"$eq": ["$half_day", True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$late_minutes", 0]},
                                            0,
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
                    "$project": {
                        "_id": 0,
                        "day": {
                            "$dateFromString": {"dateString": "$_id"}
                        },
                        "present_count": 1,
                        "late_count": 1,
                    }
                },
                {
                    "$densify": {
                        "field": "day",
                        "range": {
                            "step": 1,
                            "unit": "day",
                            "bounds": [start_dt, end_dt],
                        },
                    }
                },
                {
                    "$set": {
                        "date": {
                            "$dateToString": {
                                "date": "$day",
                                "format": "%Y-%m-%d",
                            }
                        },
                        "present_count": {
                            "$ifNull": ["$present_count", 0]
                        },
                        "late_count": {"$ifNull": ["$late_count", 0]},
                        "is_working_day": {
                            "$and": [
                                {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                                {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                            ]
                        },
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "let": {"d": "$date"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$department",
                                                    department,
                                                ]
                                            },
                                            {"$lte": ["$joined_on", "$$d"]},
                                        ]
                                    }
                                }
                            },
                            {"$count": "count"},
                        ],
                        "as": "hc",
                    }
                },
                {
                    "$set": {
                        "headcount": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$hc.count", 0]},
                                0,
                            ]
                        }
                    }
                },
                {
                    "$set": {
                        "attendance_rate": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$is_working_day",
                                        {"$gt": ["$headcount", 0]},
                                    ]
                                },
                                {
                                    "$round": [
                                        {
                                            "$divide": [
                                                "$present_count",
                                                "$headcount",
                                            ]
                                        },
                                        4,
                                    ]
                                },
                                None,
                            ]
                        }
                    }
                },
                {
                    "$setWindowFields": {
                        "sortBy": {"day": 1},
                        "output": {
                            "moving_avg_7d": {
                                "$avg": "$attendance_rate",
                                "window": {"documents": [-6, 0]},
                            }
                        },
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "date": 1,
                        "is_working_day": 1,
                        "headcount": 1,
                        "present_count": 1,
                        "late_count": 1,
                        "attendance_rate": 1,
                        "moving_avg_7d": {
                            "$round": ["$moving_avg_7d", 4]
                        },
                    }
                },
                {"$sort": {"date": 1}},
            ]

            collection = "attendance_logs"

        explain = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": mongo_json(explain)}
