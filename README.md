# 批次过敏原声明追溯服务

面向食品加工厂的批次过敏原声明追溯 HTTP API。原料记录过敏原集合与修订版本；生产批次引用原料或其他批次形成来源图；按全部可达原料的**当前版本**计算过敏原并集及每项来源路径；批次可定版，定版快照不可变，上游更正后自动给出差异并标记待复核。

技术栈：Python 3.12 · FastAPI · SQLite（WAL）。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**，交互式文档：http://localhost:8000/docs
首次启动即自动完成 SQLite 初始化（建库建表），数据持久化在命名卷 `allergen-data`。

## 本地启动

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置（环境变量）

| 变量      | 默认值           | 说明                     |
|-----------|------------------|--------------------------|
| `DB_PATH` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |
| `PORT`    | `8000`           | 容器内监听端口           |

## 运行测试与示例

```bash
pip install -r requirements-dev.txt
pytest -q                 # 接口测试
./examples/demo.sh        # 端到端 curl 示例（需服务已启动）
```

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/ingredients` | 登记原料，初始版本 1 |
| GET  | `/ingredients` / `/ingredients/{id}` | 查询原料（当前版本） |
| GET  | `/ingredients/{id}/versions` | 完整修订历史（旧版本保留） |
| POST | `/ingredients/{id}/corrections` | 更正过敏原，须携带 `expected_version`；版本不符返回 409，历史保留 |
| POST | `/batches` | 创建未定版批次；空来源 / 未知来源 / 循环引用返回 422 |
| GET  | `/batches/{id}` | 批次信息与来源 |
| PUT  | `/batches/{id}/sources` | 整体替换来源（仅未定版；已定版返回 409） |
| GET  | `/batches/{id}/declaration` | 未定版：现算声明；已定版：定版快照 + 现算集合 + 差异 + 复核状态 |
| POST | `/batches/{id}/finalize` | 定版：原子保存声明集合、来源版本和路径 |

## 调用示例

```bash
# 登记原料
curl -X POST localhost:8000/ingredients -H 'Content-Type: application/json' \
  -d '{"id":"ING-MILK","name":"全脂奶粉","allergens":["milk"]}'

# 更正（携带期望版本）
curl -X POST localhost:8000/ingredients/ING-MILK/corrections \
  -H 'Content-Type: application/json' \
  -d '{"allergens":["milk","lactose"],"expected_version":1}'

# 建批次（来源可为原料或其他批次）
curl -X POST localhost:8000/batches -H 'Content-Type: application/json' \
  -d '{"id":"B-1","product":"饼干","sources":[{"type":"ingredient","id":"ING-MILK"}]}'

# 现算声明 / 定版 / 定版后查询（含差异与复核状态）
curl localhost:8000/batches/B-1/declaration
curl -X POST localhost:8000/batches/B-1/finalize
curl localhost:8000/batches/B-1/declaration
```

声明响应示例（已定版且上游发生更正）：

```json
{
  "batch_id": "B-1",
  "status": "finalized",
  "review_status": "pending_review",
  "finalized": {"allergens": ["milk"], "source_versions": {"ING-MILK": 1},
                "paths": {"milk": ["B-1 -> ING-MILK"]}, "finalized_at": "..."},
  "current":  {"allergens": ["lactose", "milk"], "source_versions": {"ING-MILK": 2},
                "paths": {"...": ["..."]}},
  "diff": {"added": ["lactose"], "removed": []}
}
```

## 关键语义

- **声明计算**：沿来源图遍历全部可达原料，按其**当前版本**取过敏原并集；每个过敏原附来源路径（`批次 -> 批次 -> 原料`），路径与来源版本均去重，重复来源只计一次。
- **原料更正**：请求必须携带 `expected_version`，与当前版本不符即拒绝（409），不写入任何内容；所有历史版本可通过 `/versions` 查询。
- **批次来源**：仅未定版批次可整体替换来源；空来源、未知来源、直接或间接循环引用均报错（422）。
- **定版**：在单个事务内原子保存声明集合、来源版本与路径；此后该批次来源与声明不可改（409）。
- **上游更正后**：查询已定版批次会同时返回定版快照与按当前证据现算的集合，并给出相对定版**新增 / 消失**的过敏原；两者不同则 `review_status = pending_review`。下游批次（无论是否定版）同样按当前证据重算。
- **并发一致性**：所有请求在单连接 + 全局锁下执行，写操作使用 `BEGIN IMMEDIATE` 事务。原料更正的"校验期望版本 → 追加新版本"与定版的"计算声明 → 落库"各自是不可分割的事务，并发更正与定版不会拼接不同提交状态。
