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
| POST | `/batches/{id}/replace/preview` | 替换预演（不改批次）：禁过敏原 + 各位置候选，求替换位置数最少的可行方案 |
| POST | `/batches/{id}/replace/adopt` | 携预演依据采纳：同事务校验证据未变后整体替换直接来源 |

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

## 本轮用法：过敏原替换预演与采纳

配方员为**未定版批次**指定禁止出现的过敏原、可替换的直接来源序号（从 0 开始）
及各位置候选来源。先预演、后采纳，两步配套：

```bash
# 准备一个无过敏原的候选原料
curl -X POST localhost:8000/ingredients -H 'Content-Type: application/json' \
  -d '{"id":"ING-RICE","name":"米粉","allergens":[]}'

# 批次 B-1 直接来源为 [ING-MILK(seq0), ING-PEANUT(seq1)]
# 1) 预演：禁止 peanut；给出每个可替换位置的候选（未列出的位置保持不变）
curl -X POST localhost:8000/batches/B-1/replace/preview \
  -H 'Content-Type: application/json' \
  -d '{"forbidden_allergens":["peanut"],
       "replacements":[
         {"seq":0,"candidates":[{"type":"ingredient","id":"ING-RICE"}]},
         {"seq":1,"candidates":[{"type":"ingredient","id":"ING-RICE"}]}]}'

# 2) 采纳：原样回传预演返回的 preview_token
curl -X POST localhost:8000/batches/B-1/replace/adopt \
  -H 'Content-Type: application/json' \
  -d '{"preview_token":"<上一步 basis.preview_token>"}'
```

预演成功响应（`feasible=true`）示例：

```json
{
  "feasible": true,
  "batch_id": "B-1",
  "replaced_positions": [1],
  "replacement_count": 1,
  "forbidden_allergens": ["peanut"],
  "sources": [
    {"type": "ingredient", "id": "ING-MILK"},
    {"type": "ingredient", "id": "ING-RICE"}
  ],
  "declaration": {
    "allergens": ["milk"],
    "paths": {"milk": ["B-1 -> ING-MILK"]},
    "source_versions": {"ING-MILK": 1, "ING-RICE": 1}
  },
  "basis": {
    "preview_token": "038810a3...",
    "batch_id": "B-1",
    "forbidden_allergens": ["peanut"],
    "fingerprint": "432622db..."
  }
}
```

无可行组合时 HTTP 200 且 `feasible=false`（不返回 token、不改动批次）。
完整可运行脚本见 `examples/demo.sh`（第 9–12 节）。

### 预演与采纳语义

- **预演不改动批次**：在假设的直接来源集合上现算声明，按替换位置数**最少**选方案；
  原配方已合格时返回零替换（`replacement_count=0`）。并列时依次按
  **来源序号 → 候选类型（`batch` 先于 `ingredient`）→ 候选 ID** 字典序选首个。
- **返回内容**：替换后的直接来源、声明（过敏原、每项来源路径、原料当前版本），
  以及一次性采纳依据 `basis.preview_token` 与证据指纹。
- **硬性报错（422）**：非法序号（越界）、重复序号、空替换指令/空候选、
  未知候选来源、候选会引入循环（自引用或沿现有来源回到目标批次）。
- **采纳是同一事务内的条件写入**：先重算证据指纹，确认目标批次状态与直接来源、
  参与预演的每条来源关系（含路径上的子批次边）、每个可达原料版本均未变化，
  然后才整体替换直接来源并消费依据；任一变化即 **409 拒绝且批次不变**。
  依据未知返回 404，依据归属其他批次返回 422，依据一次性有效。
- **已定版批次不得预演或采纳**（409），定版快照、批次与声明既有接口保持兼容。

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
- **并发一致性**：所有请求在单连接 + 全局锁下执行，写操作使用 `BEGIN IMMEDIATE` 事务。原料更正的"校验期望版本 → 追加新版本"与定版的"计算声明 → 落库"各自是不可分割的事务，并发更正与定版不会拼接不同提交状态。替换采纳的"重算证据指纹 → 比对依据 → 整体替换来源 → 消费依据"同样是一个不可分割的事务，预演与采纳之间的任何相关变化都会使依据过期。
