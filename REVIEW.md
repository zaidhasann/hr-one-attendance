# Implementation & Code Review (Part A)

## Starter Code Inspection & Defect Audit

No starter code was provided in the repository workspace; the project was created from scratch according to the **Contract v2.0** specifications. Below is the defect audit table documenting architectural and critical edge cases identified during specification review and implementation design, how they are detected, and how they were systematically addressed and fixed in `app/main.py`.

| # | Where | What is wrong / Risk | How to reproduce or detect it | How it was fixed |
|---|---|---|---|---|
| 1 | `POST /employees` & `POST /attendance/punch-in` | Race conditions on concurrent creations or simultaneous punch-ins leading to duplicate keys or corrupted state. | Send simultaneous requests for the same employee / date using concurrent threads. | Added unique database indexes (`emp_code` on `employees` and compound `(emp_code, date)` on `attendance_logs`). Handled `DuplicateKeyError` to return HTTP 409 cleanly. |
| 2 | Timezone & Shift Boundaries (R1, R2, R3) | Naive datetime calculations failing across UTC/IST boundaries and overnight shifts (e.g. 22:00 to 06:00). | Punch at 02:00 IST for a 22:00-06:00 shift. Naive parsers assign the calendar day instead of previous shift date. | Implemented dedicated `get_attendance_date_for_punch` and `compute_derived_attendance` that convert instants to IST (UTC+05:30) and correctly resolve shift start date and next-day scheduled end. |
| 3 | Numeric Rounding (R4, R5, R8) | Python's default `round()` uses round-to-even (banker's rounding), causing boundary errors on work hours and attendance percentages. | Pass durations like 8.755 or 4.495 hours. Banker's rounding yields 8.75 and 4.49 instead of half-up 8.76 and 4.50. | Implemented `round_half_up` using Python's standard `decimal.Decimal` with `ROUND_HALF_UP`. |
| 4 | `POST /attendance/punch-out` | Concurrent punch-out attempts both calculating overtime and work hours, overwriting punch-out. | Fire two concurrent punch-out requests for the same employee session. | Implemented atomic `find_one_and_update` with filter `punch_out: None`, ensuring exactly one request succeeds and the other receives HTTP 409. |
| 5 | `PATCH /attendance/{emp_code}/{date}` | Missing optimistic concurrency leading to lost audit history entries during concurrent edits; also applying no-op edits. | Run concurrent PATCH updates or submit identical data. | Checked `history` array length and matched current state inside atomic `find_one_and_update`. Compared old vs new fields and returned HTTP 422 for no-op changes. |
| 6 | Serialization & Leakage | MongoDB `_id` / synthetic `id` leakage and BSON datetime serialization failures in responses. | Query endpoints with raw cursors. BSON `ObjectId` and `datetime` cause FastAPI serialization errors. | Implemented explicit serializers (`serialize_employee`, `serialize_attendance`) removing `_id`, and converting BSON datetimes to integer epoch milliseconds. |
| 7 | Analytics (H, I, J) | Aggregations dropping employees without attendance records or processing aggregations inside Python loops. | Departments with zero-log employees or missing dates omitted from headcount and department summaries. | Preserved employees as primary documents, joined attendance using `$lookup` with proper date matching, and evaluated metrics in MongoDB. |

## Important Areas Reviewed & Found Correct

- **Timestamp Precision & Range Enforcement**: Integers between 100000000000 and 4102444800000 validated; seconds-based values, floats, strings, and booleans rejected with HTTP 422. Timestamps truncated to whole seconds.
- **Grace Period & Lateness (R2)**: Exact 10-minute grace period enforced (`diff_sec > 600`). Whole minutes elapsed from shift start calculated using floor semantics.
- **Half-Day Boundary (R5)**: Work hours strictly less than 4.50 marked as `half_day = True`, contributing 0.5 present days.
- **Explain Endpoint (`/admin/explain/{endpoint}`)**: Real `executionStats` queries generated without hard-coded responses, validating index scans against query shapes.
