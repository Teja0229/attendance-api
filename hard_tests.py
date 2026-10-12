"""
Hard-case test for the analytics endpoints.  NOT part of the submission (do not commit it).

What it does
  1. Fills a TEST database with awkward data (ties, weekend records, a mid-month joiner, an employee with no logs,
     an overnight shift, a missing weekday, a ghost employee, an open record, a half day, a future joiner).
  2. Computes the expected answers with plain Python loops (independent of the MongoDB pipelines).
  3. Calls your running API and compares.  Prints PASS / FAIL for every check.

How to run (PowerShell, from the project folder, venv active)
  $env:MONGO_DB = "attendance_test"          # a throw-away database, NOT your real one
  uvicorn app.main:app --port 8000           # terminal 1 (started AFTER setting MONGO_DB)
  python hard_tests.py                       # terminal 2 (same $env:MONGO_DB must be set there too)

WARNING: it empties the employees and attendance_logs collections of the database named in MONGO_DB.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
DB_NAME = os.getenv("MONGO_DB", "")
if not DB_NAME.endswith("_test"):
    sys.exit('Refusing to run: set a throw-away database first, e.g.  $env:MONGO_DB = "attendance_test"\n'
             "(its name must end with _test, and the server must be started with the same value).")

IST = timezone(timedelta(hours=5, minutes=30))
PRES = {"PRESENT", "WFH", "ON_DUTY"}
MONTH = "2026-07"


# --------------------------------------------------------------------------- data
def at(day: str, h: int, m: int, s: int = 0, plus_days: int = 0) -> datetime:
    d = date.fromisoformat(day) + timedelta(days=plus_days)
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST).astimezone(timezone.utc)


def rec(code, day, status="PRESENT", pin=(9, 30), pout=(18, 30), wh=9.0, late=0, ot=0, half=False, out_next=False):
    present = status in PRES
    return {
        "emp_code": code, "date": day, "status": status,
        "punch_in": at(day, *pin) if present else None,
        "punch_out": at(day, *pout, plus_days=1 if out_next else 0) if present and pout else None,
        "work_hours": wh if present and pout else None,
        "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": [],
    }


EMPLOYEES = [
    ("EMP1001", "Asha Eng", "Eng", "09:30", "18:30", "2026-01-05"),
    ("EMP1002", "Ravi Eng", "Eng", "09:30", "18:30", "2026-01-05"),
    ("EMP1003", "Mid Joiner", "Eng", "09:30", "18:30", "2026-07-15"),   # joins mid-month
    ("EMP1004", "No Logs", "Eng", "09:30", "18:30", "2026-01-05"),      # zero logs, must still be counted
    ("EMP2001", "Night Nina", "Ops", "22:00", "06:00", "2026-01-05"),   # overnight shift
    ("EMP2002", "Day Dan", "Ops", "09:30", "18:30", "2026-03-01"),
    ("EMP3001", "Future Fay", "Future", "09:30", "18:30", "2026-08-01"),  # joins after July: department omitted
]

LOGS = [
    # EMP1001: late 30 + 11 + 20 (the 20 is on a Saturday) = 61
    rec("EMP1001", "2026-07-01", pin=(9, 28), pout=(18, 35), wh=9.12),
    rec("EMP1001", "2026-07-02", pin=(10, 0), pout=(19, 10), wh=9.17, late=30, ot=40),
    rec("EMP1001", "2026-07-03", status="WFH", pin=(9, 30), pout=(13, 30), wh=4.0, half=True),
    rec("EMP1001", "2026-07-06", pin=(9, 41), pout=(18, 30), wh=8.82, late=11),
    rec("EMP1001", "2026-07-08", status="LEAVE"),
    rec("EMP1001", "2026-07-09", status="ABSENT"),
    # 2026-07-10 (Friday): no record at all
    rec("EMP1001", "2026-07-11", pin=(10, 0), pout=(20, 0), wh=10.0, late=20, ot=90),   # Saturday
    # EMP1002: late 25 + 15 = 40 (tie)
    rec("EMP1002", "2026-07-01", pin=(9, 55), pout=(18, 30), wh=8.58, late=25),
    rec("EMP1002", "2026-07-02", status="ON_DUTY", wh=9.0),
    rec("EMP1002", "2026-07-03", pin=(9, 45), pout=(18, 30), wh=8.75, late=15),
    rec("EMP1002", "2026-07-06", wh=9.0),
    rec("EMP1002", "2026-07-12", pin=(10, 0), pout=(12, 0), wh=2.0, half=True),          # Sunday half day
    # EMP1003 joins 2026-07-15
    rec("EMP1003", "2026-07-15", wh=9.0),
    rec("EMP1003", "2026-07-16", pin=(9, 42), pout=(18, 30), wh=8.8, late=12),
    rec("EMP1003", "2026-07-17", status="LEAVE"),
    # EMP2001: overnight 22:00-06:00; punch-out is on the next calendar day
    rec("EMP2001", "2026-07-06", pin=(21, 55), pout=(6, 40), wh=8.75, ot=40, out_next=True),
    rec("EMP2001", "2026-07-07", pin=(22, 10), pout=(6, 0), wh=7.83, late=10, out_next=True),
    rec("EMP2001", "2026-07-11", pin=(22, 0), pout=(6, 0), wh=8.0, out_next=True),         # Saturday
    # EMP2002: late 40 (tie), plus an open record (no punch-out)
    rec("EMP2002", "2026-07-01", pin=(10, 10), pout=(18, 30), wh=8.33, late=40),
    rec("EMP2002", "2026-07-02", pout=None),
    rec("EMP2002", "2026-07-03", status="ON_DUTY", wh=9.0),
    # ghost: a log whose employee does not exist, must be ignored everywhere
    rec("EMP9999", "2026-07-01", pin=(11, 0), pout=(18, 30), wh=7.5, late=100),
]
# a regular pattern for the rest of July (weekdays), with one gap every 5th day
_d = date(2026, 7, 13)
_i = 0
while _d <= date(2026, 7, 31):
    if _d.weekday() < 5:
        _i += 1
        if _i % 5:
            for code in ("EMP1001", "EMP1002", "EMP2002"):
                LOGS.append(rec(code, _d.isoformat(), wh=9.0))
    _d += timedelta(days=1)

EMP = {c: dict(emp_code=c, name=n, department=dp, shift_start=s, shift_end=e, joined_on=j,
               email=f"{c.lower()}@example.com", created_at=at("2026-01-05", 10, 0))
       for c, n, dp, s, e, j in EMPLOYEES}


# --------------------------------------------------------------------------- reference answers
def rhu(value, places):
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def weekday(day):
    return date.fromisoformat(day).weekday() < 5


def month_range(month):
    y, m = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{monthrange(y, m)[1]:02d}"


def count_wd(a, b):
    d, n = date.fromisoformat(a), 0
    while d <= date.fromisoformat(b):
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def weight(l):
    return 0.5 if l.get("half_day") else 1


def ref_monthly(code, month):
    first, last = month_range(month)
    rs = [l for l in LOGS if l["emp_code"] == code and first <= l["date"] <= last]
    start = max(first, EMP[code]["joined_on"])
    wd = count_wd(start, last) if start <= last else 0
    present = sum(weight(l) for l in rs if l["status"] in PRES and weekday(l["date"]))
    return {
        "emp_code": code, "month": month, "working_days": wd, "present_days": rhu(present, 2),
        "leave_days": sum(l["status"] == "LEAVE" for l in rs),
        "late_count": sum(l["late_minutes"] > 0 for l in rs),
        "total_late_minutes": sum(l["late_minutes"] for l in rs),
        "total_overtime_minutes": sum(l["overtime_minutes"] for l in rs),
        "attendance_pct": rhu(Decimal(str(present)) / wd * 100, 2) if wd else None,
    }


def ref_summary(month, dept=None):
    first, last = month_range(month)
    out = []
    for dp in sorted({e["department"] for e in EMP.values()}):
        if dept and dp != dept:
            continue
        codes = {c for c, e in EMP.items() if e["department"] == dp and e["joined_on"] <= last}
        if not codes:
            continue
        rs = [l for l in LOGS if l["emp_code"] in codes and first <= l["date"] <= last]
        whs = [Decimal(str(l["work_hours"])) for l in rs if l["status"] in PRES and l["work_hours"] is not None]
        out.append({
            "department": dp, "headcount": len(codes),
            "present_days": rhu(sum(weight(l) for l in rs if l["status"] in PRES and weekday(l["date"])), 2),
            "avg_work_hours": rhu(sum(whs) / len(whs), 2) if whs else None,
            "late_count": sum(l["late_minutes"] > 0 for l in rs),
            "total_late_minutes": sum(l["late_minutes"] for l in rs),
            "leave_count": sum(l["status"] == "LEAVE" for l in rs),
            "on_duty_count": sum(l["status"] == "ON_DUTY" for l in rs),
        })
    return {"month": month, "items": out}


def ref_leaderboard(month, limit=10, dept=None):
    first, last = month_range(month)
    tot, cnt = {}, {}
    for l in LOGS:
        if first <= l["date"] <= last and l["late_minutes"] > 0 and l["emp_code"] in EMP:
            if dept and EMP[l["emp_code"]]["department"] != dept:
                continue
            tot[l["emp_code"]] = tot.get(l["emp_code"], 0) + l["late_minutes"]
            cnt[l["emp_code"]] = cnt.get(l["emp_code"], 0) + 1
    rows = sorted(tot, key=lambda c: (-tot[c], c))
    items = []
    for c in rows:
        rank = 1 + sum(1 for o in tot.values() if o > tot[c])
        if rank <= limit:
            items.append({"rank": rank, "emp_code": c, "name": EMP[c]["name"], "department": EMP[c]["department"],
                          "total_late_minutes": tot[c], "late_count": cnt[c]})
    return {"month": month, "items": items}


def ref_trend(dept, d_from, d_to):
    codes = [c for c, e in EMP.items() if e["department"] == dept]
    days, d = [], date.fromisoformat(d_from)
    while d <= date.fromisoformat(d_to):
        days.append(d.isoformat())
        d += timedelta(days=1)
    rows = []
    for day in days:
        hc = sum(EMP[c]["joined_on"] <= day for c in codes)
        rs = [l for l in LOGS if l["emp_code"] in codes and l["date"] == day]
        present = sum(weight(l) for l in rs if l["status"] in PRES)
        wd = weekday(day)
        rate = Decimal(str(present)) / hc if wd and hc > 0 else None
        rows.append({"date": day, "is_working_day": wd, "headcount": hc, "present_count": float(present),
                     "late_count": sum(l["late_minutes"] > 0 for l in rs), "_rate": rate})
    for i, r in enumerate(rows):
        window = [x["_rate"] for x in rows[max(0, i - 6): i + 1] if x["_rate"] is not None]
        r["attendance_rate"] = rhu(r["_rate"], 4) if r["_rate"] is not None else None
        r["moving_avg_7d"] = rhu(sum(window) / len(window), 4) if window else None
    for r in rows:
        del r["_rate"]
    return {"department": dept, "items": rows}


# --------------------------------------------------------------------------- http helpers
def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


FAILS = 0


def check(name, got, want):
    global FAILS
    if got == want:
        print(f"PASS  {name}")
    else:
        FAILS += 1
        print(f"FAIL  {name}")
        if isinstance(got, dict) and isinstance(want, dict) and "items" in want and isinstance(got.get("items"), list):
            for i, (g, w) in enumerate(zip(got["items"], want["items"])):
                if g != w:
                    print(f"      row {i}\n        got : {g}\n        want: {w}")
                    break
            if len(got["items"]) != len(want["items"]):
                print(f"      row count got {len(got['items'])}, want {len(want['items'])}")
        else:
            print(f"      got : {got}\n      want: {want}")


def ms(day, h, m, s=0, extra_ms=0):
    return int(datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:{s:02d}+05:30").timestamp()) * 1000 + extra_ms


def q(**kw):
    return "?" + urllib.parse.urlencode(kw)


# --------------------------------------------------------------------------- main
def main():
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=8000)[DB_NAME]
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([dict(e) for e in EMP.values()])
    db.attendance_logs.insert_many([dict(l) for l in LOGS])
    print(f"seeded {len(EMP)} employees and {len(LOGS)} logs into '{DB_NAME}'\n")

    s, _ = call("GET", "/health")
    check("health is 200", s, 200)

    print("\n-- employee monthly")
    for code in ("EMP1001", "EMP1002", "EMP1003", "EMP1004", "EMP2001", "EMP2002", "EMP3001"):
        s, j = call("GET", f"/analytics/employees/{code}/monthly" + q(month=MONTH))
        check(f"monthly {code}", j if s == 200 else (s, j), ref_monthly(code, MONTH))
    check("monthly unknown employee -> 404", call("GET", "/analytics/employees/EMP0000/monthly" + q(month=MONTH))[0], 404)
    check("monthly bad month -> 422", call("GET", "/analytics/employees/EMP1001/monthly" + q(month="2026-13"))[0], 422)
    s, j = call("GET", "/analytics/employees/EMP3001/monthly" + q(month=MONTH))
    check("monthly joiner after month: working_days 0, pct null", (j.get("working_days"), j.get("attendance_pct")), (0, None))

    print("\n-- department summary")
    s, j = call("GET", "/analytics/departments/summary" + q(month=MONTH))
    check("summary all departments", j if s == 200 else (s, j), ref_summary(MONTH))
    s, j = call("GET", "/analytics/departments/summary" + q(month=MONTH, department="Eng"))
    check("summary department=Eng", j if s == 200 else (s, j), ref_summary(MONTH, "Eng"))
    s, j = call("GET", "/analytics/departments/summary" + q(month="2026-08"))
    check("summary August (Future dept now counted)", j if s == 200 else (s, j), ref_summary("2026-08"))
    s, j = call("GET", "/analytics/departments/summary" + q(month="2025-12"))
    check("summary before anyone joined -> empty", j if s == 200 else (s, j), ref_summary("2025-12"))
    if s == 200:
        eng = [i for i in call("GET", "/analytics/departments/summary" + q(month=MONTH))[1]["items"] if i["department"] == "Eng"]
        check("Eng headcount is 4 (no-logs employee counted, mid-month joiner counted)", eng[0]["headcount"] if eng else None, 4)

    print("\n-- late leaderboard (expected ranks 1, 2, 2, 4, 5)")
    for kw in ({"limit": 10}, {"limit": 2}, {"limit": 1}, {"limit": 10, "department": "Ops"}):
        s, j = call("GET", "/analytics/leaderboard/late" + q(month=MONTH, **kw))
        check(f"leaderboard {kw}", j if s == 200 else (s, j),
              ref_leaderboard(MONTH, kw["limit"], kw.get("department")))
    s, j = call("GET", "/analytics/leaderboard/late" + q(month=MONTH, limit=10))
    if s == 200:
        check("ranks are 1,2,2,4,5 (tie shares rank 2, next rank skipped)", [i["rank"] for i in j["items"]], [1, 2, 2, 4, 5])
        check("ghost employee EMP9999 ignored", "EMP9999" in [i["emp_code"] for i in j["items"]], False)
    check("leaderboard limit 51 -> 422", call("GET", "/analytics/leaderboard/late" + q(month=MONTH, limit=51))[0], 422)

    print("\n-- department trend")
    for dp, a, b in (("Eng", "2026-07-01", "2026-07-31"), ("Ops", "2026-07-05", "2026-07-20"),
                     ("Eng", "2026-07-14", "2026-07-16"), ("Future", "2026-07-30", "2026-08-03")):
        s, j = call("GET", f"/analytics/departments/{dp}/trend" + q(**{"from": a, "to": b}))
        check(f"trend {dp} {a}..{b}", j if s == 200 else (s, j), ref_trend(dp, a, b))
        if s == 200:
            check(f"   one row per day ({dp})", len(j["items"]), (date.fromisoformat(b) - date.fromisoformat(a)).days + 1)
    check("trend unknown department -> 404", call("GET", "/analytics/departments/Nope/trend" + q(**{"from": "2026-07-01", "to": "2026-07-05"}))[0], 404)
    check("trend to < from -> 422", call("GET", "/analytics/departments/Eng/trend" + q(**{"from": "2026-07-05", "to": "2026-07-01"}))[0], 422)
    check("trend 93 days -> 422", call("GET", "/analytics/departments/Eng/trend" + q(**{"from": "2026-01-01", "to": "2026-04-03"}))[0], 422)
    check("trend 92 days -> 200", call("GET", "/analytics/departments/Eng/trend" + q(**{"from": "2026-01-01", "to": "2026-04-02"}))[0], 200)

    print("\n-- explain (must use IXSCAN, never COLLSCAN)")
    cases = {
        "attendance_list": {}, "attendance_list ": {"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31"},
        "department_summary": {"month": MONTH}, "late_leaderboard": {"month": MONTH, "limit": 5},
        "employee_monthly": {"emp_code": "EMP1001", "month": MONTH},
        "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"},
    }
    for name, params in cases.items():
        s, j = call("GET", f"/admin/explain/{name.strip()}" + q(**params))
        text = json.dumps(j)
        check(f"explain {name.strip()} {params or ''}".strip(), (s, "IXSCAN" in text, "COLLSCAN" in text), (200, True, False))
    check("explain missing params -> 422", call("GET", "/admin/explain/department_summary")[0], 422)

    print("\n-- rules through the API (R1 - R5, validation)")
    day = "2026-09-07"  # a Monday
    for code in ("EMP7001", "EMP7002", "EMP7003", "EMP7004"):
        call("POST", "/employees", {"emp_code": code, "name": code, "email": f"{code}@x.com", "department": "Tmp",
                                    "joined_on": "2026-01-01"})
    call("POST", "/employees", {"emp_code": "EMP7005", "name": "N", "email": "n@x.com", "department": "Tmp",
                                "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-01"})
    pin = lambda c, *t, **k: call("POST", "/attendance/punch-in", {"emp_code": c, "punched_at": ms(day, *t, **k)})[1]
    check("late 09:40:00 -> 0", pin("EMP7001", 9, 40, 0).get("late_minutes"), 0)
    check("late 09:40:01 -> 10", pin("EMP7002", 9, 40, 1).get("late_minutes"), 10)
    check("late 10:15:59 -> 45", pin("EMP7003", 10, 15, 59).get("late_minutes"), 45)
    r = pin("EMP7004", 9, 40, 0, extra_ms=900)
    check("09:40:00.900 truncated to 09:40:00 (late 0, ms stored without fraction)", (r.get("late_minutes"), r.get("punch_in") % 1000), (0, 0))
    out = lambda c, *t: call("POST", "/attendance/punch-out", {"emp_code": c, "punched_at": ms(day, *t)})[1]
    check("overtime exactly 30 min counts", out("EMP7001", 19, 0).get("overtime_minutes"), 30)
    check("overtime 29:59 -> 0", out("EMP7002", 18, 59, 59).get("overtime_minutes"), 0)
    r = out("EMP7003", 10, 16, 17)
    check("18 s = 0.005 h rounds half-up to 0.01 and is a half day", (r.get("work_hours"), r.get("half_day")), (0.01, True))
    # overnight: punch-in 02:00 belongs to the shift that started the day before
    s, r = call("POST", "/attendance/punch-in", {"emp_code": "EMP7005", "punched_at": ms("2026-09-08", 2, 0)})
    check("overnight punch-in at 02:00 -> date is the day before", r.get("date"), "2026-09-07")
    check("seconds instead of ms -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": 1783312500})[0], 422)
    check("status ABSENT on punch-in -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "status": "ABSENT"})[0], 422)
    check("unknown employee -> 404", call("POST", "/attendance/punch-in", {"emp_code": "EMP0000"})[0], 404)
    check("second punch-in same day -> 409", call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": ms(day, 9, 0)})[0], 409)
    check("second punch-out -> 409", call("POST", "/attendance/punch-out", {"emp_code": "EMP7001", "punched_at": ms(day, 20, 0)})[0], 409)
    s, r = call("PATCH", f"/attendance/EMP7001/{day}", {"reason": "forgot to punch", "regularized_by": "hr", "status": "LEAVE"})
    check("PATCH to LEAVE clears punches, adds one history entry",
          (s, r.get("punch_in"), r.get("work_hours"), len(r.get("history", []))), (200, None, None, 1))
    check("PATCH that changes nothing -> 422",
          call("PATCH", f"/attendance/EMP7001/{day}", {"reason": "same again", "regularized_by": "hr", "status": "LEAVE"})[0], 422)

    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(FAILS) + ' CHECK(S) FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
