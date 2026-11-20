# 电商客服接入指南

商城同源网关在 `/support/` 提供知汇客服。顾客可以查商品、本人订单和物流。发货提醒与退款申请会先显示订单和待提交内容，用户确认后才写入电商库。商品说明、故障排查和退款政策继续走知识问答。

## 配置两个服务

在电商服务进程中设置 `DATABASE_URL` 和 `COMMERCE_SERVICE_TOKEN`。从电商项目根目录运行 `uv run uvicorn app.app:app --host 127.0.0.1 --port 18081 --no-access-log`。跨主机部署时改用受控的内网监听地址。已有接口调用方需同步启用服务鉴权，个人业务请求需附带经过核验的 `X-Commerce-User-Id`。

| 查询服务配置 | 用途 |
| --- | --- |
| `COMMERCE_ENABLED` | 默认关闭。设为 `true` 后开放电商查询 |
| `COMMERCE_ACTIONS_ENABLED` | 默认关闭。验证查询和订单归属后设为 `true` 以开放申请提交 |
| `COMMERCE_ACCOUNT_LOGIN_ENABLED` | 默认关闭。使用本项目电商后端签发会话时设为 `true` |
| `COMMERCE_API_BASE_URL` | 电商服务的内部地址，例如同机试点时的 `http://127.0.0.1:18081` |
| `COMMERCE_SERVICE_TOKEN` | 与电商服务配置一致的服务令牌 |
| `MALL_SESSION_VERIFY_URL` | 商城内部身份核验接口的完整 URL |
| `MALL_SESSION_VERIFY_TOKEN` | 商城身份核验接口的服务令牌，独立于电商服务令牌 |
| `MALL_SESSION_COOKIE_NAME` | 可选。商城会话 Cookie 名称，默认 `mall_session` |
| `COMMERCE_PUBLIC_ORIGIN` | 浏览器访问商城的源，包含协议和主机，有非默认端口时包含端口，例如 `https://mall.example.test` |

商城提供 `POST /internal/auth/customer-session/verify`。客服转发 `Cookie: mall_session=…`，并通过 `Authorization: Bearer …` 发出内部核验请求。有效会话返回 HTTP 200 和 `{"user_id":"customer-a","expires_at":"2030-01-01T00:00:00Z"}`。无效或过期会话返回 HTTP 401，服务令牌错误返回 HTTP 403。商城负责把账号映射为订单库里的 `users.user_id`。生产 Cookie 使用 `Secure` 和 `HttpOnly`，其路径需覆盖 `/support/`。

### 使用电商后端的账号登录

在电商后端设置 `MALL_SESSION_VERIFY_TOKEN`。在知汇使用相同的会话令牌，并另外设置业务令牌。确认订单库的 `users` 表已有待开通的用户，再从电商后端项目根目录运行以下命令。将示例用户 ID 替换为实际用户 ID。

```powershell
uv run python -m app.customer_auth init
uv run python -m app.customer_auth set-password 'customer-a'
```

命令会提示输入 15 至 128 个字符的密码。设置密码会撤销该账号先前的会话。

本机联调时，在知汇设置以下值。把业务令牌和会话令牌分别填入同名配置项。启动两个服务后，通过 `http://127.0.0.1:8001/` 打开知汇，在商城工作台登录。

```dotenv
COMMERCE_ENABLED=true
COMMERCE_ACCOUNT_LOGIN_ENABLED=true
COMMERCE_API_BASE_URL=http://127.0.0.1:18081
MALL_SESSION_VERIFY_URL=http://127.0.0.1:18081/internal/auth/customer-session/verify
COMMERCE_PUBLIC_ORIGIN=http://127.0.0.1:8001
```

正式环境通过 HTTPS 提供客服。两个服务部署在不同主机时，会话接口也使用 HTTPS。

### 挂载到商城

启动查询服务时为商城挂载设置 `--root-path /support --no-access-log`。网关将 `/support/`、客服静态资源、查询及会话接口、`/api/commerce/` 和已有会话反馈接口转发给查询服务，转发时去掉 `/support` 前缀。SSE 路由关闭网关缓冲。导入、审批及运维接口由原内部入口提供。提交请求校验浏览器的 `Origin`。

## 业务边界和运维

- 商品需要商品 ID。登录后可从最近购买的商品中选择。订单列表展示最近 5 条，历史订单可通过订单号查询。
- 发货提醒受订单状态限制。退款操作登记申请，回执显示申请号和状态。商城确认订单库与申请处理入口后再开放正式写入。
- 首次确认在 5 分钟内有效。已确认的请求若结果未知，可在 24 小时内用同一张卡重试，客服用原幂等键交给电商服务。恢复卡片的 GET 请求只读取状态。
- 启用账号登录时，顾客可在工作台登录或退出。会话在 8 小时后过期。退出后，旧会话立即失效。使用商城已有登录入口时，顾客从商城登录。知识问答继续使用原会话历史和引用。

## 选中对象和续办任务

登录后，在商城工作台选择一条订单或商品。接着可以问「物流到哪了」或「这个商品多少钱」。输入带编号的问题也可以查询指定订单。查询订单时，系统会核验商城账号和订单归属。

也可以分步办理。先说「我要退款」，下一条消息只发订单号，再填写退款原因。收到补充提示时，可以用「原因：」开头重发。系统会展示原订单和原因，等待用户确认。

办理途中可以查政策、切换业务或暂停。每个会话最多保留一项当前任务和一项暂停任务。可以在工作台继续或取消任务，也可以发送「继续上一个任务」或「取消当前任务」。取消未提交的确认后，同一确认编号的提交请求会被拒绝。

系统按商城账号、浏览器归属和会话 ID 保存任务。草稿在上次状态更新 15 分钟后过期。已确认且结果未知的请求可用原确认卡在 24 小时内重试。

## 客服接口

以下路径相对于查询服务。挂载于商城时，在路径前加 `/support`。

- `GET /api/commerce/session` 返回 `enabled`、`actions_enabled` 和 `authenticated`。页面请求 `include_scope=true` 时可取得用于识别账号切换的 `account_scope`。
- 启用账号登录时，`GET /api/commerce/session` 还返回 `account_login_enabled`。工作台向 `POST /api/commerce/login` 提交 `user_id` 和 `password`。`POST /api/commerce/logout` 撤销会话并清除登录 Cookie。两个接口只在 JSON 响应中返回 `authenticated`。
- `POST /api/commerce/assist` 接收 `query` 或 `operation`，可附加目标 `order_id` 或 `product_id`。页面将 `knowledge` 交给现有问答接口，将 `result`、`choose`、`operation`、`policy_choice` 和 `confirmation` 显示为业务卡片。`task_control` 返回取消或续办提示。
- `GET /api/commerce/objects` 返回本人的近期 `orders` 和 `products`。`GET /api/commerce/task?session_id=…` 返回当前 `active` 和暂停的 `paused`。在查询和确认预览中附带 `session_id` 可使用多轮任务。`expect_task` 表示当前文本意在续办，任务失效时会提示重新选择。
- `POST /api/commerce/confirmations` 接收 `operation`、`order_id` 和备注 `note` 或退款原因 `reason`。响应包含 `confirmation_id`、`status`、`expires_at` 和预览内容。确认完成后，`result` 包含商城申请信息。
- `GET /api/commerce/confirmations/{id}` 恢复卡片，`POST /api/commerce/confirmations/{id}/submit` 提交已核对的请求。浏览器恢复页面时读取卡片，重试由用户点击触发。
- `POST /api/commerce/confirmations/{id}/cancel` 取消未提交的确认。再次提交该确认返回 `409 confirmation_cancelled`。

错误格式为 `{"code":"…","message":"…"}`。身份缺失或过期使用 `401 commerce_login_required`，来源不符使用 `403 commerce_origin_forbidden`。资源缺失或越权使用 `404 commerce_not_found`，本人订单暂缺物流使用 `404 logistics_unavailable`。

账号登录失败使用 `401 commerce_login_failed`。

订单不适合催发货时使用 `400 shipping_ineligible`，已有退款或幂等键冲突使用 `409 refund_in_progress` 或 `409 idempotency_conflict`。确认失效使用 `410 confirmation_expired`，输入校验失败使用 `422 validation_error`。上游异常响应、服务不可用及超时分别使用 `502 commerce_upstream_error`、`503 commerce_unavailable`、`504 commerce_timeout`。提交是否已写入无法判定时使用 `503 commerce_action_unknown`。

## 验收

用两个商城测试账号分别查询本人近期订单、本人旧订单和对方订单。验证匿名商品查询、无物流、空列表和金额精度。确认订单归属失败与资源缺失使用相同的错误。

从商城面板选择订单和商品，再用指代式问题查询。分步输入退款订单号和原因，插入一项物流查询，再续办退款。刷新页面后应恢复草稿。切换账号或浏览器后应隐藏原草稿。取消确认后应拒绝旧卡提交。

在隔离 MySQL 校验 `shipping_urge_requests.urge_id` 与 `refund_requests.refund_id` 唯一索引。并发提交同一张催发货确认卡应只生成一条记录。两张不同退款确认卡并发提交时，订单只应新增一个进行中的退款申请。模拟订单库已提交但回执丢失，原卡重试应返回原请求号。

在商城路径打开客服页，验证页面资源、查询和 SSE 使用同一前缀。验证 RAG 流式、非流式回答及历史恢复。试点先启用查询，再启用提交。正式验收使用真实商城身份和业务方可核对的数据库回执。

查询服务可运行 `uv run pytest -q` 和 `node --test tests/frontend/*.test.cjs`。电商服务可运行 `uv run python -m unittest discover -s tests`。执行 MySQL 并发测试时，将 `COMMERCE_TEST_MYSQL_URL` 指向专用空测试库。
