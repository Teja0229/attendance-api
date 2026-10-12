"""
Concurrency test.  NOT part of the submission (do not commit it).

Fires parallel requests at the running API and checks the exact guarantees from the assignment:
  * N parallel POST /employees with the same emp_code     -> exactly one 201, the rest 409
  * N parallel punch-ins for the same employee and date   -> exactly one 201, the rest 409
  * N parallel punch-outs for the same record             -> exactly one 200, the rest 409
  * N parallel PATCH (regularize) on the same record      -> no history entry is lost
                                                             (history length == number of 200 answers)

Run (server already running, any database; it only creates its own random employee):
  python concurrency_test.py
"""
import json
import os
import random
import threading
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime

BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
N = 20


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


def blast(n, fn):
    """Run fn(i) in n threads released at the same instant. Returns the list of results."""
    barrier, results = threading.Barrier(n), [None] * n

    def worker(i):
        barrier.wait()
        results[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp()) * 1000


failures = 0


def verdict(name, ok, detail):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}   {detail}")


def main():
    code = f"EMP{random.randint(100000, 999999)}"
    day = "2026-10-05"  # a Monday
    emp = {"emp_code": code, "name": "Race Test", "email": "race@example.com", "department": "RaceDept",
           "joined_on": "2026-01-01"}

    res = blast(N, lambda i: call("POST", "/employees", emp)[0])
    c = Counter(res)
    verdict(f"{N} parallel employee creates -> one 201, rest 409", c[201] == 1 and c[409] == N - 1, dict(c))

    body = {"emp_code": code, "punched_at": ms(f"{day}T09:30:00+05:30")}
    res = blast(N, lambda i: call("POST", "/attendance/punch-in", body)[0])
    c = Counter(res)
    verdict(f"{N} parallel punch-ins -> one 201, rest 409", c[201] == 1 and c[409] == N - 1, dict(c))
    s, page = call("GET", f"/attendance?emp_code={code}")
    verdict("exactly one record stored", page.get("total") == 1, f"total={page.get('total')}")

    body = {"emp_code": code, "punched_at": ms(f"{day}T18:30:00+05:30")}
    res = blast(N, lambda i: call("POST", "/attendance/punch-out", body)[0])
    c = Counter(res)
    verdict(f"{N} parallel punch-outs -> one 200, rest 409", c[200] == 1 and c[409] == N - 1, dict(c))

    def regularize(i):
        # every request asks for a different, valid punch_out, so every success is a real change
        t = ms(f"{day}T18:{31 + i:02d}:00+05:30")
        return call("PATCH", f"/attendance/{code}/{day}",
                    {"punch_out": t, "reason": f"race edit number {i}", "regularized_by": f"tester{i}"})[0]

    res = blast(N, regularize)
    c = Counter(res)
    ok_count = c[200]
    s, page = call("GET", f"/attendance?emp_code={code}")
    history = page["items"][0]["history"] if page.get("items") else []
    only_expected = set(c) <= {200, 409}
    verdict(f"{N} parallel corrections: only 200/409 answers", only_expected, dict(c))
    verdict("history length equals the number of 200 answers (nothing lost)",
            len(history) == ok_count and ok_count >= 1, f"200s={ok_count}, history entries={len(history)}")

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
