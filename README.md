# Employee Attendance & Analytics API

FastAPI + MongoDB backend for employee punch-in / punch-out, manual corrections and attendance analytics.
All application code is in `app/main.py`.

## Requirements
- Python 3.11+
- MongoDB 6.0 or newer (local Docker or Atlas free tier)

## Run

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export MONGO_URI="mongodb+srv://<user>:<password>@<cluster>.mongodb.net/"
export MONGO_DB="attendance_db"      # any database name

uvicorn app.main:app --port 8000
```

- `MONGO_URI` and `MONGO_DB` are read from environment variables. A local `.env` file is also loaded,
  but real environment variables always win. Nothing is hard-coded and `.env` is not committed.
- All indexes are created at startup and the step is idempotent. No manual setup is needed.
- `GET /health` returns 200 only when MongoDB answers a ping, otherwise 503.
- Interactive API docs: `http://127.0.0.1:8000/docs`

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Readiness probe |
| POST | `/employees` | Create an employee (409 on duplicate `emp_code`) |
| GET | `/employees` | List, filter by department, paginated |
| POST | `/attendance/punch-in` | Create the day's record (race-safe) |
| POST | `/attendance/punch-out` | Close the record; computes hours, overtime, half day |
| GET | `/attendance` | List, filter, paginated, index-backed |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize a record; recomputes derived fields, appends to `history` |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Department summary for a month |
| GET | `/analytics/leaderboard/late` | Late-comers ranking with ties (1, 2, 2, 4) |
| GET | `/analytics/departments/{department}/trend` | Daily series with 7-day moving average |
| GET | `/admin/explain/{endpoint}` | MongoDB `executionStats` explain for an endpoint's main query |

## Design notes
- **Time:** instants are stored as UTC datetimes and exchanged as epoch milliseconds; calendar dates and shift
  times are IST strings. The client is `tz_aware`, so PyMongo never returns naive datetimes.
- **Rules R1-R5** live in small pure helper functions (attendance date, late minutes, overtime, work hours with
  half-up rounding, half day).
- **Concurrency:** duplicate employees and double punch-ins are stopped by unique indexes; a second punch-out is
  stopped by a conditional update (`punch_out: null`); corrections use optimistic concurrency, so a concurrent
  edit gets 409 and no history entry is lost.
- **Analytics** are MongoDB aggregation pipelines. The same builder functions feed both the real endpoint and
  `/admin/explain`, so the explained plan is the plan that actually runs. Queries carry an index hint.
- **Trend gap filling** is done inside the pipeline with `$range` + `$unwind` (one row per calendar day, no Python
  loop) and the moving average with `$setWindowFields`, instead of `$densify`.
- Stored derived values (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) are read as stored by the
  analytics, never recomputed from punch times.

## Documents
- `REVIEW.md`: defects found in the starter code and how each was fixed.
- `DECISIONS.md`: indexes, race handling, tie handling, headcount, scaling.

## Status
All endpoints in the contract are implemented. Nothing is intentionally missing.