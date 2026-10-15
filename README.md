# HROne Employee Attendance & Analytics API

A REST API for employee management, attendance tracking, attendance regularization, and analytics. Built with Python, FastAPI, Pydantic, PyMongo, and MongoDB.

## Features

- Employee creation and paginated employee listing.
- Punch-in and punch-out with UTC epoch-millisecond timestamps and IST shift calculations.
- Support for overnight shifts, late arrival, overtime, work hours, and half-day calculations.
- Attendance filtering and pagination.
- Attendance regularization with an audit history and optimistic concurrency checks.
- Monthly employee analytics, department summaries, late-arrival leaderboard, and department trends.
- MongoDB indexes created during application startup. Startup fails if required indexes cannot be created.
- MongoDB execution-plan diagnostics for supported endpoints.
- Unit tests and a GitHub Actions workflow.

## Technology Stack

- **Python:** 3.11+
- **FastAPI:** >= 0.110
- **Uvicorn:** >= 0.29
- **Pydantic:** >= 2.6
- **PyMongo:** >= 4.7
- **python-dotenv:** >= 1.0
- **pytest:** >= 8.0
- **MongoDB:** 6.0+ recommended

## Configuration

The app reads these environment variables:

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGO_DB` | `hrone_db` | Database name |

For local development, create a `.env` file in the repository root:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=hrone_db
```

For MongoDB Atlas, use the connection string from your Atlas project. Keep credentials private: do not commit `.env` files or paste database passwords into source code.

## Installation

Create and activate a virtual environment, then install dependencies.

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux/macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the API

From the repository root:

```bash
uvicorn app.main:app --reload --port 8000
```

Open the interactive API documentation at [http://localhost:8000/docs](http://localhost:8000/docs). The alternative OpenAPI UI is available at [http://localhost:8000/redoc](http://localhost:8000/redoc).

### Health and readiness

```bash
curl http://localhost:8000/health
```

A successful response is `200 OK` with:

```json
{"status":"ok"}
```

The health endpoint returns `503 Service Unavailable` if the database cannot be reached or required indexes are not ready. Required indexes are created at application startup; if index creation fails, startup fails rather than silently serving without those guarantees.

## API Endpoints

All routes are defined in `app/main.py`. Unless otherwise stated, request and response bodies use JSON.

### Health

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Checks database connectivity and application readiness |

### Employees

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/employees` | Create an employee; returns `201 Created` |
| GET | `/employees` | List employees with pagination and optional department filtering |

Example employee creation:

```bash
curl -X POST http://localhost:8000/employees \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP1001",
    "name": "Aarav Sharma",
    "email": "aarav@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-15"
  }'
```

Employee codes must match `EMP` followed by 4–6 digits. Shift times use 24-hour `HH:MM` format, and `joined_on` must use the exact `YYYY-MM-DD` format. Shift times cannot be identical.

### Attendance

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/attendance/punch-in` | Record an employee punch-in; returns `201 Created` |
| POST | `/attendance/punch-out` | Record punch-out and calculate derived attendance fields |
| GET | `/attendance` | Filter and paginate attendance records |
| PATCH | `/attendance/{emp_code}/{att_date}` | Regularize a record and append an audit-history entry |

Example punch-in:

```bash
curl -X POST http://localhost:8000/attendance/punch-in \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP1001",
    "punched_at": 1799991000000,
    "status": "PRESENT"
  }'
```

- `punched_at` is optional; when supplied, it must be an integer Unix epoch timestamp in **milliseconds**, not seconds. The API truncates timestamps to whole seconds.
- Punch-in status can be `PRESENT`, `WFH`, or `ON_DUTY`.
- If `punched_at` is omitted, the server uses its current time.
- Attendance dates are determined using IST and the employee's shift, including overnight shifts.
- A unique database index on `(emp_code, date)` prevents duplicate daily attendance records.
- Derived fields include `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day`. A regularization requires a reason and the identity of the person making the change.

### Analytics

| Method | Endpoint | Required query parameters | Purpose |
|---|---|---|---|
| GET | `/analytics/employees/{emp_code}/monthly` | `month=YYYY-MM` | Monthly working days, present days, leaves, lateness, overtime, and attendance percentage |
| GET | `/analytics/departments/summary` | `month=YYYY-MM`; optional `department` | Department headcount and attendance metrics |
| GET | `/analytics/leaderboard/late` | `month=YYYY-MM`; optional `limit` (default 10, range 1–50) and `department` | Late-arrival leaderboard with competition ranking for ties |
| GET | `/analytics/departments/{department}/trend` | `from=YYYY-MM-DD` and `to=YYYY-MM-DD` | Daily headcount, attendance rate, lateness, and seven-day moving average |

The department trend accepts a date range of up to 92 calendar days. Date-only parameters use the exact `YYYY-MM-DD` format. Trend metrics and the seven-day moving average are calculated in a MongoDB aggregation; the date range is applied within the attendance-log lookup.

## Query-plan diagnostics

`GET /admin/explain/{endpoint}` returns MongoDB explain output with `executionStats` for supported query shapes.

Supported endpoint names:

- `attendance_list` — accepts the attendance filters, including optional `emp_code`, `date_from`, `date_to`, `status`, `page`, and `page_size`.
- `employee_monthly` — requires `emp_code` and `month=YYYY-MM`.
- `department_summary` — requires `month=YYYY-MM`; optionally accepts `department`.
- `late_leaderboard` — requires `month=YYYY-MM`; optionally accepts `department` and `limit`.
- `department_trend` — requires `department`, `from=YYYY-MM-DD`, and `to=YYYY-MM-DD`.

Example:

```bash
curl "http://localhost:8000/admin/explain/department_trend?department=Engineering&from=2026-10-01&to=2026-10-07"
```

The explain routes use the same pipeline builders as the corresponding production analytics endpoints, so their execution plans reflect those aggregation stages. Use the returned execution statistics to inspect documents examined, documents returned, and index use; an index scan is not guaranteed for every query or dataset.

## Database indexes

The application creates the following indexes during startup:

- Unique `employees.emp_code`
- `employees(department, emp_code)`
- `employees(department, joined_on)`
- `employees(joined_on)`
- Unique `attendance_logs(emp_code, date)`
- `attendance_logs(emp_code, punch_in DESC)`
- `attendance_logs(date DESC, emp_code)`
- `attendance_logs(emp_code, date, status)`

Existing data must satisfy the unique constraints before MongoDB can create the unique indexes. If duplicate records already exist, resolve them before starting the application.

## Run tests

The unit tests do not require a running MongoDB instance:

```bash
python -m pytest -q
```

GitHub Actions runs the same test command on pushes to `main` and on pull requests.

## Project layout

```text
.
├── app/
│   ├── __init__.py
│   └── main.py
├── tests/
│   └── test_helpers.py
├── .github/
│   └── workflows/
│       └── tests.yml
├── DECISIONS.md
├── REVIEW.md
├── requirements.txt
└── README.md
```

## Related documentation

- [Architecture and design decisions](DECISIONS.md)
- [Implementation and code review](REVIEW.md)
