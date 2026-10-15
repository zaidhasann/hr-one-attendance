import os
import re
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any, Union
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request, status, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

# Load local .env if present (environment variables take precedence)
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "hrone_db")

# Timezone: IST (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Global DB client and handles
mongo_client: Optional[MongoClient] = None
db = None

def get_client() -> MongoClient:
    global mongo_client
    if mongo_client is None:
        mongo_client = MongoClient(
            MONGO_URI,
            tz_aware=True,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000
        )
    return mongo_client

def get_db():
    global db
    if db is None:
        client = get_client()
        db = client[MONGO_DB]
    return db

def round_half_up(val: Union[float, Decimal, int], decimals: int = 2) -> float:
    d = Decimal(str(val))
    exp = Decimal("10") ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))

def round_half_up_decimal(d: Decimal, decimals: int = 2) -> Decimal:
    exp = Decimal("10") ** -decimals
    return d.quantize(exp, rounding=ROUND_HALF_UP)

# --- Timestamp & Shift Helpers ---
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000

def validate_epoch_ms(val: Any) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError("Timestamp must be an integer number of epoch milliseconds.")
    if val < MIN_EPOCH_MS or val > MAX_EPOCH_MS:
        raise ValueError(f"Timestamp {val} out of valid range [{MIN_EPOCH_MS}, {MAX_EPOCH_MS}].")
    return val

def truncate_ms_to_sec_ms(ms: int) -> int:
    # Truncate to whole second
    return (ms // 1000) * 1000

def ms_to_utc_dt(ms: int) -> datetime:
    truncated = truncate_ms_to_sec_ms(ms)
    return datetime.fromtimestamp(truncated / 1000.0, tz=timezone.utc)

def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return truncate_ms_to_sec_ms(ms)

def parse_hh_mm(t_str: str) -> tuple[int, int]:
    m = re.match(r"^(\d{2}):(\d{2})$", t_str)
    if not m:
        raise ValueError(f"Invalid time format: {t_str}")
    h, m_val = int(m.group(1)), int(m.group(2))
    if h < 0 or h > 23 or m_val < 0 or m_val > 59:
        raise ValueError(f"Invalid hour/minute: {t_str}")
    return h, m_val

def parse_iso_date(value: str, field_name: str = "date") -> date:
    """Parse a strict YYYY-MM-DD date, rejecting permissive ISO variants."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field_name} must use YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}: {value}") from exc


def get_attendance_date_for_punch(punch_in_utc_dt: datetime, shift_start: str, shift_end: str) -> str:
    # Convert punch_in to IST
    punch_ist = punch_in_utc_dt.astimezone(IST)
    p_date = punch_ist.date()
    p_time = punch_ist.time()
    
    s_h, s_m = parse_hh_mm(shift_start)
    e_h, e_m = parse_hh_mm(shift_end)
    start_t = time(s_h, s_m)
    end_t = time(e_h, e_m)
    
    is_overnight = end_t <= start_t
    if is_overnight:
        # If punch-in is earlier than shift_end, it belongs to previous day's shift
        if p_time < end_t:
            return (p_date - timedelta(days=1)).isoformat()
    return p_date.isoformat()

def compute_derived_attendance(
    punch_in_utc: Optional[datetime],
    punch_out_utc: Optional[datetime],
    att_date_str: str,
    shift_start: str,
    shift_end: str,
    status: str
) -> tuple[Optional[float], int, int, bool]:
    """
    Returns: (work_hours, late_minutes, overtime_minutes, half_day)
    """
    if status in ("ABSENT", "LEAVE") or punch_in_utc is None:
        return (None, 0, 0, False)
    
    # 1. Late minutes (R2)
    # shift_start on attendance date in IST
    att_date = date.fromisoformat(att_date_str)
    s_h, s_m = parse_hh_mm(shift_start)
    scheduled_start_ist = datetime.combine(att_date, time(s_h, s_m), tzinfo=IST)
    scheduled_start_utc = scheduled_start_ist.astimezone(timezone.utc)
    
    # Floor whole minutes elapsed from shift start
    diff_sec = (punch_in_utc - scheduled_start_utc).total_seconds()
    if diff_sec > 600:  # strictly more than 10 minutes (600s)
        late_minutes = max(0, int(diff_sec // 60))
    else:
        late_minutes = 0

    # 2. Punch out derived fields: work_hours, overtime, half_day
    if punch_out_utc is None:
        return (None, late_minutes, 0, False)

    # R4: (punch_out - punch_in) in seconds / 3600
    duration_sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw_hours = Decimal(str(duration_sec)) / Decimal("3600")
    work_hours = float(round_half_up_decimal(raw_hours, 2))

    # R5: half_day strictly less than 4.50
    half_day = work_hours < 4.50

    # R3: Overtime
    e_h, e_m = parse_hh_mm(shift_end)
    is_overnight = time(e_h, e_m) <= time(s_h, s_m)
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    scheduled_end_ist = datetime.combine(end_date, time(e_h, e_m), tzinfo=IST)
    scheduled_end_utc = scheduled_end_ist.astimezone(timezone.utc)
    
    ot_sec = (punch_out_utc - scheduled_end_utc).total_seconds()
    if ot_sec >= 1800:  # at least 30 minutes (1800s)
        overtime_minutes = max(0, int(ot_sec // 60))
    else:
        overtime_minutes = 0

    return (work_hours, late_minutes, overtime_minutes, half_day)

# --- Pydantic Models ---
EMP_CODE_REGEX = r"^EMP\d{4,6}$"
EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
HH_MM_REGEX = r"^(?:[01]\d|2[0-3]):[0-5]\d$"

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class EmployeeCreate(StrictModel):
    emp_code: str = Field(..., max_length=20)
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.match(EMP_CODE_REGEX, v):
            raise ValueError(f"emp_code '{v}' must match ^EMP\\d{{4,6}}$")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped or len(v) > 100:
            raise ValueError("name must be nonempty and at most 100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if len(v) > 120 or not re.match(EMAIL_REGEX, v) or " " in v:
            raise ValueError("email must be valid with no whitespace and at most 120 characters")
        return v

    @field_validator("department")
    @classmethod
    def validate_dept(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("department must be nonempty and at most 50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_times(cls, v: str) -> str:
        if not re.match(HH_MM_REGEX, v):
            raise ValueError(f"Invalid shift time '{v}', must be 24-hr HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            parse_iso_date(v, "joined_on")
        except ValueError as exc:
            raise ValueError(f"Invalid joined_on date '{v}', must be YYYY-MM-DD") from exc
        return v

    @model_validator(mode="after")
    def check_shift_diff(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be identical")
        return self

class PunchInRequest(StrictModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.match(EMP_CODE_REGEX, v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError(f"status '{v}' not permitted for punch-in. Allowed: PRESENT, WFH, ON_DUTY")
        return v

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms(v)

class PunchOutRequest(StrictModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.match(EMP_CODE_REGEX, v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms(v)

class RegularizeRequest(StrictModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError(f"Invalid status '{v}'")
        return v

    @field_validator("punch_in")
    @classmethod
    def validate_pin(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms(v)

    @field_validator("punch_out")
    @classmethod
    def validate_pout(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms(v)

# --- Serializers ---
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(created_at) if isinstance(created_at, datetime) else created_at
    }

def serialize_history_entry(h: Dict[str, Any]) -> Dict[str, Any]:
    at_val = h.get("at")
    if isinstance(at_val, datetime):
        at_ms = dt_to_ms(at_val)
    elif isinstance(at_val, int):
        at_ms = truncate_ms_to_sec_ms(at_val)
    else:
        at_ms = at_val

    raw_changes = h.get("changes", {})
    changes = {}
    for k, v in raw_changes.items():
        if isinstance(v, dict) and "from" in v and "to" in v:
            f_val = v["from"]
            t_val = v["to"]
            if k in ("punch_in", "punch_out"):
                f_val = dt_to_ms(f_val) if isinstance(f_val, datetime) else (truncate_ms_to_sec_ms(f_val) if isinstance(f_val, int) else f_val)
                t_val = dt_to_ms(t_val) if isinstance(t_val, datetime) else (truncate_ms_to_sec_ms(t_val) if isinstance(t_val, int) else t_val)
            changes[k] = {"from": f_val, "to": t_val}
        else:
            changes[k] = v

    return {
        "at": at_ms,
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes
    }

def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    p_in = doc.get("punch_in")
    p_out = doc.get("punch_out")
    
    # Normalize legacy missing fields
    work_hours = doc.get("work_hours")
    if work_hours is not None:
        work_hours = round_half_up(work_hours, 2)
    late_minutes = int(doc.get("late_minutes", 0))
    overtime_minutes = int(doc.get("overtime_minutes", 0))
    half_day = bool(doc.get("half_day", False))
    
    raw_history = doc.get("history") or []
    history = [serialize_history_entry(item) for item in raw_history]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(p_in) if isinstance(p_in, datetime) else p_in,
        "punch_out": dt_to_ms(p_out) if isinstance(p_out, datetime) else p_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": history
    }

# --- Database Startup Indexes ---
def init_db_indexes(database):
    """
    Creates all required indexes idempotently.
    """
    # 1. employees.emp_code unique
    database.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    # 2. employees department + emp_code
    database.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_emp_dept_code")
    # 3. employees department + joined_on
    database.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")
    # 4. employees joined_on
    database.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined")

    # 5. attendance_logs (emp_code, date) unique
    database.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_att_emp_date_unique")
    # 6. attendance_logs (emp_code, punch_in) for punch-out lookup
    database.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_att_emp_punchin")
    # 7. attendance_logs (date, emp_code) for date range filtering and sorting
    database.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_att_date_emp")
    # 8. attendance_logs (emp_code, date, status)
    database.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="idx_att_emp_date_status")

# --- FastAPI Lifespan ---
indexes_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Required indexes enforce uniqueness and support contract-critical queries.
    # Startup fails rather than silently serving without these guarantees.
    global indexes_ready
    database = get_db()
    init_db_indexes(database)
    indexes_ready = True
    try:
        yield
    finally:
        indexes_ready = False
        global mongo_client
        if mongo_client:
            mongo_client.close()
            mongo_client = None

app = FastAPI(title="HROne Employee Attendance & Analytics API", lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Ensure errors are JSON serializable
    errors = []
    for err in exc.errors():
        err_copy = dict(err)
        if "ctx" in err_copy and isinstance(err_copy["ctx"], dict):
            # serialize non-serializable objects like ValueError
            err_copy["ctx"] = {k: str(v) for k, v in err_copy["ctx"].items()}
        errors.append(err_copy)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": errors}
    )

# --- Endpoint 1: GET /health ---
@app.get("/health")
def health_check():
    if not indexes_ready:
        raise HTTPException(status_code=503, detail="Database indexes are not ready")
    try:
        client = get_client()
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

# --- Endpoint A: POST /employees ---
@app.post("/employees", status_code=201)
def create_employee(emp: EmployeeCreate):
    database = get_db()
    # Server clock UTC instant
    now_utc = datetime.now(timezone.utc)
    truncated_now_ms = truncate_ms_to_sec_ms(int(now_utc.timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(truncated_now_ms / 1000.0, tz=timezone.utc)

    doc = {
        "emp_code": emp.emp_code,
        "name": emp.name,
        "email": emp.email,
        "department": emp.department,
        "shift_start": emp.shift_start,
        "shift_end": emp.shift_end,
        "joined_on": emp.joined_on,
        "created_at": created_at_dt
    }

    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee with emp_code '{emp.emp_code}' already exists")
    except PyMongoError as pe:
        raise HTTPException(status_code=500, detail="Database error occurred")

    return serialize_employee(doc)

# --- Endpoint B: GET /employees ---
@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    database = get_db()
    query: Dict[str, Any] = {}
    if department is not None:
        query["department"] = department

    total = database.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = database.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

# --- Endpoint C: POST /attendance/punch-in ---
@app.post("/attendance/punch-in", status_code=201)
def punch_in(req: PunchInRequest):
    database = get_db()
    emp = database.employees.find_one({"emp_code": req.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{req.emp_code}' not found")

    if req.punched_at is not None:
        p_ms = req.punched_at
    else:
        p_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    p_dt = ms_to_utc_dt(p_ms)
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    att_date = get_attendance_date_for_punch(p_dt, shift_start, shift_end)

    # Compute derived late_minutes
    work_hours, late_minutes, overtime_minutes, half_day = compute_derived_attendance(
        punch_in_utc=p_dt,
        punch_out_utc=None,
        att_date_str=att_date,
        shift_start=shift_start,
        shift_end=shift_end,
        status=req.status
    )

    doc = {
        "emp_code": req.emp_code,
        "date": att_date,
        "status": req.status,
        "punch_in": p_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Attendance record already exists for {req.emp_code} on {att_date}")
    except PyMongoError:
        raise HTTPException(status_code=500, detail="Database error occurred")

    return serialize_attendance(doc)

# --- Endpoint D: POST /attendance/punch-out ---
@app.post("/attendance/punch-out")
def punch_out(req: PunchOutRequest):
    database = get_db()
    emp = database.employees.find_one({"emp_code": req.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{req.emp_code}' not found")

    if req.punched_at is not None:
        p_ms = req.punched_at
    else:
        p_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    p_out_dt = ms_to_utc_dt(p_ms)

    # Find the most recent attendance record whose punch_in is <= punch_out
    recent_record = database.attendance_logs.find_one(
        {"emp_code": req.emp_code, "punch_in": {"$ne": None, "$lte": p_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )

    if not recent_record:
        raise HTTPException(status_code=404, detail="No eligible punch-in record found for punch-out")

    # If already punched out -> 409
    if recent_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record has already been punched out")

    p_in_dt = recent_record["punch_in"]
    if p_in_dt.tzinfo is None:
        p_in_dt = p_in_dt.replace(tzinfo=timezone.utc)
    else:
        p_in_dt = p_in_dt.astimezone(timezone.utc)

    if p_out_dt <= p_in_dt:
        raise HTTPException(status_code=422, detail="Punch-out timestamp must be strictly greater than punch-in")

    if (p_out_dt - p_in_dt).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="Punch-out cannot be more than 24 hours after punch-in")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    att_date = recent_record["date"]

    work_hours, late_minutes, overtime_minutes, half_day = compute_derived_attendance(
        punch_in_utc=p_in_dt,
        punch_out_utc=p_out_dt,
        att_date_str=att_date,
        shift_start=shift_start,
        shift_end=shift_end,
        status=recent_record["status"]
    )

    # Atomic update to avoid concurrent punch-out race
    res = database.attendance_logs.find_one_and_update(
        {
            "emp_code": req.emp_code,
            "date": att_date,
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": p_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not res:
        # Lost the race or already updated
        raise HTTPException(status_code=409, detail="Record was punched out concurrently or is no longer eligible")

    return serialize_attendance(res)

# --- Endpoint E: GET /attendance ---
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    database = get_db()
    query: Dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        date_q = {}
        if date_from is not None:
            try:
                parse_iso_date(date_from, "date_from")
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid date_from format: {date_from}")
            date_q["$gte"] = date_from
        if date_to is not None:
            try:
                parse_iso_date(date_to, "date_to")
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid date_to format: {date_to}")
            date_q["$lte"] = date_to
        
        if date_from is not None and date_to is not None:
            if parse_iso_date(date_from, "date_from") > parse_iso_date(date_to, "date_to"):
                raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

        query["date"] = date_q

    if status is not None:
        if status not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
        query["status"] = status

    total = database.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = database.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

# --- Endpoint F: PATCH /attendance/{emp_code}/{date} ---
@app.patch("/attendance/{emp_code}/{att_date}")
def regularize_attendance(
    emp_code: str = Path(..., pattern=EMP_CODE_REGEX),
    att_date: str = Path(...),
    req: RegularizeRequest = ...
):
    try:
        parse_iso_date(att_date, "att_date")
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date in path: {att_date}")

    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    existing = database.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not existing:
        raise HTTPException(status_code=404, detail=f"Attendance record for '{emp_code}' on '{att_date}' not found")

    # Determine candidate new values
    cur_status = existing["status"]
    cur_pin = existing.get("punch_in")
    cur_pout = existing.get("punch_out")
    cur_work_hours = existing.get("work_hours")
    cur_late_mins = existing.get("late_minutes", 0)
    cur_ot_mins = existing.get("overtime_minutes", 0)
    cur_half_day = existing.get("half_day", False)
    cur_history = existing.get("history") or []

    new_status = req.status if req.status is not None else cur_status

    # Rule 3: ABSENT or LEAVE clears both punch times. Supplying a punch time in same request is 422
    if new_status in ("ABSENT", "LEAVE"):
        if req.punch_in is not None or req.punch_out is not None:
            raise HTTPException(status_code=422, detail="Punch times cannot be supplied when status is ABSENT or LEAVE")
        new_pin = None
        new_pout = None
    else:
        # Presence status
        if req.punch_in is not None:
            new_pin = ms_to_utc_dt(req.punch_in)
        else:
            new_pin = cur_pin

        if req.punch_out is not None:
            new_pout = ms_to_utc_dt(req.punch_out)
        else:
            new_pout = cur_pout

        # Rule 4: Presence status requires an existing or supplied punch_in
        if new_pin is None:
            raise HTTPException(status_code=422, detail=f"Presence status '{new_status}' requires a valid punch_in")

        # Rule 5: Final punch_in must belong to the record's attendance date under R1
        shift_start = emp.get("shift_start", "09:30")
        shift_end = emp.get("shift_end", "18:30")
        calc_date = get_attendance_date_for_punch(new_pin, shift_start, shift_end)
        if calc_date != att_date:
            raise HTTPException(status_code=422, detail=f"punch_in belongs to attendance date {calc_date}, not record date {att_date}")

        # Rule 6: If punch-out exists, strictly after punch-in and <= 24 hours later
        if new_pout is not None:
            if new_pout <= new_pin:
                raise HTTPException(status_code=422, detail="punch_out must be strictly greater than punch_in")
            if (new_pout - new_pin).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    # Rule 7 & 8: Recompute derived fields
    new_work_hours, new_late_mins, new_ot_mins, new_half_day = compute_derived_attendance(
        punch_in_utc=new_pin,
        punch_out_utc=new_pout,
        att_date_str=att_date,
        shift_start=shift_start,
        shift_end=shift_end,
        status=new_status
    )

    # Compare changes
    changes: Dict[str, Any] = {}

    if new_status != cur_status:
        changes["status"] = {"from": cur_status, "to": new_status}

    def dt_equal(d1: Optional[datetime], d2: Optional[datetime]) -> bool:
        if d1 is None and d2 is None:
            return True
        if d1 is None or d2 is None:
            return False
        return dt_to_ms(d1) == dt_to_ms(d2)

    if not dt_equal(cur_pin, new_pin):
        changes["punch_in"] = {"from": cur_pin, "to": new_pin}

    if not dt_equal(cur_pout, new_pout):
        changes["punch_out"] = {"from": cur_pout, "to": new_pout}

    # Derived fields comparison
    if cur_work_hours != new_work_hours:
        changes["work_hours"] = {"from": cur_work_hours, "to": new_work_hours}

    if cur_late_mins != new_late_mins:
        changes["late_minutes"] = {"from": cur_late_mins, "to": new_late_mins}

    if cur_ot_mins != new_ot_mins:
        changes["overtime_minutes"] = {"from": cur_ot_mins, "to": new_ot_mins}

    if cur_half_day != new_half_day:
        changes["half_day"] = {"from": cur_half_day, "to": new_half_day}

    # Rule 9: No changes -> 422
    if not changes:
        raise HTTPException(status_code=422, detail="Regularization request resulted in no changes to the record")

    # History entry
    now_utc = datetime.now(timezone.utc)
    truncated_now_ms = truncate_ms_to_sec_ms(int(now_utc.timestamp() * 1000))
    server_time_dt = datetime.fromtimestamp(truncated_now_ms / 1000.0, tz=timezone.utc)

    history_entry = {
        "at": server_time_dt,
        "by": req.regularized_by,
        "reason": req.reason,
        "changes": changes
    }

    # Optimistic concurrency check: match current history length and fields
    res = database.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": att_date,
            "$or": [
                {"history": {"$size": len(cur_history)}},
                {"history": {"$exists": False}} if not cur_history else {"history": {"$size": len(cur_history)}}
            ],
            "status": cur_status,
            "punch_in": cur_pin,
            "punch_out": cur_pout
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "work_hours": new_work_hours,
                "late_minutes": new_late_mins,
                "overtime_minutes": new_ot_mins,
                "half_day": new_half_day
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent modification detected on attendance record")

    return serialize_attendance(res)

# --- Endpoint G: GET /analytics/employees/{emp_code}/monthly ---
def parse_year_month(month_str: str) -> tuple[int, int, date, date]:
    m = re.match(r"^(\d{4})-(\d{2})$", month_str)
    if not m:
        raise HTTPException(status_code=422, detail=f"Invalid month format '{month_str}', expected YYYY-MM")
    year, month = int(m.group(1)), int(m.group(2))
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail=f"Invalid month: {month}")
    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    return year, month, first_day, last_day

def count_weekdays_in_range(start_d: date, end_d: date) -> int:
    if start_d > end_d:
        return 0
    cur = start_d
    count = 0
    while cur <= end_d:
        if cur.weekday() < 5:  # Monday=0 ... Friday=4
            count += 1
        cur += timedelta(days=1)
    return count

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str = Path(..., pattern=EMP_CODE_REGEX),
    month: str = Query(...)
):
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    year, mon_val, first_day, last_day = parse_year_month(month)
    joined_on = date.fromisoformat(emp["joined_on"])

    # Working days: Monday-Friday from later of joined_on and first_day to last_day
    # If employee has not joined by the end of month -> 0
    if joined_on > last_day:
        working_days = 0
    else:
        effective_start = max(first_day, joined_on)
        working_days = count_weekdays_in_range(effective_start, last_day)

    # MongoDB aggregation for attendance metrics in the month
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
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
                                    {"$lte": ["$dayOfWeek", 5]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    agg_res = list(database.attendance_logs.aggregate(pipeline))
    if agg_res:
        m = agg_res[0]
        present_days = float(m.get("present_days", 0.0))
        leave_days = int(m.get("leave_days", 0))
        late_count = int(m.get("late_count", 0))
        total_late_minutes = int(m.get("total_late_minutes", 0))
        total_overtime_minutes = int(m.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        raw_pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(round_half_up_decimal(raw_pct, 2))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }

# --- Endpoint H: GET /analytics/departments/summary ---
@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(...),
    department: Optional[str] = Query(None)
):
    database = get_db()
    year, mon_val, first_day, last_day = parse_year_month(month)
    last_day_str = last_day.isoformat()
    first_day_str = first_day.isoformat()

    match_stage = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$match": {
                "$or": [
                    {"logs": None},
                    {"logs.date": {"$gte": first_day_str, "$lte": last_day_str}}
                ]
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$dayOfWeek", 5]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0.0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "total_work_hours": 1,
                "work_hours_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": [
                            {"$floor": {"$add": [
                                {"$multiply": [{"$divide": ["$total_work_hours", "$work_hours_count"]}, 100]}, 0.5
                            ]}},
                            100
                        ]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": ASCENDING}}
    ]

    cursor = database.employees.aggregate(pipeline)
    items = []
    for row in cursor:
        avg_wh = row.get("avg_work_hours")

        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": float(row.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0))
        })

    return {
        "month": month,
        "items": items
    }

# --- Endpoint I: GET /analytics/leaderboard/late ---
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    database = get_db()
    year, mon_val, first_day, last_day = parse_year_month(month)
    first_day_str = first_day.isoformat()
    last_day_str = last_day.isoformat()

    match_att = {
        "date": {"$gte": first_day_str, "$lte": last_day_str},
        "late_minutes": {"$gt": 0}
    }

    pipeline = [
        {"$match": match_att},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    # Sort descending by total_late_minutes, then emp_code ascending
    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": ASCENDING, "emp_code": ASCENDING}}
    ])

    cursor = database.attendance_logs.aggregate(pipeline)
    items = []
    for doc in cursor:
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"])
        })

    return {
        "month": month,
        "items": items
    }

# --- Endpoint J: GET /analytics/departments/{department}/trend ---
def build_department_trend_pipeline(department: str, from_date: str, to_date: str, day_span: int) -> list[dict]:
    start_date_expr = {"$dateFromString": {"dateString": from_date}}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", from_date]},
                    {"$lte": ["$date", to_date]}
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}
            ],
            "as": "logs"
        }},
        {"$project": {"_id": 0, "joined_on": 1, "logs": 1}},
        {"$group": {"_id": None, "employees": {"$push": {"joined_on": "$joined_on", "logs": "$logs"}}}},
        {"$project": {
            "_id": 0, "employees": 1,
            "dates": {"$map": {
                "input": {"$range": [0, day_span]}, "as": "day_offset",
                "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {
                    "$dateAdd": {"startDate": start_date_expr, "unit": "day", "amount": "$$day_offset"}
                }}}
            }}
        }},
        {"$unwind": "$dates"},
        {"$set": {
            "date": "$dates",
            "headcount": {"$size": {"$filter": {
                "input": "$employees", "as": "employee",
                "cond": {"$lte": ["$$employee.joined_on", "$dates"]}
            }}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$dates"}}}, 5]},
            "present_count": {"$sum": {"$map": {
                "input": "$employees", "as": "employee",
                "in": {"$sum": {"$map": {
                    "input": {"$filter": {
                        "input": "$$employee.logs", "as": "log",
                        "cond": {"$and": [
                            {"$eq": ["$$log.date", "$dates"]},
                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                        ]}
                    }},
                    "as": "log",
                    "in": {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]}
                }}}
            }}},
            "late_count": {"$sum": {"$map": {
                "input": "$employees", "as": "employee",
                "in": {"$cond": [{"$gt": [{"$size": {"$filter": {
                    "input": "$$employee.logs", "as": "log",
                    "cond": {"$and": [
                        {"$eq": ["$$log.date", "$dates"]},
                        {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}
                    ]}
                }}, 0]}, 1, 0]}
            }}}
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": [
                {"$divide": ["$present_count", "$headcount"]}, 10000
            ]}, 0.5]}}, 10000]},
            None
        ]}}},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"_moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$_moving_avg_raw", None]}, None,
                {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_moving_avg_raw", 10000]}, 0.5]}}, 10000]}
            ]}
        }},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    try:
        d_from = parse_iso_date(from_date, "from")
        d_to = parse_iso_date(to_date, "to")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date format for 'from' or 'to'") from exc

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' cannot be before 'from'")

    day_span = (d_to - d_from).days + 1
    if day_span > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 calendar days")

    database = get_db()
    if database.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail=f"Department '{department}' not found or has no employees")

    pipeline = build_department_trend_pipeline(department, from_date, to_date, day_span)
    return {"department": department, "items": list(database.employees.aggregate(pipeline))}


# --- Endpoint K: GET /admin/explain/{endpoint} ---
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_param: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to")
):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Unsupported endpoint '{endpoint}'. Supported: {valid_endpoints}")

    database = get_db()

    try:
        if endpoint == "attendance_list":
            query: Dict[str, Any] = {}
            if emp_code is not None:
                query["emp_code"] = emp_code
            if date_from is not None or date_to is not None:
                date_q = {}
                if date_from is not None:
                    parse_iso_date(date_from, "date_from")
                    date_q["$gte"] = date_from
                if date_to is not None:
                    parse_iso_date(date_to, "date_to")
                    date_q["$lte"] = date_to
                if date_from and date_to and parse_iso_date(date_from, "date_from") > parse_iso_date(date_to, "date_to"):
                    raise HTTPException(status_code=422, detail="date_from > date_to")
                query["date"] = date_q
            if status_param is not None:
                if status_param not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
                    raise HTTPException(status_code=422, detail="Invalid status")
                query["status"] = status_param

            skip = (page - 1) * page_size
            explain_cmd = {
                "explain": {
                    "find": "attendance_logs",
                    "filter": query,
                    "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                    "skip": skip,
                    "limit": page_size
                },
                "verbosity": "executionStats"
            }
            explain_res = database.command(explain_cmd)
            return Response(content=json_util.dumps(explain_res), media_type="application/json")

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="'emp_code' and 'month' are required for employee_monthly")
            year, mon_val, first_day, last_day = parse_year_month(month)
            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                }
            ]
            explain_cmd = {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {}
                },
                "verbosity": "executionStats"
            }
            explain_res = database.command(explain_cmd)
            return Response(content=json_util.dumps(explain_res), media_type="application/json")

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="'month' is required for department_summary")
            year, mon_val, first_day, last_day = parse_year_month(month)
            match_stage = {"joined_on": {"$lte": last_day.isoformat()}}
            if department is not None:
                match_stage["department"] = department

            pipeline = [
                {"$match": match_stage},
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
                                            {"$gte": ["$date", first_day.isoformat()]},
                                            {"$lte": ["$date", last_day.isoformat()]}
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs"
                    }
                }
            ]
            explain_cmd = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {}
                },
                "verbosity": "executionStats"
            }
            explain_res = database.command(explain_cmd)
            return Response(content=json_util.dumps(explain_res), media_type="application/json")

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="'month' is required for late_leaderboard")
            year, mon_val, first_day, last_day = parse_year_month(month)
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()},
                        "late_minutes": {"$gt": 0}
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"}
                    }
                }
            ]
            explain_cmd = {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {}
                },
                "verbosity": "executionStats"
            }
            explain_res = database.command(explain_cmd)
            return Response(content=json_util.dumps(explain_res), media_type="application/json")

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(status_code=422, detail="'department', 'from', and 'to' are required for department_trend")
            d_f = parse_iso_date(from_date, "from")
            d_t = parse_iso_date(to_date, "to")
            if d_t < d_f:
                raise HTTPException(status_code=422, detail="'to' < 'from'")
            day_span = (d_t - d_f).days + 1
            if day_span > 92:
                raise HTTPException(status_code=422, detail="Date range exceeds 92 calendar days")
            pipeline = build_department_trend_pipeline(department, from_date, to_date, day_span)
            explain_cmd = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {}
                },
                "verbosity": "executionStats"
            }
            explain_res = database.command(explain_cmd)
            return Response(content=json_util.dumps(explain_res), media_type="application/json")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to explain query: {str(e)}")
