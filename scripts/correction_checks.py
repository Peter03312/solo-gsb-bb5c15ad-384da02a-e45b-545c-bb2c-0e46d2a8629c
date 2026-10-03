"""更正声明专项检查：顺序生效、显式置空 vs 未提交、幂等/冲突、并发不乱序不丢失、
撤销/失效证书可更正且既有契约不变、数据库层不可改写。

直接压 HTTP 接口（urllib，无第三方依赖）。
用法: python3 scripts/correction_checks.py [base_url]
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
results = []


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)


def iso(dt):
    return dt.isoformat()


def ref(no, dev, name=None, operator=None, remark=None, days=600):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": dev, "device_name": name,
        "valid_from": iso(t0), "valid_to": iso(t0 + timedelta(days=days)),
        "calibrated_at": iso(t0 + timedelta(hours=9)),
        "operator": operator, "remark": remark,
    })


def correct(no, stmt, changes, reason):
    return call("POST", f"/api/v1/certificates/{no}/corrections", {
        "statement_no": stmt, "changes": changes, "reason": reason,
    })


print("== A. 顺序生效与合并；显式置空与未提交严格区分 ==")
s, _ = ref("REF-K1", "DEV-K1", name="原名", operator="原操作", remark="原备注")
check("前置证书 201", s == 201, f"got {s}")

s, body = correct("REF-K1", "K-S1", {"device_name": "名一", "operator": "操一"}, "笔误")
check("声明1 201, seq=1", s == 201 and body["statement"]["seq"] == 1
      and body["current_display"] == {"device_name": "名一", "operator": "操一", "remark": "原备注"},
      json.dumps(body.get("current_display"), ensure_ascii=False))

# remark 显式置空；device_name/operator 未提交必须保持
s, body = correct("REF-K1", "K-S2", {"remark": None}, "清空备注")
check("声明2 显式置空 remark，未提交字段保持",
      s == 201 and body["statement"]["seq"] == 2
      and body["current_display"] == {"device_name": "名一", "operator": "操一", "remark": None},
      json.dumps(body.get("current_display"), ensure_ascii=False))

# 空串也是显式提交（置空的一种文本值）；只改 device_name 不影响 operator
s, body = correct("REF-K1", "K-S3", {"device_name": ""}, "名称清空为空白文本")
check("声明3 空串显式提交，仅影响本字段",
      s == 201 and body["statement"]["seq"] == 3
      and body["current_display"] == {"device_name": "", "operator": "操一", "remark": None},
      json.dumps(body.get("current_display"), ensure_ascii=False))

# null 后可再改回文本
s, body = correct("REF-K1", "K-S4", {"remark": "补回备注"}, "恢复备注")
check("声明4 null 可再改回文本",
      s == 201 and body["current_display"]["remark"] == "补回备注",
      json.dumps(body.get("current_display"), ensure_ascii=False))

s, body = call("GET", "/api/v1/certificates/REF-K1/corrections")
seqs = [(c["seq"], c["statement_no"]) for c in body["corrections"]]
check("查询返回完整声明历史且按 seq 升序",
      s == 200 and seqs == [(1, "K-S1"), (2, "K-S2"), (3, "K-S3"), (4, "K-S4")], str(seqs))
check("查询返回原证书（签发内容未被改写）",
      body["certificate"]["device_name"] == "原名"
      and body["certificate"]["operator"] == "原操作"
      and body["certificate"]["remark"] == "原备注",
      json.dumps(body["certificate"], ensure_ascii=False)[:200])

# 原证书详情接口契约不变
s, body = call("GET", "/api/v1/certificates/REF-K1")
check("原证书详情仍展示原始字段",
      s == 200 and body["device_name"] == "原名" and body["operator"] == "原操作",
      json.dumps(body, ensure_ascii=False)[:200])

print("== B. 幂等：同编号同内容重放首次声明；同编号异内容冲突 ==")
s1, b1 = correct("REF-K1", "K-S2", {"remark": None}, "清空备注")
check("同编号同内容重放 -> 200", s1 == 200, f"got {s1}")
check("重放返回首次声明快照（seq 仍为 2，历史为首次时点的 2 条）",
      b1["statement"]["seq"] == 2 and len(b1["corrections"]) == 2,
      f"seq={b1['statement']['seq']} hist={len(b1['corrections'])}")
# 键顺序不同但语义相同 => 同内容
s2, b2 = correct("REF-K1", "K-S2", {"remark": None}, "清空备注")
check("完全相同请求再次重放仍 200", s2 == 200, f"got {s2}")

s, body = correct("REF-K1", "K-S2", {"remark": "不同"}, "不同原因")
check("同编号异内容 -> 409 correction_conflict",
      s == 409 and body["error"]["code"] == "correction_conflict",
      json.dumps(body, ensure_ascii=False)[:200])

# 同编号用于另一张证书也判冲突
s, _ = ref("REF-K2", "DEV-K2")
s, body = correct("REF-K2", "K-S2", {"remark": None}, "清空备注")
check("同编号用于不同证书 -> 409 correction_conflict",
      s == 409 and body["error"]["code"] == "correction_conflict",
      json.dumps(body, ensure_ascii=False)[:200])

print("== C. 错误路径 ==")
s, body = correct("NO-SUCH-CERT", "E-S1", {"operator": "甲"}, "r")
check("证书不存在 -> 404 cert_not_found",
      s == 404 and body["error"]["code"] == "cert_not_found", f"got {s}")
s, body = correct("REF-K1", "E-S2", {"status": "REVOKED"}, "r")
check("越界字段 -> 422 correction_field_invalid",
      s == 422 and body["error"]["code"] == "correction_field_invalid", f"got {s}")
s, body = correct("REF-K1", "E-S3", {"valid_from": "x"}, "r")
check("有效期字段不可更正 -> 422", s == 422
      and body["error"]["code"] == "correction_field_invalid", f"got {s}")
s, body = correct("REF-K1", "E-S4", {}, "r")
check("空修正 -> 422 correction_empty",
      s == 422 and body["error"]["code"] == "correction_empty", f"got {s}")
s, body = correct("REF-K1", "E-S5", {"operator": "甲"}, "   ")
check("空白原因 -> 422 correction_reason_empty",
      s == 422 and body["error"]["code"] == "correction_reason_empty", f"got {s}")
s, body = correct("REF-K1", "E-S6", {"operator": 123}, "r")
check("非文本/非 null 值 -> 422", s == 422
      and body["error"]["code"] == "correction_field_invalid", f"got {s}")
s, body = correct("REF-K1", "E-S7", {"operator": "长" * 129}, "r")
check("字段超长 -> 422", s == 422
      and body["error"]["code"] == "correction_field_invalid", f"got {s}")
s, body = call("GET", "/api/v1/certificates/REF-K1/corrections/NOT-EXIST")
check("单条声明不存在 -> 404 correction_not_found",
      s == 404 and body["error"]["code"] == "correction_not_found", f"got {s}")

print("== D. 并发更正同一证书：seq 连续不丢失、不乱序 ==")
s, _ = ref("REF-P", "DEV-P", name="并发原名")
check("前置证书 201", s == 201, f"got {s}")

N = 8
outcomes = []
lock = threading.Lock()
barrier = threading.Barrier(N)


def worker(i):
    barrier.wait()  # 尽量让 N 个请求同时打入
    st, bd = correct("REF-P", f"P-S{i:02d}", {"device_name": f"并发名{i}"}, f"并发原因{i}")
    with lock:
        outcomes.append((i, st, bd))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
[t.start() for t in threads]
[t.join() for t in threads]
created = [o for o in outcomes if o[1] == 201]
check("8 个并发声明全部首次登记成功", len(created) == N,
      json.dumps([(o[0], o[1]) for o in outcomes]))
seqs = sorted(o[2]["statement"]["seq"] for o in created)
check("seq 恰好为 1..8（不丢失、不重复）", seqs == list(range(1, N + 1)), str(seqs))

s, body = call("GET", "/api/v1/certificates/REF-P/corrections")
hist_seqs = [c["seq"] for c in body["corrections"]]
check("历史按 seq 升序，共 8 条", hist_seqs == list(range(1, N + 1)), str(hist_seqs))
# 声明顺序即生效顺序：当前名称应等于最后一条声明（seq=8）的写入
last_change = body["corrections"][-1]["changes"]["device_name"]
stmt_by_no = {c["statement_no"]: c for c in body["corrections"]}
winner = next(no for no, c in stmt_by_no.items() if c["changes"]["device_name"] == last_change)
check("当前展示 = 最后生效声明的值（按提交顺序合并）",
      body["current_display"]["device_name"] == last_change
      and stmt_by_no[winner]["seq"] == N,
      f"current={body['current_display']['device_name']} last={last_change}")

print("== E. 并发同声明编号：仅一次首次登记，其余重放/冲突 ==")
s, _ = ref("REF-Q", "DEV-Q", name="Q原名")
outcomes_q = []


def worker_q(i):
    barrier_q.wait()
    # 一半线程发同内容，一半线程发异内容
    if i % 2 == 0:
        st, bd = correct("REF-Q", "Q-DUP", {"operator": f"同内容"}, "同原因")
    else:
        st, bd = correct("REF-Q", "Q-DUP", {"operator": f"异内容{i}"}, "异原因")
    with lock:
        outcomes_q.append((i, st, bd))


barrier_q = threading.Barrier(N)
threads = [threading.Thread(target=worker_q, args=(i,)) for i in range(N)]
[t.start() for t in threads]
[t.join() for t in threads]
first = [o for o in outcomes_q if o[1] == 201]
replay = [o for o in outcomes_q if o[1] == 200]
conflict = [o for o in outcomes_q if o[1] == 409]
check("同编号并发：恰好 1 个首次登记 201", len(first) == 1,
      json.dumps([(o[0], o[1]) for o in outcomes_q]))
check("同内容线程重放 200、异内容线程 409，无 5xx",
      len(replay) + len(conflict) == N - 1
      and all(o[2]["error"]["code"] == "correction_conflict" for o in conflict)
      and all(o[2]["statement"]["seq"] == 1 for o in replay),
      json.dumps([(o[0], o[1]) for o in outcomes_q]))
s, body = call("GET", "/api/v1/certificates/REF-Q/corrections")
check("同编号并发仅落库 1 条声明", len(body["corrections"]) == 1,
      f"count={len(body['corrections'])}")

print("== F. 已撤销/级联失效证书可更正，状态与有效期判定不受影响 ==")
s, _ = ref("REF-R", "DEV-R", name="基准R")
s, _ = call("POST", "/api/v1/certificates", {
    "cert_no": "CAL-R", "device_id": "DEV-RC", "device_name": "下游R",
    "valid_from": iso(t0 + timedelta(days=30)), "valid_to": iso(t0 + timedelta(days=300)),
    "calibrated_at": iso(t0 + timedelta(days=31)),
    "conclusion": "PASS", "parent_cert_no": "REF-R", "operator": "李"})
s, _ = call("POST", "/api/v1/certificates/REF-R/revoke", {"reason": "超差"})
s, body = call("GET", "/api/v1/certificates/CAL-R")
check("下游已 INVALID", s == 200 and body["status"] == "INVALID", body.get("status"))

s, body = correct("REF-R", "R-S1", {"device_name": "基准R-更正"}, "撤销后更正")
check("REVOKED 证书可更正",
      s == 201 and body["certificate"]["status"] == "REVOKED"
      and body["current_display"]["device_name"] == "基准R-更正",
      f"got {s}")
s, body = correct("CAL-R", "R-S2", {"operator": "更正操作员", "remark": None}, "失效后更正")
check("INVALID 证书可更正",
      s == 201 and body["certificate"]["status"] == "INVALID"
      and body["current_display"]["operator"] == "更正操作员"
      and body["current_display"]["remark"] is None,
      f"got {s}")

# 撤销时间/原因/有效期原样保留
s, body = call("GET", "/api/v1/certificates/REF-R/corrections/R-S1")
check("更正不改撤销元数据与有效期",
      body["certificate"]["revoked_reason"] == "超差"
      and body["certificate"]["revoked_at"] is not None
      and body["certificate"]["valid_from"] is not None,
      json.dumps(body["certificate"], ensure_ascii=False)[:220])

# 追溯链契约不变（节点仍是签发时原始内容）
s, body = call("GET", "/api/v1/certificates/CAL-R/chain")
names = [(n["certificate"]["cert_no"], n["certificate"]["device_name"])
         for n in body["nodes"]]
check("追溯链节点仍为原始签发内容",
      names == [("REF-R", "基准R"), ("CAL-R", "下游R")], str(names))

# 失效证书仍不能作为签发依据（状态判定不受更正影响）
s, body = call("POST", "/api/v1/certificates", {
    "cert_no": "CAL-R-USE", "device_id": "DEV-RU",
    "valid_from": iso(t0 + timedelta(days=40)), "valid_to": iso(t0 + timedelta(days=200)),
    "calibrated_at": iso(t0 + timedelta(days=41)),
    "conclusion": "PASS", "parent_cert_no": "CAL-R"})
check("更正后失效证书仍不得作校准依据 -> 422 reference_not_active",
      s == 422 and body["error"]["code"] == "reference_not_active", f"got {s}")

print("== G. 单条声明查询 ==")
s, body = call("GET", "/api/v1/certificates/REF-K1/corrections/K-S4")
check("单条声明 200，含 statement 与完整历史/当前展示",
      s == 200 and body["statement"]["statement_no"] == "K-S4"
      and len(body["corrections"]) == 4
      and body["current_display"]["remark"] == "补回备注",
      f"got {s}")

fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
