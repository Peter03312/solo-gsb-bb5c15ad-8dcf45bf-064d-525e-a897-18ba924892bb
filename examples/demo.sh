#!/usr/bin/env bash
# 端到端调用示例：登记原料 -> 建批次 -> 定版 -> 上游更正 -> 复核差异
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi
}

echo "== 1. 登记两种原料（初始版本 1）=="
curl -s -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"id":"ING-MILK","name":"全脂奶粉","allergens":["milk"]}' | pretty
curl -s -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"id":"ING-PEANUT","name":"花生粉","allergens":["peanut"]}' | pretty

echo "== 2. 创建基料批次与成品批次（批次可引用批次）=="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' \
  -d '{"id":"B-BASE","product":"基料","sources":[
        {"type":"ingredient","id":"ING-MILK"},
        {"type":"ingredient","id":"ING-PEANUT"}]}' | pretty
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' \
  -d '{"id":"B-TOP","product":"成品饼干","sources":[
        {"type":"batch","id":"B-BASE"},
        {"type":"ingredient","id":"ING-MILK"}]}' | pretty

echo "== 3. 现算声明（并集 + 每项来源路径，重复来源只计一次）=="
curl -s "$BASE/batches/B-TOP/declaration" | pretty

echo "== 4. 定版：原子保存声明集合、来源版本和路径 =="
curl -s -X POST "$BASE/batches/B-TOP/finalize" | pretty

echo "== 5. 定版后来源不可改（409）=="
curl -s -X PUT "$BASE/batches/B-TOP/sources" -H 'Content-Type: application/json' \
  -d '{"sources":[{"type":"ingredient","id":"ING-MILK"}]}' | pretty

echo "== 6. 上游更正：期望版本不符被拒绝（409），版本匹配则生成新版本 =="
curl -s -X POST "$BASE/ingredients/ING-PEANUT/corrections" \
  -H 'Content-Type: application/json' \
  -d '{"allergens":["peanut","sesame"],"expected_version":9}' | pretty
curl -s -X POST "$BASE/ingredients/ING-PEANUT/corrections" \
  -H 'Content-Type: application/json' \
  -d '{"allergens":["peanut","sesame"],"expected_version":1}' | pretty

echo "== 7. 查询已定版批次：定版快照 + 现算集合 + 新增/消失差异，标记待复核 =="
curl -s "$BASE/batches/B-TOP/declaration" | pretty

echo "== 8. 下游批次按当前证据重算 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' \
  -d '{"id":"B-GIFT","product":"礼盒","sources":[{"type":"batch","id":"B-TOP"}]}' | pretty
curl -s "$BASE/batches/B-GIFT/declaration" | pretty

echo "== 9. 错误示例：空来源 / 未知来源 / 循环引用（均 422）=="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' \
  -d '{"id":"B-EMPTY","product":"x","sources":[]}' | pretty
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' \
  -d '{"id":"B-GHOST","product":"x","sources":[{"type":"ingredient","id":"NOPE"}]}' | pretty
curl -s -X PUT "$BASE/batches/B-BASE/sources" -H 'Content-Type: application/json' \
  -d '{"sources":[{"type":"batch","id":"B-GIFT"}]}' | pretty

echo "== 10. 原料修订历史（旧版本保留）=="
curl -s "$BASE/ingredients/ING-PEANUT/versions" | pretty
