"""
Diagnostic: runs every analytics pipeline directly against MongoDB and prints the real error, if any.
It does NOT need the server.  NOT part of the submission (do not commit it).

  $env:MONGO_DB = "attendance_test"      # the database that hard_tests.py seeded
  python diag.py
"""
import os
import traceback

import app.main as m

print("database:", m.db.name)
print("employees:", m.db.employees.count_documents({}), " logs:", m.db.attendance_logs.count_documents({}))
print("indexes on attendance_logs:", sorted(m.db.attendance_logs.index_information()))
print("indexes on employees:", sorted(m.db.employees.index_information()))

builders = {
    "employee_monthly": m.build_monthly_pipeline("EMP1001", "2026-07"),
    "department_summary": m.build_department_summary_pipeline("2026-07", None),
    "late_leaderboard": m.build_leaderboard_pipeline("2026-07", 10, None),
    "department_trend": m.build_trend_pipeline("Eng", "2026-07-01", "2026-07-31"),
}
for name, (coll, pipeline, hint) in builders.items():
    print(f"\n=== {name} (collection {coll}, hint {hint})")
    try:
        rows = list(m.db[coll].aggregate(pipeline, hint=hint))
        print(f"OK, {len(rows)} row(s); first: {rows[0] if rows else None}")
    except Exception:
        traceback.print_exc()

print("\n=== employee_monthly() endpoint function")
try:
    print(m.employee_monthly("EMP1001", "2026-07"))
except Exception:
    traceback.print_exc()
