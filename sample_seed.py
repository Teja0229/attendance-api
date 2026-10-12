"""Loads the 9 sample documents into your database (self-contained, no sample_data folder needed).
Usage:  python sample_seed.py     (reads MONGO_URI / MONGO_DB from .env)
Warning: it empties the employees and attendance_logs collections of the database named in MONGO_DB."""
import os

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
uri, name = os.getenv("MONGO_URI"), os.getenv("MONGO_DB")
if not uri or not name:
    raise SystemExit("Set MONGO_URI and MONGO_DB in your .env file first.")

EMPLOYEES = r'''[
  {
    "emp_code": "EMP0001",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-05",
    "created_at": {
      "$date": "2026-01-05T04:30:00Z"
    }
  },
  {
    "emp_code": "EMP0002",
    "name": "Vikram Shah",
    "email": "vikram@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-07-13",
    "created_at": {
      "$date": "2026-07-13T04:00:00Z"
    }
  },
  {
    "emp_code": "EMP0003",
    "name": "Neha Iyer",
    "email": "neha@example.com",
    "department": "Sales",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-02-01",
    "created_at": {
      "$date": "2026-02-01T04:30:00Z"
    }
  },
  {
    "emp_code": "EMP0004",
    "name": "Rohit Das",
    "email": "rohit@example.com",
    "department": "Sales",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-02-01",
    "created_at": {
      "$date": "2026-02-01T04:31:00Z"
    }
  },
  {
    "emp_code": "EMP0005",
    "name": "Meera Nair",
    "email": "meera@example.com",
    "department": "Support",
    "shift_start": "22:00",
    "shift_end": "06:00",
    "joined_on": "2026-03-10",
    "created_at": {
      "$date": "2026-03-10T04:30:00Z"
    }
  },
  {
    "emp_code": "EMP0006",
    "name": "Karan Bedi",
    "email": "karan@example.com",
    "department": "Support",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-03-10",
    "created_at": {
      "$date": "2026-03-10T04:31:00Z"
    }
  }
]'''

LOGS = r'''[
  {
    "emp_code": "EMP0001",
    "date": "2026-07-06",
    "status": "PRESENT",
    "punch_in": {
      "$date": "2026-07-06T03:58:00Z"
    },
    "punch_out": {
      "$date": "2026-07-06T13:05:00Z"
    },
    "work_hours": 9.12,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": false,
    "history": []
  },
  {
    "emp_code": "EMP0003",
    "date": "2026-07-06",
    "status": "PRESENT",
    "punch_in": {
      "$date": "2026-07-06T04:35:00Z"
    },
    "punch_out": {
      "$date": "2026-07-06T13:40:00Z"
    },
    "work_hours": 9.08,
    "late_minutes": 35,
    "overtime_minutes": 40,
    "half_day": false,
    "history": []
  },
  {
    "emp_code": "EMP0006",
    "date": "2026-07-07",
    "status": "PRESENT",
    "punch_in": {
      "$date": "2026-07-07T04:05:00Z"
    },
    "punch_out": null,
    "work_hours": null,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": false,
    "history": []
  },
  {
    "emp_code": "EMP0004",
    "date": "2026-07-07",
    "status": "WFH",
    "punch_in": {
      "$date": "2026-07-07T04:00:00Z"
    },
    "punch_out": {
      "$date": "2026-07-07T07:30:00Z"
    },
    "work_hours": 3.5,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": true,
    "history": []
  },
  {
    "emp_code": "EMP0004",
    "date": "2026-07-08",
    "status": "ABSENT",
    "punch_in": null,
    "punch_out": null,
    "work_hours": null,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": false,
    "history": []
  },
  {
    "emp_code": "EMP0003",
    "date": "2026-07-08",
    "status": "LEAVE",
    "punch_in": null,
    "punch_out": null,
    "work_hours": null,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": false,
    "history": []
  },
  {
    "emp_code": "EMP0005",
    "date": "2026-07-06",
    "status": "PRESENT",
    "punch_in": {
      "$date": "2026-07-06T16:25:00Z"
    },
    "punch_out": {
      "$date": "2026-07-07T01:10:00Z"
    },
    "work_hours": 8.75,
    "late_minutes": 0,
    "overtime_minutes": 40,
    "half_day": false,
    "history": []
  },
  {
    "emp_code": "EMP0001",
    "date": "2026-07-07",
    "status": "PRESENT",
    "punch_in": {
      "$date": "2026-07-07T03:58:00Z"
    },
    "punch_out": {
      "$date": "2026-07-07T13:00:00Z"
    },
    "work_hours": 9.03,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": false,
    "history": [
      {
        "at": {
          "$date": "2026-07-08T05:00:00Z"
        },
        "by": "hr.admin",
        "reason": "biometric glitch",
        "changes": {
          "punch_in": {
            "from": {
              "$date": "2026-07-07T04:35:00Z"
            },
            "to": {
              "$date": "2026-07-07T03:58:00Z"
            }
          },
          "late_minutes": {
            "from": 35,
            "to": 0
          },
          "work_hours": {
            "from": 8.42,
            "to": 9.03
          }
        }
      }
    ]
  },
  {
    "emp_code": "EMP0002",
    "date": "2026-07-14",
    "status": "PRESENT",
    "punch_in": {
      "$date": "2026-07-14T04:02:00Z"
    },
    "punch_out": {
      "$date": "2026-07-14T13:01:00Z"
    },
    "work_hours": 8.98,
    "late_minutes": 0,
    "overtime_minutes": 0
  }
]'''

opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=8000)[name]
for coll, text in (("employees", EMPLOYEES), ("attendance_logs", LOGS)):
    docs = json_util.loads(text, json_options=opts)
    db[coll].delete_many({})
    db[coll].insert_many(docs)
    print(f"loaded {len(docs):>2} documents into '{db.name}.{coll}'")
