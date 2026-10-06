#!/usr/bin/env bash
# 端到端调用示例：录入 -> 幂等/冲突 -> 重叠校验 -> 合规查询 -> 更正 -> 审核发布 -> 历史对照
#              -> 跨司机批量调整 -> 告警确认（创建/幂等/失效/再确认）
# 用法: BASE_URL=http://localhost:8000 ./examples/demo.sh
set -uo pipefail

BASE="${BASE_URL:-http://localhost:8000}"

req() { # req METHOD PATH [JSON]
  local method="$1" path="$2" data="${3:-}"
  echo "==> ${method} ${BASE}${path}"
  [ -n "${data}" ] && echo "    ${data}"
  local out code body
  if [ -n "${data}" ]; then
    out=$(curl -s -X "${method}" "${BASE}${path}" -H 'Content-Type: application/json' -d "${data}" -w $'\n%{http_code}')
  else
    out=$(curl -s -X "${method}" "${BASE}${path}" -w $'\n%{http_code}')
  fi
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  echo "${body}" | python3 -m json.tool 2>/dev/null || echo "${body}"
  echo "HTTP ${code}"
  echo
}

echo "########## 0. 健康检查"
req GET /health

echo "########## 1. 录入司机（重复提交同内容 -> 幂等 200；异内容 -> 409）"
req POST /drivers '{"driver_id":"DRV-1001","name":"张三"}'
req POST /drivers '{"driver_id":"DRV-1001","name":"张三"}'
req POST /drivers '{"driver_id":"DRV-1001","name":"李四"}'

echo "########## 2. 录入片段：驾驶 08:00-10:00（重试幂等 / 异内容冲突 / 非正时长拒绝）"
req POST /segments '{"segment_id":"SEG-A","driver_id":"DRV-1001","start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T10:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-A","driver_id":"DRV-1001","start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T10:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-A","driver_id":"DRV-1001","start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T11:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-BAD","driver_id":"DRV-1001","start_utc":"2026-10-05T10:00:00Z","end_utc":"2026-10-05T09:00:00Z","type":"REST"}'

echo "########## 3. 重叠非法（409），相接合法（201）"
req POST /segments '{"segment_id":"SEG-X","driver_id":"DRV-1001","start_utc":"2026-10-05T09:00:00Z","end_utc":"2026-10-05T11:00:00Z","type":"OTHER_DUTY"}'
req POST /segments '{"segment_id":"SEG-E","driver_id":"DRV-1001","start_utc":"2026-10-05T07:30:00Z","end_utc":"2026-10-05T08:00:00Z","type":"REST"}'

echo "########## 4. 休息 20 分钟（不足 30 分钟，不清零）+ 继续驾驶 -> 累计驾驶越限"
req POST /segments '{"segment_id":"SEG-B","driver_id":"DRV-1001","start_utc":"2026-10-05T10:00:00Z","end_utc":"2026-10-05T10:20:00Z","type":"REST"}'
req POST /segments '{"segment_id":"SEG-C","driver_id":"DRV-1001","start_utc":"2026-10-05T10:20:00Z","end_utc":"2026-10-05T12:30:00Z","type":"DRIVING"}'
req GET /drivers/DRV-1001/compliance
# 期望：CONTINUOUS_DRIVING_LIMIT，首次越限 2026-10-05T12:20:00Z，参与片段 SEG-A、SEG-C

echo "########## 5. 按预期修订号更正：休息延长到 40 分钟（合格休息，清零重计）"
req PATCH /segments/SEG-B '{"expected_revision":1,"end_utc":"2026-10-05T10:40:00Z"}'
req PATCH /segments/SEG-C '{"expected_revision":1,"start_utc":"2026-10-05T10:40:00Z"}'
echo "---- 用过期的修订号再次更正 -> 409"
req PATCH /segments/SEG-C '{"expected_revision":1,"start_utc":"2026-10-05T10:45:00Z"}'
req GET /drivers/DRV-1001/compliance
# 期望：compliant=true（更正后按现存记录重算）

echo "########## 6. 补录其他值勤 13:00-19:30 -> 滚动 24h 值勤合计 10h20m 越限"
req POST /segments '{"segment_id":"SEG-D","driver_id":"DRV-1001","start_utc":"2026-10-05T13:00:00Z","end_utc":"2026-10-05T19:30:00Z","type":"OTHER_DUTY"}'
req GET /drivers/DRV-1001/compliance
# 期望：ROLLING_24H_DUTY_LIMIT，首次越限 2026-10-05T19:10:00Z，参与片段 SEG-A、SEG-C、SEG-D

echo "########## 7. 审核发布：固化当前输入修订与结论；同修订重复发布只留一版"
req POST /drivers/DRV-1001/audits
req POST /drivers/DRV-1001/audits

echo "########## 8. 更正 SEG-D 缩短到 19:00（旧版本不变），重算后合规"
req PATCH /segments/SEG-D '{"expected_revision":1,"end_utc":"2026-10-05T19:00:00Z"}'
req GET /drivers/DRV-1001/compliance

echo "########## 9. 再次发布产生新版本；列表查看历史版本"
req POST /drivers/DRV-1001/audits
req GET /drivers/DRV-1001/audits

echo "########## 10. 对照历史版本 1 与当前结果"
req GET /drivers/DRV-1001/audits/1/compare

echo "########## 11. 跨司机批量调整：录入第二名司机与两条相接片段（不同司机、同一天空闲日）"
req POST /drivers '{"driver_id":"DRV-2002","name":"李四"}'
req POST /segments '{"segment_id":"SEG-SW1","driver_id":"DRV-1001","start_utc":"2026-10-07T08:00:00Z","end_utc":"2026-10-07T10:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-SW2","driver_id":"DRV-2002","start_utc":"2026-10-07T10:00:00Z","end_utc":"2026-10-07T12:00:00Z","type":"DRIVING"}'

echo "########## 12. 一批内互换两条相接片段（按最终排布检查，与提交顺序无关；两条修订号各 +1）"
req POST /reassignment-batches '{
  "batch_id":"BATCH-SWAP-01",
  "items":[
    {"segment_id":"SEG-SW1","expected_revision":1,"driver_id":"DRV-2002",
     "start_utc":"2026-10-07T08:00:00Z","end_utc":"2026-10-07T10:00:00Z","type":"DRIVING"},
    {"segment_id":"SEG-SW2","expected_revision":1,"driver_id":"DRV-1001",
     "start_utc":"2026-10-07T10:00:00Z","end_utc":"2026-10-07T12:00:00Z","type":"DRIVING"}
  ]
}'
# 期望：201，affected_drivers 两名司机调整前后合规结论，segment_changes 两条均 1 -> 2

echo "########## 13. 同批次号同内容重试 -> 200 replayed=true，修订号不再增加"
req POST /reassignment-batches '{
  "batch_id":"BATCH-SWAP-01",
  "items":[
    {"segment_id":"SEG-SW1","expected_revision":1,"driver_id":"DRV-2002",
     "start_utc":"2026-10-07T08:00:00Z","end_utc":"2026-10-07T10:00:00Z","type":"DRIVING"},
    {"segment_id":"SEG-SW2","expected_revision":1,"driver_id":"DRV-1001",
     "start_utc":"2026-10-07T10:00:00Z","end_utc":"2026-10-07T12:00:00Z","type":"DRIVING"}
  ]
}'

echo "########## 14. 同批次号异内容 -> 409 batch_content_conflict"
req POST /reassignment-batches '{
  "batch_id":"BATCH-SWAP-01",
  "items":[
    {"segment_id":"SEG-SW1","expected_revision":2,"driver_id":"DRV-2002",
     "start_utc":"2026-10-07T09:00:00Z","end_utc":"2026-10-07T10:00:00Z","type":"DRIVING"}
  ]
}'

echo "########## 15. 最终排布重叠 -> 409 segment_overlap，整批拒绝不改任何记录"
req POST /reassignment-batches '{
  "batch_id":"BATCH-BAD-01",
  "items":[
    {"segment_id":"SEG-SW1","expected_revision":2,"driver_id":"DRV-1001",
     "start_utc":"2026-10-05T09:00:00Z","end_utc":"2026-10-05T09:30:00Z","type":"DRIVING"}
  ]
}'
# 期望：segment_overlap（与 DRV-1001 当天 08:00-10:00 的 SEG-A 重叠；相接合法、重叠非法）

echo "########## 16. 修订号过期 -> 409 revision_conflict（整批拒绝；SEG-SW2 当前已是修订号 2）"
req POST /reassignment-batches '{
  "batch_id":"BATCH-STALE-01",
  "items":[
    {"segment_id":"SEG-SW2","expected_revision":1,"driver_id":"DRV-1001",
     "start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T09:00:00Z","type":"DRIVING"}
  ]
}'

echo "########## 17. 查询批次首次提交结果"
req GET /reassignment-batches/BATCH-SWAP-01

echo "########## 18. 告警确认：构造违规（DRV-3003 驾驶 08-12 + 12-13，累计 5h 越限于 12:00）"
req POST /drivers '{"driver_id":"DRV-3003","name":"王五"}'
req POST /segments '{"segment_id":"SEG-K1","driver_id":"DRV-3003","start_utc":"2026-10-08T08:00:00Z","end_utc":"2026-10-08T12:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-K2","driver_id":"DRV-3003","start_utc":"2026-10-08T12:00:00Z","end_utc":"2026-10-08T13:00:00Z","type":"DRIVING"}'
req GET /drivers/DRV-3003/compliance
# 期望：CONTINUOUS_DRIVING_LIMIT，首次越限 2026-10-08T12:00:00Z，参与片段 SEG-K1、SEG-K2

echo "########## 19. 创建告警确认（201 created_new=true，固化覆盖的片段修订集合）"
req POST /drivers/DRV-3003/acknowledgments '{"rule":"CONTINUOUS_DRIVING_LIMIT","note":"已电话告知司机，责令整改"}'
echo "---- 同司机同规则且修订集合未变：重复提交返回原记录（200 created_new=false），备注不同也不新增"
req POST /drivers/DRV-3003/acknowledgments '{"rule":"CONTINUOUS_DRIVING_LIMIT","note":"换个备注也不会新增"}'
echo "---- 当前不存在的违规 -> 409 violation_not_found；司机不存在 -> 404 driver_not_found"
req POST /drivers/DRV-3003/acknowledgments '{"rule":"ROLLING_24H_DUTY_LIMIT","note":"当前并无该违规"}'
req POST /drivers/DRV-9999/acknowledgments '{"rule":"CONTINUOUS_DRIVING_LIMIT","note":"司机不存在"}'

echo "########## 20. 任一片段增删或修订后原确认失效：更正 SEG-K2 -> 查询返回失效原因"
req PATCH /segments/SEG-K2 '{"expected_revision":1,"end_utc":"2026-10-08T12:30:00Z"}'
req GET /drivers/DRV-3003/acknowledgments
# 期望：status=INVALIDATED，invalidation.reason 指明 SEG-K2 修订号 1→2

echo "########## 21. 修订集合已变且违规仍存在 -> 可再次提交生成新记录；旧记录保持失效可查"
req GET /drivers/DRV-3003/compliance
req POST /drivers/DRV-3003/acknowledgments '{"rule":"CONTINUOUS_DRIVING_LIMIT","note":"修订后再次确认"}'
req GET /drivers/DRV-3003/acknowledgments
req GET '/drivers/DRV-3003/acknowledgments?status=ACTIVE'
# 期望：列表两条记录（#1 INVALIDATED 附失效原因，#2 ACTIVE）；过滤后只剩 #2
