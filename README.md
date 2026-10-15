# HROne Employee Attendance & Analytics API (Contract v2.0)

A high-performance, production-grade REST API for employee attendance tracking and analytics built with Python 3.11+, FastAPI, and MongoDB 6.0+.

## Technology Stack & Requirements

- **Python**: 3.11+
- **FastAPI**: >= 0.110
- **Uvicorn**: >= 0.29
- **MongoDB**: 6.0+
- **PyMongo**: >= 4.7
- **Pydantic**: >= 2.6
- **python-dotenv**: >= 1.0

## Required Environment Variables

The application reads configuration from environment variables (a local `.env` is supported for development):

- `MONGO_URI`: MongoDB connection string (e.g., `mongodb://localhost:27017` or MongoDB Atlas SRV URI). Default: `mongodb://localhost:27017`
- `MONGO_DB`: Target database name. Default: `hrone_db`

## Installation

```bash
python -m venv .venv
# Activate virtual environment:
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

## Running the Application

Start the API server from the repository root:

```bash
uvicorn app.main:app --port 8000
```

To verify database readiness and application health:

```bash
curl http://localhost:8000/health
```

Returns `{"status": "ok"}` with HTTP 200 only after MongoDB connectivity and required indexes are ready. Startup fails if required index creation fails; the endpoint returns HTTP 503 when the database is unavailable or initialization is incomplete.

## Implemented API Endpoints

1. **System & Health**:
   - `GET /health` — Verifies MongoDB connectivity via admin ping.
2. **Employee Management**:
   - `POST /employees` — Creates an employee with database-enforced code uniqueness and UTC instant creation.
   - `GET /employees` — Paginated, department-filtered list sorted by `emp_code`.
3. **Attendance & Biometrics**:
   - `POST /attendance/punch-in` — Atomic punch-in supporting IST shifts and overnight shifts, enforcing single punch per day via unique compound index `(emp_code, date)`.
   - `POST /attendance/punch-out` — Atomic conditional punch-out with duration, overtime, half-day, and work hour derivations.
   - `GET /attendance` — Multi-parameter filtered and paginated attendance records with descending date sorting.
   - `PATCH /attendance/{emp_code}/{date}` — Optimistically-controlled regularization with comprehensive audit logging and derived field recalculation.
4. **Analytics Pipelines**:
   - `GET /analytics/employees/{emp_code}/monthly` — Monthly summary calculating working days, presence days, leaves, lateness, overtime, and attendance percentage.
   - `GET /analytics/departments/summary` — Department-level aggregation joining employees and attendance logs, preserving zero-log employees in headcount.
   - `GET /analytics/leaderboard/late` — Standard competition ranking (`$rank`) of employees by late minutes with tie handling.
   - `GET /analytics/departments/{department}/trend` — Daily department trend over calendar windows with 7-day moving averages.
5. **Administration & Diagnostics**:
   - `GET /admin/explain/{endpoint}` — Real MongoDB query execution statistics (`executionStats`) using the same analytics pipelines as production, so index use and execution work can be inspected.

## Running Tests

Unit tests can be executed locally using `pytest` (they do not require a running MongoDB instance):

```bash
python -m pytest
```
