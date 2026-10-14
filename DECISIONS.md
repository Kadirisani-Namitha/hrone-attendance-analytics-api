# DECISIONS.md

1. **Indexes.**
   - `employees: {emp_code: 1}` (unique) guarantees uniqueness of business codes.
   - `employees: {department: 1, emp_code: 1}` optimizes department filtering and sorting.
   - `attendance_logs: {emp_code: 1, date: 1}` (unique) enforces the one-record-per-day rule and speeds natural key queries.
   - `attendance_logs: {date: -1, emp_code: 1}` powers fast paginated attendance listing.
   - `attendance_logs: {emp_code: 1, punch_in: -1}` enables instant punch-out lookups.
   - *Rejected:* An index on `status` alone, due to low cardinality and poor selectivity.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` guarantees only one insertion commits (HTTP 201). The concurrent request raises a `DuplicateKeyError`, which our handler catches and returns as HTTP 409 Conflict.

3. **Ties.**
   We apply standard competition ranking (`$rank` in `$setWindowFields`) where tied employees share the same rank and subsequent ranks are skipped (e.g., 1, 2, 2, 4). Filtering `rank <= limit` occurs after ranking, so all employees tied at the cutoff are returned, sorted secondarily by `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline starts from `employees` filtered by `joined_on <= end_of_month`. We `$lookup` attendance logs and `$unwind` with `preserveNullAndEmptyArrays: True`, ensuring employees with zero logs are preserved and counted in their department's headcount.

5. **One thing you would change for 100x data.**
   Pre-aggregate monthly attendance and department statistics asynchronously into dedicated summary collections via MongoDB change streams, avoiding expensive on-the-fly cross-collection `$lookup` joins on massive datasets.
