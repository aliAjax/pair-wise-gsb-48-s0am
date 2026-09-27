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
- `GET /api/records`：记录列表，可带`state`和`limit`参数，每条记录附带`replenishment`缺口摘要。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/replenishment`：补券任务详情（含分批补券记录），无任务时返回`{}`。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 失败处置流程

状态机：`approved → failed → approved（补够后恢复）→ settled`；或截止日未补足时
`failed → compensation_pending → reversed（确认赔付后）`。

- `fail`（结算员）：交收失败时必须提供`fail_reason`、`responsible_seller`、`shortfall_quantity`、`due_date`（YYYY-MM-DD），同一指令只创建一条未关闭补券任务。
- `replenish`（结算员）：分批登记补券，参数`quantity`、`note`；累计补够应补数量后任务关闭、原指令恢复`approved`继续交收，补券数量不能超过当前缺口。
- `escalate_compensation`（结算员）：仅当当前日期晚于`due_date`且仍有缺口时可执行；按未补数量×交收价计算违约金（可用`penalty_amount`覆盖），指令停在`compensation_pending`。
- `confirm_compensation`（结算员）：确认赔付（`compensation_amount`不低于违约金），任务关闭为`compensated`，此后才允许`reverse`冲正。

补券任务状态：`open`（补券中）、`completed`（已补足）、`breached`（已违约待赔付）、`compensated`（已赔付）。
失败、建任务、每批补券、补足恢复、违约认定、赔付确认、冲正全部进入审计时间线。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
