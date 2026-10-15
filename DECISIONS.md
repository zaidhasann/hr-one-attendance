# Architecture & Design Decisions

### 1. Indexes
Created unique index on `employees.emp_code` and compound unique index on `attendance_logs(emp_code, date)`. For query performance, indexed `employees(department, emp_code)`, `employees(joined_on)`, and `attendance_logs(date, emp_code)` for sorted filtering. An index on `(emp_code, punch_in)` powers punch-out lookups. Considered indexing every status field independently, but rejected it as compound date/code indexes provide superior selectivity without write overhead.

### 2. Punch-in race
When two identical punch-in requests arrive concurrently, both compute identical attendance dates and attempt atomic insertion into `attendance_logs`. MongoDB's unique compound index `(emp_code, date)` guarantees only one insert succeeds. The winning request inserts the document and returns HTTP 201. The losing request triggers a `DuplicateKeyError`, intercepted by the handler to return HTTP 409 Conflict.

### 3. Leaderboard ties
Standard competition ranking (`$rank`) assigns ranks based on the count of strictly greater values plus one (e.g. 1, 2, 2, 4). The `limit` filter is evaluated against this calculated `rank <= limit`. When ties occur at the cutoff boundary, all employees sharing that rank qualify, returning more items than `limit`.

### 4. Department headcount
The department summary originates from the `employees` collection filtered by `joined_on <= month_end`. Attendance records are joined via `$lookup` and unwound with `preserveNullAndEmptyArrays: true`. Employees with no logs are grouped into the department's headcount, while future joiners are excluded upfront in `$match`.

### 5. 100x scale
At 100x scale, time-based sharding on `attendance_logs` using hashed `(emp_code, date)` or pre-aggregating daily summary rollups would eliminate full-month join latency and distribute high-volume biometric punch writes evenly across shards.


### 6. Startup readiness and date validation

Required indexes are part of the API's correctness contract. Startup fails if index creation fails, rather than silently serving requests without uniqueness guarantees. `/health` is ready only after index initialization and a successful database ping. Date-only API values are accepted only in canonical `YYYY-MM-DD` form.
