# REVIEW.md

Defects found in the starter `app/main.py`, all fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds the shift start from the punch-in's own (UTC/naive) clock, not IST. A 10:05 IST punch is 04:35 UTC, so it is compared with 09:30 UTC and "late" is never detected. | Punch in 10:05 IST on a 09:30 shift: expected 35, got 0. | Shift start is built on the attendance date in IST (`shift_start_dt`); all maths is on aware datetimes. |
| 2 | `compute_late_minutes` | Grace test is on whole minutes (`minutes > 10`), so 09:40:01 (10 min 1 s) gives 0 instead of 10; no truncation to seconds. | 09:40:00 must be 0 and 09:40:01 must be 10; 10:15:59 must be 45. | Work in whole seconds: late only if `secs > 600`, then `secs // 60` (R2). |
| 3 | `compute_late_minutes` | Ignores overnight shifts: a 02:00 punch on a 22:00 shift is measured against 22:00 of the same day. | Overnight employee punching after midnight. | Attendance date (R1) is computed first, then the shift start is taken on that date. |
| 4 | `compute_work_hours` | Python `round()` is banker's rounding, R4 wants half-up; float division adds error. | 18 s -> 0.005 h must give 0.01. | `Decimal` seconds/3600 with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). Uses the *punch-in's* date for shift end, wrong for overnight shifts. Seconds are not floored consistently. | Punch-out 5 min after shift end returned 5, should be 0. Overnight shift returned ~0. | Minimum 30 applied; shift end is on the next day for overnight shifts. |
| 6 | `create_employee` | find-then-insert duplicate check is a race: two parallel creates can both succeed. | Parallel POSTs with the same `emp_code`. | Unique index on `emp_code`, `DuplicateKeyError` -> 409. |
| 7 | `create_employee` / `EmployeeIn` | No validation (code format, e-mail, `HH:MM`, real dates, `shift_start != shift_end`, lengths). Bad input stores garbage and returns 201. | POST `joined_on: "2026-13-45"`. | Pydantic patterns and validators -> 422. |
| 8 | `create_employee` | `created_at = datetime.now()` is naive local time, and was returned as a datetime, not epoch ms. | Response `created_at` is a string. | Aware UTC, returned as epoch ms. |
| 9 | `list_employees` | `skip = page * page_size` skips a whole page; page 1 starts at row 21. | `GET /employees` returns items 21-40 first. | `(page - 1) * page_size`. |
| 10 | `list_employees` | `total = count_documents({})` ignores the department filter (R10). | `?department=X` shows total of all employees. | Count with the same filter. |
| 11 | `list_employees` | No sort, so pages are unstable; no bounds on `page` / `page_size`. | `page_size=100000`, `page=-1`. | Sort by `emp_code`, `page >= 1`, `1 <= page_size <= 100`. |
| 12 | `punch_in` | Unknown employee -> `emp` is `None` -> `TypeError` -> 500. | Punch in for `NOPE`. | 404. |
| 13 | `punch_in` | `fromtimestamp(ms/1000)` uses server-local time; `if body.punched_at` treats `0` as "missing"; seconds/floats/strings are accepted. | Send `punched_at` in seconds. | `StrictInt` between 1e11 and 4.1e12 -> 422; explicit `is not None`. |
| 14 | `punch_in` | `date` is the local date of the timestamp, not the IST attendance day (R1); no overnight handling; no truncation to seconds. | Punch at 23:50 IST from a UTC server lands on the next day. | `attendance_date()` (R1), `ms_to_dt()` truncates to seconds. |
| 15 | `punch_in` | Check-then-insert race: two simultaneous requests can both pass `find_one` and create two records. | Fire 20 parallel punch-ins. | Unique index (`emp_code`, `date`); loser gets `DuplicateKeyError` -> 409. |
| 16 | `punch_in` | `status` is free text (`ABSENT`, `"foo"` accepted); response leaks `id` (the `_id`) and returns a raw datetime. | POST `status: "foo"`. | `Literal["PRESENT","WFH","ON_DUTY"]`; one serializer, no `_id`, epoch ms. |
| 17 | `list_attendance` | Loads every matching document and sorts/paginates in Python; no index. Sort is by `date` only, no `emp_code` tie-break. | 100k records: seconds and big memory. | Index-backed `find` with `sort`, `skip`, `limit` in MongoDB (sort `date` desc, `emp_code` asc). |
| 18 | `list_attendance` | Returns `id` and raw datetimes; `date_from > date_to`, bad dates, bad `status`, `page=0`, huge `page_size` are accepted. | Any of those inputs. | Validation -> 422; serializer. |
| 19 | `/health` | Always returns 200, even if MongoDB is down. | Stop MongoDB, call `/health`. | `ping`, 503 on failure. |
| 20 | Connection | Client is not `tz_aware`, so all datetimes come back naive and silently turn into local time. No server selection timeout. No indexes at all. | Any time conversion. | `tz_aware=True`, 5 s timeout, `ensure_indexes()` at startup. |

## Looked at and decided it was fine
- `load_dotenv()`: its default does **not** override real environment variables, so it already meets the contract; I only made it explicit (`override=False`).
- `doc.pop("_id")` after `insert_one`: correct idea (PyMongo adds `_id` to the dict), but I replaced it with a serializer that builds the response from named fields.
- Storing `history: []` at punch-in: correct, matches DATA_MODEL.md.
- Sync `def` endpoints with PyMongo: fine, FastAPI runs them in a thread pool and `MongoClient` is thread-safe.

## A bug I introduced myself and found by testing against a real MongoDB
This one was not in the starter. It was in my first version of the new analytics code, and I only found it by running everything against Atlas.

| Where | What was wrong | How I noticed | How I fixed it |
|---|---|---|---|
| Index hints passed to `aggregate()` (monthly, department summary, leaderboard, trend, explain) | I passed each hint as a list of tuples, e.g. `[("emp_code", 1), ("date", 1)]`. `find().hint()` accepts that, but `aggregate(hint=...)` does not convert it, so MongoDB answered `Hint must be a string or an object` and the endpoint returned 500. | Every analytics endpoint returned 500; the server log showed the `OperationFailure`. An in-memory fake database had hidden it because it ignores hints. | The builders now return hints as dicts (`{"emp_code": 1, "date": 1}`), used by the endpoints and by `/admin/explain`. |