# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/make-up`：补券任务总览（任务状态、责任卖方、应补/已补/缺口、截止日、违约金、各批补券）。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 失败处置流程

卖方券不足时，结算员对`approved`指令执行`fail`，记录失败原因并生成补券任务（责任卖方、应补数量、截止日），指令进入`make_up`：

- `fail`：参数`fail_reason`、`responsible_seller`、`due_date`（YYYY-MM-DD，不早于当天），可选`shortage_quantity`（默认全部应交数量）、`breach_price`、`penalty_rate`（默认日费率0.0005）。同一指令数据库层强制只保留一条未关闭任务。
- `make_up_delivery`：分批登记补券，参数`delivered_quantity`，超过缺口被拒；累计补足后任务关闭，指令回到`approved`继续交收。
- `mark_breach`：截止日过后仍有缺口方可执行，按`未补数量×价格`的未补市值乘以日费率与逾期天数计违约金，指令停在`pending_compensation`（待赔付），不可再补券、不可冲正。
- `confirm_compensation`：参数`compensation_reference`，确认赔付到账，任务关闭。
- `reverse`：待赔付指令必须先确认赔付才能冲正到`reversed`。

全部处置经过（失败、每批补券、违约认定、赔付确认、冲正）写入审计时间线。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
