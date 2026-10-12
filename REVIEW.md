# REVIEW.md

## Defects Found in the Starter `app/main.py`

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `department_trend()` — MongoDB `$densify` date bounds | The upper bound was exclusive, so the requested final date could be missing from the generated daily trend. | Request a trend from `2026-10-05` to `2026-10-11` and verify that the response contains all seven dates, including `2026-10-11`. | Extended the upper bound to midnight on the day after `to`, so the requested end date is included. |

## Areas Reviewed and Confirmed Not to Be Defects

- **Unique attendance index:** The unique compound index on `(emp_code, date)` is intentional. It prevents duplicate attendance records for the same employee and attendance date.
- **Zero attendance on weekends:** Monthly `present_days` excludes Saturday and Sunday, as required by the business rules. Weekend records can still contribute to late and overtime totals.
- **Null attendance rate on non-working days:** A `null` rate on weekends is intentional because the rate is defined for working days.
- **No attendance record versus `ABSENT`:** A missing attendance record is not automatically the same as an explicit `ABSENT` record. These represent different states.
- **Stored derived values in analytics:** Analytics should use stored fields such as `late_minutes`, `overtime_minutes`, and `work_hours` rather than silently recalculating them from punch timestamps.
- **Manual-correction history:** Punch-in and punch-out should not create regularization history entries. The audit history is for manual corrections.

## Verification Notes

The API was tested with valid requests, invalid date ranges, unknown departments, attendance regularization, analytics endpoints, and MongoDB explain plans. The tested endpoints returned expected results after the fixes.

This document records the defect confirmed during the trend-boundary investigation. Add any other defects only after confirming the original starter code, reproducing the issue, and verifying the corresponding fix.
