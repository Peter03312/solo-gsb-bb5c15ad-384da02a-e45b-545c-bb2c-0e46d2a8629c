#!/usr/bin/env bash
# 端到端冒烟测试：服务已启动后运行（BASE_URL 可覆盖）
# 用法: scripts/smoke-test.sh [BASE_URL]
set -u
BASE="${1:-http://localhost:8080}"
CURL="curl -sS -H Content-Type:application/json"
pass=0; fail=0

ok()   { echo "  [PASS] $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }

# expect_status <预期码> <实际码> <描述>
expect() {
  if [ "$1" = "$2" ]; then ok "$3 (HTTP $2)"; else bad "$3 (期望 $1 实际 $2)"; fi;
}

echo "== 0. 健康检查 =="
expect 200 "$($CURL -o /dev/null -w '%{http_code}' "$BASE/health")" "健康检查"

echo "== 1. 登记基准设备证书（追溯起点） =="
expect 201 "$($CURL -o /tmp/st_ref.json -w '%{http_code}' -X POST "$BASE/api/v1/reference-certificates" -d '{
  "cert_no":"REF-MASTER-001","device_id":"DEV-MASTER","device_name":"实验室最高标准器",
  "valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00",
  "calibrated_at":"2026-01-01T09:00:00+08:00","operator":"张工","remark":"年度基准登记"}')" "登记基准证书"

echo "== 2. 签发普通证书（引用基准） =="
expect 201 "$($CURL -o /tmp/st_c1.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-WS-001","device_id":"DEV-WS-A","device_name":"车间万用表A",
  "valid_from":"2026-03-01T00:00:00+08:00","valid_to":"2027-03-01T00:00:00+08:00",
  "calibrated_at":"2026-03-05T10:00:00+08:00","conclusion":"PASS",
  "parent_cert_no":"REF-MASTER-001","operator":"李工"}')" "签发二级证书"

expect 201 "$($CURL -o /tmp/st_c2.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-CUS-001","device_id":"DEV-CUS-B","device_name":"客户仪表B",
  "valid_from":"2026-04-01T00:00:00+08:00","valid_to":"2026-10-01T00:00:00+08:00",
  "calibrated_at":"2026-04-02T10:00:00+08:00","conclusion":"PASS",
  "parent_cert_no":"CAL-WS-001","operator":"王工"}')" "签发三级证书"

echo "== 3. 无效引用必须被拒绝 =="
expect 422 "$($CURL -o /tmp/st_e1.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-E-001","device_id":"DEV-E","valid_from":"2026-05-01T00:00:00+08:00",
  "valid_to":"2026-11-01T00:00:00+08:00","calibrated_at":"2026-05-01T09:00:00+08:00",
  "conclusion":"PASS","parent_cert_no":"NOT-EXIST"}')" "引用不存在的证书 -> 422"

expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-E-002","device_id":"DEV-E","valid_from":"2028-01-01T00:00:00+08:00",
  "valid_to":"2028-07-01T00:00:00+08:00","calibrated_at":"2028-01-02T09:00:00+08:00",
  "conclusion":"PASS","parent_cert_no":"REF-MASTER-001"}')" "校准时刻超出基准有效期 -> 422"

echo "== 4. 有效期冲突必须被拒绝 =="
expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/reference-certificates" -d '{
  "cert_no":"REF-MASTER-002","device_id":"DEV-MASTER",
  "valid_from":"2026-06-01T00:00:00+08:00","valid_to":"2026-12-01T00:00:00+08:00",
  "calibrated_at":"2026-06-01T09:00:00+08:00"}')" "同设备生效区间重叠 -> 422"

echo "== 5. 重复证书编号必须被拒绝 =="
expect 409 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/reference-certificates" -d '{
  "cert_no":"REF-MASTER-001","device_id":"DEV-OTHER",
  "valid_from":"2027-02-01T00:00:00+08:00","valid_to":"2028-02-01T00:00:00+08:00",
  "calibrated_at":"2027-02-01T09:00:00+08:00"}')" "证书编号重复 -> 409"

echo "== 6. 追溯链查询 =="
expect 200 "$($CURL -o /tmp/st_chain.json -w '%{http_code}' "$BASE/api/v1/certificates/CAL-CUS-001/chain")" "查询追溯链"
grep -q '"length":3' /tmp/st_chain.json && ok "追溯链长度为 3" || bad "追溯链长度"

echo "== 7. 撤销基准证书，级联失效下游 =="
expect 200 "$($CURL -o /tmp/st_rev.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-MASTER-001/revoke" -d '{
  "reason":"年度比对发现超差","operator":"质量负责人"}')" "撤销基准证书"
grep -q '"affected_count":2' /tmp/st_rev.json && ok "受影响证书数量 2" || bad "受影响证书数量"
grep -q CAL-WS-001 /tmp/st_rev.json && grep -q CAL-CUS-001 /tmp/st_rev.json \
  && ok "返回受影响证书及引用链" || bad "受影响证书内容"

echo "== 8. 失效证书不得再作校准依据 =="
expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-E-003","device_id":"DEV-E","valid_from":"2026-05-01T00:00:00+08:00",
  "valid_to":"2026-11-01T00:00:00+08:00","calibrated_at":"2026-05-01T09:00:00+08:00",
  "conclusion":"PASS","parent_cert_no":"CAL-WS-001"}')" "引用已失效证书 -> 422"

expect 409 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-MASTER-001/revoke" -d '{
  "reason":"重复撤销"}')" "重复撤销 -> 409"

echo "== 9. 历史内容仍可查 =="
expect 200 "$($CURL -o /tmp/st_hist.json -w '%{http_code}' "$BASE/api/v1/devices/DEV-CUS-B/certificates")" "设备历史可查"
grep -q '"status":"INVALID"' /tmp/st_hist.json && ok "历史证书状态为 INVALID 且内容保留" || bad "历史内容"

echo "== 10. 证书登记更正声明（仅展示字段） =="
expect 201 "$($CURL -o /tmp/st_cref.json -w '%{http_code}' -X POST "$BASE/api/v1/reference-certificates" -d '{
  "cert_no":"REF-CORR","device_id":"DEV-CORR","device_name":"更正前名称",
  "valid_from":"2026-02-01T00:00:00+08:00","valid_to":"2028-02-01T00:00:00+08:00",
  "calibrated_at":"2026-02-01T09:00:00+08:00","operator":"更正前操作员","remark":"更正前备注"}')" "更正演示用证书"

expect 201 "$($CURL -o /tmp/st_corr1.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-1","changes":{"device_name":"更正后名称","operator":"更正后操作员"},
  "reason":"录入笔误"}')" "登记更正声明1"
grep -q '"seq":1' /tmp/st_corr1.json && ok "声明1 seq=1" || bad "声明1 seq"

# remark 显式置空（null），未提交的 device_name/operator 保持
expect 201 "$($CURL -o /tmp/st_corr2.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-2","changes":{"remark":null},"reason":"备注误录需清空"}')" "声明2 显式置空 remark"
grep -q '"remark":null' /tmp/st_corr2.json && grep -q '"device_name":"更正后名称"' /tmp/st_corr2.json \
  && ok "显式置空生效，未提交字段保持" || bad "置空/保持合并"

# 同编号同内容重试 -> 200，返回首次声明
expect 200 "$($CURL -o /tmp/st_replay.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-2","changes":{"remark":null},"reason":"备注误录需清空"}')" "同编号同内容重试 200"
# 同编号异内容 -> 409
expect 409 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-2","changes":{"operator":"甲"},"reason":"冲突内容"}')" "同编号异内容 409"
# 字段越界 / 空修正 / 空原因 / 证书不存在
expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-X","changes":{"status":"REVOKED"},"reason":"x"}')" "越界字段 422"
expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-X","changes":{},"reason":"x"}')" "空修正 422"
expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-X","changes":{"operator":"甲"},"reason":"  "}'
)" "空原因 422"
expect 404 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/NO-SUCH-CERT/corrections" -d '{
  "statement_no":"STMT-SMOKE-X","changes":{"operator":"甲"},"reason":"x"}')" "证书不存在 404"

# 查询：声明历史 + 原证书 + 合并当前展示
expect 200 "$($CURL -o /tmp/st_corrl.json -w '%{http_code}' "$BASE/api/v1/certificates/REF-CORR/corrections")" "查询声明历史"
grep -q '"device_name":"更正前名称"' /tmp/st_corrl.json && ok "原证书签发内容保持原样" || bad "原证书内容"
grep -q '"seq":2' /tmp/st_corrl.json && ok "声明历史按顺序返回" || bad "声明历史顺序"

# 撤销后仍可更正，且状态判定不变
expect 200 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/revoke" -d '{
  "reason":"演示撤销","operator":"qa"}')" "撤销更正演示证书"
expect 201 "$($CURL -o /tmp/st_corr3.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-CORR/corrections" -d '{
  "statement_no":"STMT-SMOKE-3","changes":{"device_name":"撤销后仍可更正名称"},"reason":"撤销后补正"}')" "已撤销证书仍可更正"
grep -q '"status":"REVOKED"' /tmp/st_corr3.json && ok "更正不改变证书状态" || bad "状态保持"

echo
echo "结果: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
