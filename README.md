# Xianyu Bridge

基于原 `XianYuApis` 的闲鱼协议能力，补成供 `laoxiangji server` 调用的独立 Sidecar。Python 负责登录态、WebSocket、闲鱼 HTTP 协议和事件可靠投递；Java 只负责业务编排与订单映射。

> 非闲鱼官方接口，协议可能变化。写操作必须保留人工兜底；严禁用于违法、骚扰、批量营销或绕过平台风控。

## 当前能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 多闲鱼账号扫码登录、Cookie 复用 | 已实现 | 每账号独立 Cookie、Session 和 WebSocket |
| WebSocket 私信监听、断线重连 | 已实现 | 结构化为统一事件，不在 Python 内自动回复 |
| 文字/图片消息解析 | 已实现 | 未知卡片也保留原始载荷 |
| 主动创建会话、发送文字 | 已实现 | 内部 API 调用 |
| 卖家订单列表、订单详情 | 已实现 | 只读请求允许一次 token 刷新重试 |
| 订单状态轮询补偿 | 已实现 | 防止 WebSocket 漏事件，默认 60 秒 |
| 虚拟发货、取消待付款订单、商品下架 | 已实现 | 写请求绝不自动重放；发货/取消先查状态 |
| 事件持久化和 Java 回调 | 已实现 | SQLite Outbox、事件 ID 去重、指数退避 |
| 双向 HMAC 鉴权 | 已实现 | 5 分钟时间窗和 nonce 防重放 |
| 改价、卖家主动退款 | 明确不支持 | 协议尚未验证，接口返回 HTTP 501，不伪造成功 |

本仓库没有复制 `xianyu-auto-reply` 的 AGPL 源码；只参考其产品能力边界，在原项目上独立实现所需桥接能力。

## 运行

要求 Python 3.11+、Node.js 22+。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
set -a; source .env; set +a
.venv/bin/python run_live.py
```

首次运行没有账号时会生成 `login_qr.png` 并自动进入扫码。使用闲鱼 App 扫码后，Cookie 按账号保存到 `data/accounts/{accountId}.cookies`。二维码、Cookie 目录和 `.env` 都已加入 `.gitignore`；历史 `.cookies_str` 会兼容导入一次，但不会覆盖后来刷新的 Cookie。

### 管理多个账号

服务运行前或运行中都可以添加账号：

```bash
.venv/bin/python account_cli.py login
```

每执行一次登录一个账号。服务默认每 5 秒发现新增或刷新的 Cookie，并为该账号独立启动 WebSocket，无需重启。

```bash
# 只显示账号 ID，不显示 Cookie
.venv/bin/python account_cli.py list

# 移除账号；运行中的对应连接会自动停止
.venv/bin/python account_cli.py remove 账号ID
```

同一账号重新执行 `login` 会安全替换其 Cookie 并重连，不影响其他账号。

只验证 HTTP 服务、暂不登录闲鱼时：

```bash
XIANYU_INTERNAL_SECRET=test-secret .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8090
curl http://127.0.0.1:8090/health
```

Docker 内监听需在 `.env` 设置 `XIANYU_HOST=0.0.0.0`：

```bash
docker build -t xianyu-bridge .
docker run -it --name xianyu-bridge --env-file .env -p 127.0.0.1:8090:8090 \
  -v xianyu-data:/app/data xianyu-bridge

# 容器运行中添加另一个账号
docker exec -it xianyu-bridge python account_cli.py login
```

## 配置

以 [.env.example](.env.example) 为准：

- `XIANYU_INTERNAL_SECRET`：Java 调 Python 的 HMAC 密钥；未配置时 `/internal/**` 返回 503。
- `LXJ_EVENT_CALLBACK_URL`、`LXJ_EVENT_CALLBACK_SECRET`：Python 回调 Java；两项同时存在才发送。
- `XIANYU_OUTBOX_PATH`：事件 SQLite 文件。
- `XIANYU_ACCOUNTS_DIR`：多账号 Cookie 目录，默认 `./data/accounts`。
- `XIANYU_ACCOUNT_SYNC_INTERVAL`：新增、刷新、移除账号的发现间隔，默认 5 秒。
- `XIANYU_ORDER_POLL_INTERVAL`：订单补偿轮询秒数，最小 5 秒。

订单补偿会把每个订单最后观察到的状态保存在 `XIANYU_OUTBOX_PATH` 指向的
SQLite 数据库 `order_sync_state` 表中。首次发现的历史终态订单只建立基线；
活跃订单及后续状态变化才写入 `event_outbox` 并回调。每轮轮询只输出一条汇总日志。

不要把内部接口直接暴露到公网。建议只监听 `127.0.0.1`，或置于仅 Java 可访问的容器网络。

## 内部 API

账号业务接口前缀为 `/internal/accounts/{accountId}`，账号列表是 `/internal/accounts`：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/internal/accounts` | 列出已配置账号及连接状态 |
| GET | `/status` | 连接状态 |
| POST | `/messages` | 给已有会话发文字 |
| POST | `/chats` | 创建商品会话 |
| GET | `/orders` | 卖家订单列表 |
| GET | `/orders/{orderId}` | 订单详情和稳定状态摘要 |
| POST | `/orders/{orderId}/deliver` | 虚拟发货，可随后发取餐码 |
| POST | `/orders/{orderId}/cancel` | 卖家关闭待付款订单；请求体 `{}` |
| POST | `/items/{itemId}/offline` | 商品下架 |
| POST | `/orders/{orderId}/price` | 当前固定返回 501 |
| POST | `/orders/{orderId}/refund` | 当前固定返回 501 |

健康检查 `/health` 不需要签名，其余接口都需要：

```text
X-Xianyu-Timestamp: Unix 秒
X-Xianyu-Nonce: 每次请求唯一随机值
X-Xianyu-Signature: hex(HMAC-SHA256(secret, canonical))

canonical = timestamp + "\n" + nonce + "\n" + METHOD + "\n" + path + "\n" + sha256(rawBody)
```

签名实现见 `xianyu_bridge/signature.py`。签名必须基于实际发送的原始 JSON 字节，不能把对象再次序列化。

发货请求示例：

```json
{
  "itemId": "闲鱼商品ID",
  "buyerId": "买家ID",
  "chatId": "会话ID",
  "pickupCode": "老乡鸡取餐码"
}
```

如果闲鱼发货成功但取餐码发送失败，接口返回 502；Java 可安全重试：第二次会识别为已发货，只补发消息，不重复发货。

## Python -> Java 事件

回调体为：

```json
{
  "event_id": "account:message-or-order-version",
  "event_type": "CHAT_MESSAGE|ORDER_CREATED|ORDER_PAID|ORDER_UPDATED|ORDER_CLOSED|ORDER_REFUNDED",
  "account_id": "闲鱼账号ID",
  "occurred_at": 1787462400000,
  "chat_id": "会话ID",
  "message_id": "消息ID",
  "buyer_id": "买家ID",
  "buyer_name": "昵称",
  "item_id": "商品ID",
  "order_id": "闲鱼订单ID",
  "paid_amount": "可信实付金额；无法安全识别时为 null",
  "content_type": "text|image|card|order_status",
  "content": "消息内容或状态",
  "raw_payload": {}
}
```

回调使用同一套 HMAC 规则，并额外携带 `X-Xianyu-Event-Id`。Java 必须以该 ID 建唯一索引后再处理，以实现消费幂等。HTTP 成功但业务响应 `code` 非 `0/200` 时，Python 仍视为失败并重试。

## Java 侧适配边界

`laoxiangji server` 建议只新增四层：

1. `XianyuBridgeClient`：签名调用上述内部 API，设置连接/读取超时。
2. `XianyuEventController`：校验回调签名和 nonce，仅接收入库，不在 HTTP 线程下单。
3. `XianyuEventInbox`：以 `event_id` 唯一约束，异步消费并关联本系统订单。
4. 业务编排：`ORDER_PAID -> 老乡鸡下单 -> 获取取餐码 -> 闲鱼发货并发码`；失败进入可人工重试状态。

订单映射至少保存 `xianyu_account_id / xianyu_order_id / xianyu_item_id / buyer_id / chat_id / lxj_order_id / event_id / status / last_error`。支付、取消、退款和发货都不能由通用 HTTP 重试器自动重放。

Java 调用任何订单或消息接口时都必须显式选择 `accountId`，不要设置全局默认闲鱼账号。

消息与发货接口支持 `idempotencyKey`。Java 应使用稳定业务键（例如 `meal-link:{mealOrderId}`、
`meal-pickup:{mealOrderId}`）；Sidecar 会按“账号 + 业务键”生成稳定消息 UUID，避免重试重复发消息。
`/health` 的 `capabilities` 是实际能力开关；当前 `changePrice` 和 `refund` 固定为 `false`，Java 不应自动调用。

## 项目结构

```text
app.py                       FastAPI 入口
run_live.py                  扫码登录 + WebSocket + HTTP 服务启动器
account_cli.py               多账号登录、列表和移除命令
goofish_live.py              WebSocket 协议与消息发送
goofish_apis.py              原有闲鱼 HTTP 基础能力
xianyu_bridge/
  api.py                     HMAC 内部 API
  accounts.py                多账号 Cookie 仓库
  account_login.py           扫码登录与账号保存
  parser.py                  结构化消息解析
  platform.py                卖家订单与写操作
  order_poll.py              订单轮询补偿
  outbox.py / callback.py    可靠事件回调
  manager.py / runtime.py    账号连接与生命周期
tests/                       离线单元测试，不触发真实闲鱼写操作
```

## 验证

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q .
```

原始项目及署名信息请参考 Git 历史。
