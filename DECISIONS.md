# DECISIONS.md

## 1. Indexes

I created indexes to make employee lookups, department filtering, attendance date searches, and late-arrival queries faster. The unique employee-code index prevents duplicate employees. The unique `(emp_code, date)` attendance index prevents duplicate daily attendance records. Other indexes support department summaries, date-sorted attendance lists, status filtering, and late-arrival ranking. I considered adding more indexes for every possible filter combination but rejected that because extra indexes consume storage and slow down writes.

## 2. Punch-in race

If two identical punch-in requests arrive together, both may check that the employee exists and calculate the same attendance date. Both then try to insert a record. The unique `(emp_code, date)` index allows only one insert to succeed. The other raises `DuplicateKeyError`, which the API converts into a `409 Conflict` response. This makes MongoDB enforce the guarantee even when requests run concurrently.

## 3. Ties

The leaderboard sorts by total late minutes and employee code. It uses `$rank`, so employees tied on late minutes receive the same rank. The limit is applied to the rank, meaning tied employees at the cutoff can all be returned.

## 4. Headcount

The department summary starts from employees and looks up their attendance records. Therefore, employees with no logs are still counted in headcount.

## 5. Scaling to 100x data

I would review query plans and load-test the aggregation pipelines, then optimize the slowest queries based on measured results.