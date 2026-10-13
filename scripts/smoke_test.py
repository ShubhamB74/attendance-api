"""End-to-end smoke test against a RUNNING server (standard library only).
It creates one extra employee (EMP + 6 digits, department "Smoke") for the concurrency checks.

    python scripts/seed_test_data.py --wipe        # scratch database!
    uvicorn app.main:app --port 8000               # in another terminal
    python scripts/smoke_test.py [http://localhost:8000]

Expected numbers were worked out independently of app/main.py from the data in seed_test_data.py (do not run it
against a database that has other data in July 2026). Seeding with --bulk is fine (its data is 2025 only) and makes the
explain checks more meaningful.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
failures = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw.decode()


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    if not ok:
        failures.append(name)


def expect(name, got, want):
    check(name, got == want, f"got {got!r}, expected {want!r}")


def code(path, method="GET", body=None):
    return call(method, path, body)[0]


# --------------------------------------------------------------------------- basic + validation
expect("health", code("/health"), 200)
expect("unknown employee punch-in -> 404", code("/attendance/punch-in", "POST", {"emp_code": "NOPE"}), 404)
expect("duplicate employee -> 409", code("/employees", "POST", {
    "emp_code": "EMP1001", "name": "x", "email": "x@example.com", "department": "Engineering", "joined_on": "2026-01-05"}), 409)
expect("invalid shift -> 422", code("/employees", "POST", {
    "emp_code": "EMP7777", "name": "x", "email": "x@example.com", "department": "D", "shift_start": "99:99", "joined_on": "2026-01-05"}), 422)
expect("page_size 101 -> 422", code("/employees?page_size=101"), 422)
expect("page 0 -> 422", code("/attendance?page=0"), 422)
expect("date_from > date_to -> 422", code("/attendance?date_from=2026-07-09&date_to=2026-07-01"), 422)
expect("bad status filter -> 422", code("/attendance?status=banana"), 422)
expect("month 2026-13 -> 422", code("/analytics/departments/summary?month=2026-13"), 422)
s, body = call("GET", "/employees?department=Sales&page=1&page_size=2")
expect("employees: total respects filter, sorted", (body["total"], [e["emp_code"] for e in body["items"]]), (3, ["EMP1005", "EMP1006"]))
s, body = call("GET", "/attendance?emp_code=EMP1001&page_size=2")
expect("attendance list: date desc, total", (body["total"], [r["date"] for r in body["items"]]), (4, ["2026-07-08", "2026-07-07"]))
check("attendance item has epoch-ms ints and no id", isinstance(body["items"][0]["punch_in"], int) and "id" not in body["items"][0])

# --------------------------------------------------------------------------- monthly summary
def monthly(c, m="2026-07"):
    return call("GET", f"/analytics/employees/{c}/monthly?month={m}")[1]

want = {  # working_days, present_days, leave_days, late_count, total_late, total_ot, pct
    "EMP1001": (23, 3, 0, 2, 50, 40, 13.04), "EMP1002": (23, 1.5, 1, 1, 30, 0, 6.52),
    "EMP1003": (13, 2, 0, 0, 0, 0, 15.38), "EMP1004": (0, 0, 0, 0, 0, 0, None),
    "EMP1006": (23, 0, 0, 0, 0, 0, 0.0), "EMP1007": (23, 1, 0, 1, 30, 35, 4.35),
    "EMP1008": (23, 2, 0, 1, 140, 40, 8.7),
}
for c, w in want.items():
    m = monthly(c)
    got = (m["working_days"], m["present_days"], m["leave_days"], m["late_count"],
           m["total_late_minutes"], m["total_overtime_minutes"], m["attendance_pct"])
    expect(f"monthly {c}", got, w)
expect("monthly unknown employee -> 404", code("/analytics/employees/NOPE/monthly?month=2026-07"), 404)

# --------------------------------------------------------------------------- department summary
s, body = call("GET", "/analytics/departments/summary?month=2026-07")
rows = {r["department"]: r for r in body["items"]}
expect("summary departments", sorted(rows), ["Engineering", "Ops", "Sales"])
for dep, w in {"Engineering": (3, 6.5, 7.65, 3, 80, 1, 0), "Ops": (2, 3, 7.7, 2, 160, 0, 0),
               "Sales": (3, 3, 8.9, 2, 60, 0, 1)}.items():
    r = rows[dep]
    expect(f"summary {dep}", (r["headcount"], r["present_days"], r["avg_work_hours"], r["late_count"],
                              r["total_late_minutes"], r["leave_count"], r["on_duty_count"]), w)
s, body = call("GET", "/analytics/departments/summary?month=2026-01&department=Sales")
expect("summary January: Sales headcount 3 (joined 2026-01-05 <= 01-31), no logs", body["items"][0]["headcount"], 3)
s, body = call("GET", "/analytics/departments/summary?month=2026-01")
expect("summary January: Engineering counts 2 (EMP1003/EMP1004 not joined)", {r["department"]: r["headcount"] for r in body["items"]}["Engineering"], 2)

# --------------------------------------------------------------------------- leaderboard
def board(q):
    return [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in call("GET", "/analytics/leaderboard/late?month=2026-07" + q)[1]["items"]]

expect("leaderboard limit=2", board("&limit=2"), [(1, "EMP1008", 140), (2, "EMP1001", 50)])
expect("leaderboard limit=3 returns the whole tie (5 rows)", board("&limit=3"),
       [(1, "EMP1008", 140), (2, "EMP1001", 50), (3, "EMP1002", 30), (3, "EMP1005", 30), (3, "EMP1007", 30)])
expect("leaderboard limit=4 same 5 rows (rank 4 does not exist)", len(board("&limit=4")), 5)
expect("leaderboard limit=6 includes rank 6", board("&limit=6")[-1], (6, "EMP1009", 20))
expect("leaderboard Sales: both rank 1", board("&department=Sales"), [(1, "EMP1005", 30), (1, "EMP1007", 30)])
expect("leaderboard ignores unknown EMP9999", "EMP9999" in [r[1] for r in board("&limit=50")], False)
expect("leaderboard limit=51 -> 422", code("/analytics/leaderboard/late?month=2026-07&limit=51"), 422)

# --------------------------------------------------------------------------- trend
def trend(dep, a, b):
    s, body = call("GET", f"/analytics/departments/{dep}/trend?from={a}&to={b}")
    return s, body

s, body = trend("Engineering", "2026-07-04", "2026-07-10")
got = [(r["date"], r["is_working_day"], r["headcount"], r["present_count"], r["late_count"], r["attendance_rate"],
        r["moving_avg_7d"]) for r in body["items"]]
expect("trend Engineering 07-04..07-10", got, [
    ("2026-07-04", False, 2, 0.5, 1, None, None), ("2026-07-05", False, 2, 0, 0, None, None),
    ("2026-07-06", True, 2, 2, 1, 1.0, 1.0), ("2026-07-07", True, 2, 1.5, 1, 0.75, 0.875),
    ("2026-07-08", True, 2, 1, 0, 0.5, 0.75), ("2026-07-09", True, 2, 0, 0, 0.0, 0.5625),
    ("2026-07-10", True, 2, 0, 0, 0.0, 0.45)])
s, body = trend("Engineering", "2026-07-13", "2026-07-17")
expect("trend mid-month joiner + missing weekday", [(r["headcount"], r["attendance_rate"], r["moving_avg_7d"]) for r in body["items"]],
       [(2, 0.0, 0.0), (2, 0.0, 0.0), (3, 0.3333, 0.1111), (3, 0.3333, 0.1667), (3, 0.0, 0.1333)])
s, body = trend("Ops", "2026-07-05", "2026-07-08")
expect("trend Ops (overnight shift)", [(r["present_count"], r["late_count"], r["attendance_rate"]) for r in body["items"]],
       [(0, 0, None), (2, 1, 1.0), (1, 1, 0.5), (0, 0, 0.0)])
s, body = trend("Engineering", "2026-01-01", "2026-03-02")   # 61 days, long before anybody punched
expect("trend: one row per day", len(body["items"]), 61)
expect("trend: unknown department -> 404", trend("Nope", "2026-07-01", "2026-07-05")[0], 404)
expect("trend: to < from -> 422", trend("Engineering", "2026-07-05", "2026-07-01")[0], 422)
expect("trend: 92 days ok", trend("Engineering", "2026-04-01", "2026-07-01")[0], 200)
expect("trend: 93 days -> 422", trend("Engineering", "2026-04-01", "2026-07-02")[0], 422)
s, body = trend("Engineering", "2026-07-30", "2026-08-12")   # EMP1004 joins 08-10
expect("trend: future joiner counted from join day", [r["headcount"] for r in body["items"]][-4:], [3, 4, 4, 4])

# --------------------------------------------------------------------------- punch-in / punch-out / patch concurrency
emp = "EMP" + str(int(time.time()) % 1000000).zfill(6)
expect("create employee", code("/employees", "POST", {
    "emp_code": emp, "name": "Smoke Test", "email": "smoke@example.com", "department": "Smoke", "joined_on": "2026-01-01"}), 201)
t_in = 1788237000000   # 2026-09-01 10:00:00 IST (30 minutes after the 09:30 shift start)
with ThreadPoolExecutor(8) as pool:
    codes = list(pool.map(lambda _: code("/attendance/punch-in", "POST", {"emp_code": emp, "punched_at": t_in}), range(8)))
expect("8 parallel punch-ins -> one 201, rest 409", (codes.count(201), codes.count(409)), (1, 7))
s, rec = call("GET", f"/attendance?emp_code={emp}")
expect("punch-in record", (rec["items"][0]["date"], rec["items"][0]["late_minutes"], rec["items"][0]["punch_out"]), ("2026-09-01", 30, None))
t_out = t_in + 9 * 3600 * 1000 + 15 * 60 * 1000   # 19:15 -> 9.25 h, overtime 45
expect("punch-out before any punch-in -> 404", code("/attendance/punch-out", "POST", {"emp_code": emp, "punched_at": t_in - 1000}), 404)
expect("punch-out > 24 h later -> 422", code("/attendance/punch-out", "POST", {"emp_code": emp, "punched_at": t_in + 25 * 3600000}), 422)
with ThreadPoolExecutor(8) as pool:
    results = list(pool.map(lambda _: call("POST", "/attendance/punch-out", {"emp_code": emp, "punched_at": t_out}), range(8)))
expect("8 parallel punch-outs -> one 200", [r[0] for r in results].count(200), 1)
done = [r[1] for r in results if r[0] == 200][0]
expect("punch-out derived", (done["work_hours"], done["overtime_minutes"], done["half_day"]), (9.25, 45, False))
expect("punch-in with seconds instead of ms -> 422", code("/attendance/punch-in", "POST", {"emp_code": emp, "punched_at": 1788235800}), 422)

patch_path = f"/attendance/{emp}/2026-09-01"
expect("patch that changes nothing -> 422", code(patch_path, "PATCH", {"reason": "no change here", "regularized_by": "hr"}), 422)
bodies = [{"punch_in": t_in - (i + 1) * 60000, "reason": "forgot to punch", "regularized_by": f"hr{i}"} for i in range(6)]
with ThreadPoolExecutor(6) as pool:
    results = list(pool.map(lambda b: call("PATCH", patch_path, b), bodies))
ok_count = [r[0] for r in results].count(200)
s, rec = call("GET", f"/attendance?emp_code={emp}")
hist = rec["items"][0]["history"]
check("6 parallel patches: no status other than 200/409/422/404, history length == number of 200s",
      set(r[0] for r in results) <= {200, 409, 422} and len(hist) == ok_count and ok_count >= 1,
      f"statuses={[r[0] for r in results]} history={len(hist)}")
check("history entries hold from/to of changed fields only", all(set(h["changes"]) >= {"punch_in"} for h in hist))

# --------------------------------------------------------------------------- explain: IXSCAN and no COLLSCAN
explains = {
    "attendance_list": "",
    "attendance_list ": "?emp_code=EMP1001&status=PRESENT&date_from=2026-07-01&date_to=2026-07-31",
    "employee_monthly": "?emp_code=EMP1001&month=2026-07",
    "department_summary": "?month=2026-07",
    "department_summary ": "?month=2026-07&department=Sales",
    "late_leaderboard": "?month=2026-07&limit=5",
    "late_leaderboard ": "?month=2026-07&department=Sales",
    "department_trend": "?department=Engineering&from=2026-07-01&to=2026-07-31",
}
for name, qs in explains.items():
    s, body = call("GET", f"/admin/explain/{name.strip()}{qs}")
    text = json.dumps(body)
    check(f"explain {name.strip()} {qs[:40]}", s == 200 and "IXSCAN" in text and "COLLSCAN" not in text,
          f"status={s} IXSCAN={'IXSCAN' in text} COLLSCAN={'COLLSCAN' in text}")
expect("explain missing month -> 422", code("/admin/explain/employee_monthly?emp_code=EMP1001"), 422)
expect("explain unknown endpoint -> 422", code("/admin/explain/nonsense"), 422)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
