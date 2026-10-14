# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` (lines 72-74) | Unconditionally returns `{"status": "ok"}` (200) without pinging MongoDB. | Stop MongoDB service; `GET /health` falsely returns 200 instead of 503. | Added `client.admin.command("ping")` in `try/except` raising `HTTPException(503)`. |
| 2 | Startup / Lifecycle | Zero database indexes created at startup. | Concurrency tests fail; queries perform slow `COLLSCAN`s; `/admin/explain` fails. | Created unique and compound indexes for both collections in lifespan and startup handler. |
| 3 | `create_employee` (lines 79-85) | Check-then-insert race condition on `emp_code`. | Concurrent `POST /employees` requests with identical `emp_code` both insert duplicates. | Enforced unique index on `emp_code` and handled `DuplicateKeyError` to return HTTP 409. |
| 4 | `create_employee` (lines 81-85) | `created_at` set as local naive datetime and returned as ISO string. | Response has string timestamp instead of epoch milliseconds; timezone offset discrepancy. | Set `created_at` to UTC truncated to whole seconds; serialized to integer epoch milliseconds. |
| 5 | `EmployeeIn` (lines 53-60) | Missing schema validations (`emp_code` format, email regex, string limits, shift inequality). | `POST /employees` with `shift_start == shift_end` or invalid `emp_code` returns 201 instead of 422. | Added Field validations (pattern `^EMP\d{4,6}$`, email regex, lengths) and model validator for shifts. |
| 6 | `list_employees` (line 93) | Pagination calculation `skip = page * page_size` skips page 1. | `GET /employees?page=1&page_size=20` returns records 21-40; records 1-20 are never seen. | Fixed skip calculation to `skip = (page - 1) * page_size`. |
| 7 | `list_employees` (line 94) | `total` counts entire collection with `count_documents({})` ignoring filter `q`. | `GET /employees?department=Engineering` returns 2 items but reports `total: 6`. | Passed query filter `count_documents(q)`. |
| 8 | `list_employees` (line 95) | Missing sort order. | Employees returned in non-deterministic natural database order. | Added `.sort("emp_code", ASCENDING)`. |
| 9 | `punch_in` (lines 101-113) | Missing employee existence check before indexing `emp["shift_start"]`. | Punch in with non-existent `emp_code` crashes with `TypeError` and HTTP 500 instead of 404. | Added existence check raising HTTP 404 when employee is not found. |
| 10 | `punch_in` (lines 104-118) | Check-then-insert race condition on `(emp_code, date)`. | Simultaneous punch-in requests for same date both succeed, creating duplicate logs. | Created unique compound index on `("emp_code", "date")` and caught `DuplicateKeyError` -> 409. |
| 11 | `punch_in` (lines 102-103) | Attendance date uses naive `ts.date()` without IST or overnight shift logic (R1). | Punch-in at 05:45 IST for shift 22:00-06:00 is assigned to today instead of previous day. | Implemented `get_attendance_date` with IST offset and overnight rollover. |
| 12 | `punch_in` (lines 119-120) | Injects `"id"` into response document (`doc["id"] = str(res.inserted_id)`). | Response body contains `"id"` field, violating OpenAPI `AttendanceRecord` schema. | Removed `"id"` field and serialized response according to OpenAPI schema. |
| 13 | `compute_late_minutes` (lines 33-37) | `int(diff/60)` before checking `> 10` drops late punches from 10:01 to 10:59; naive datetime replacement causes timezone bugs. | Punch in at 09:40:01 for 09:30 shift: `601/60=10.016 -> int 10`, `10>10` is False, returns 0 instead of 10. | Tested `diff_secs > 600` first, then computed `int(diff_secs // 60)` using IST-anchored shift start. |
| 14 | `compute_work_hours` (lines 40-41) | Python's `round()` uses round-half-to-even (banker's rounding) instead of half-up (R4). | 2.675 hours rounds to 2.67 instead of 2.68. | Used `Decimal` with `ROUND_HALF_UP` to round half-up to 2 decimals. |
| 15 | `compute_overtime` (lines 44-47) | Missing 30-minute minimum threshold (R3) and overnight shift next-day boundary. | Working 5 minutes late gives 5 min overtime instead of 0; overnight shifts calculate ~24h overtime. | Set threshold `minutes >= 30` (otherwise 0) and advanced overnight shift end by 1 day. |
| 16 | `list_attendance` (lines 144-147) | Loads entire collection into Python memory with `list(...)` and sorts in Python. | High memory consumption and slow response times on 100k dataset; missing secondary sort by `emp_code`. | Pushed sorting (`[("date", -1), ("emp_code", 1)]`), filter, and pagination into MongoDB; validated `date_from <= date_to`. |

### Not a Defect
- `IST = timezone(timedelta(hours=5, minutes=30))` is a static offset without daylight saving time. India does not observe Daylight Saving Time (DST), so a fixed UTC+05:30 offset is standard and correct.
