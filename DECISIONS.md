# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`, `(department, emp_code)` (filtered, sorted list; trend and summary start here), `joined_on` (headcount without a department). `attendance_logs`: unique `(emp_code, date)` (one record per day, and per-employee queries), `(date desc, emp_code)` (default list, monthly leaderboard), `(status, date desc, emp_code)` (status filter), `(emp_code, punch_in desc)` (finding the record to punch out). Rejected: copying `department` into every log. It speeds up joins but can drift when an employee moves.

2. **Punch-in race.** Both pass the employee lookup and both try `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no "check first" step to race.

3. **Ties.** I rank with `$rank` (1, 2, 2, 4) and only then keep `rank <= limit`. Everyone tied at the cutoff rank is returned, so the response can have more than `limit` rows.

4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end), then `$lookup`s logs and `$unwind`s with `preserveNullAndEmptyArrays`, so an employee with no logs still produces a row and is counted.

5. **100x data.** I would stop scanning raw logs for analytics and keep a small daily roll-up per employee and department, updated on punch and correction.
