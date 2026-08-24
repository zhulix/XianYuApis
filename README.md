# Xianyu Bridge

这是供 `laoxiangji server` 调用的闲鱼消息 Sidecar。Python 负责闲鱼账号登录态、WebSocket 私信监听、创建会话和发送消息；Java 负责闲管家商品/订单业务、推送接收、订单详情查询、改价和短链编排。

> 本项目不是闲鱼官方接口，协议可能变化。写操作必须保留人工兜底；严禁用于违法、骚扰、批量营销或绕过平台风控。

## 能力边界

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 多闲鱼账号扫码登录、Cookie 复用 | 已实现 | 每个账号独立 Cookie、Session 和 WebSocket |
| WebSocket 私信监听、断线重连 | 已实现 | 统一回调为 `CHAT_MESSAGE`，不根据聊天文案猜测订单状态 |
| 文字/图片消息解析 | 已实现 | 未知卡片也保留消息上下文和原始载荷 |
| 主动创建会话、发送文字 | 已实现 | 提供稳定幂等键，Java 可安全重试 |
| 闲管家商品查询、订单查询、改价 | 不在本 Sidecar | 统一由 Java `goofish-sdk` 调用官方开放平台 |
| 订单轮询、发货、取消、退款、商品操作 | 已移除 | Sidecar 不再保存或编排订单状态 |
| 事件可靠回调 | 已实现 | SQLite Outbox、事件 ID 去重、指数退避 |
| 双向 HMAC 鉴权 | 已实现 | 5 分钟时间窗和 nonce 防重放 |

## 运行

要求 Python 3.11+、Node.js 22+。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
set -a; source .env; set +a
.venv/bin/python run_live.py
```

首次运行没有账号时会生成 `login_qr.png` 并进入扫码。Cookie 按账号保存到
`data/accounts/{accountId}.cookies`。二维码、Cookie 目录和 `.env` 都已加入 `.gitignore`。

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
```

## 配置

以 [.env.example](.env.example) 为准：

- `XIANYU_INTERNAL_SECRET`：Java 调用 Sidecar 的 HMAC 密钥；未配置时 `/internal/**` 返回 503。
- `LXJ_EVENT_CALLBACK_URL`、`LXJ_EVENT_CALLBACK_SECRET`：Sidecar 回调 Java；两项同时存在才发送。
- `XIANYU_OUTBOX_PATH`：聊天事件 SQLite Outbox 文件。
- `XIANYU_ACCOUNTS_DIR`：多账号 Cookie 目录，默认 `./data/accounts`。
- `XIANYU_ACCOUNT_SYNC_INTERVAL`：新增、刷新、移除账号的发现间隔，默认 5 秒。

不要把内部接口直接暴露到公网。建议只监听 `127.0.0.1`，或置于仅 Java 可访问的容器网络。

## 内部 API

账号业务接口前缀为 `/internal/accounts/{accountId}`，账号列表是 `/internal/accounts`：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 健康检查和实际能力开关，无需签名 |
| GET | `/internal/accounts` | 列出账号及连接状态 |
| GET | `/internal/accounts/{accountId}/status` | 查询单个账号连接状态 |
| POST | `/internal/accounts/{accountId}/chats` | 根据买家和闲鱼商品创建会话 |
| POST | `/internal/accounts/{accountId}/messages` | 向已有会话发送文字消息 |

`/internal/**` 接口使用以下 HMAC 请求头：

```text
X-Xianyu-Timestamp: Unix 秒
X-Xianyu-Nonce: 每次请求唯一随机值
X-Xianyu-Signature: hex(HMAC-SHA256(secret, canonical))

canonical = timestamp + "\n" + nonce + "\n" + METHOD + "\n" + path + "\n" + sha256(rawBody)
```

签名必须基于实际发送的原始 JSON 字节，不能把对象再次序列化后再计算。

创建会话请求体：

```json
{
  "buyerId": "闲鱼买家ID",
  "itemId": "闲鱼商品ID"
}
```

发送消息请求体：

```json
{
  "chatId": "会话ID",
  "buyerId": "闲鱼买家ID",
  "message": "消息文本",
  "idempotencyKey": "meal-link:本地订单ID"
}
```

Sidecar 会按“账号 + 幂等键”生成稳定消息 UUID。创建会话接口只有在闲鱼返回真实会话 ID
时才返回成功；超时或没有会话 ID 时返回失败/结果未知，Java 不得伪造成功。

## Python -> Java 聊天回调

回调只用于消息上下文和诊断，订单状态不从聊天内容推断。回调体仍保留统一事件结构：

```json
{
  "event_id": "account:message-id",
  "event_type": "CHAT_MESSAGE",
  "account_id": "闲鱼账号ID",
  "occurred_at": 1787462400000,
  "chat_id": "会话ID",
  "message_id": "消息ID",
  "buyer_id": "买家ID",
  "buyer_name": "昵称",
  "item_id": "闲鱼商品ID",
  "order_id": "消息中可识别的订单上下文，可为空",
  "content_type": "text|image|card",
  "content": "消息内容",
  "raw_payload": {}
}
```

回调使用同一套 HMAC 规则，并携带 `X-Xianyu-Event-Id`。Java 端应按事件 ID 做幂等；HTTP
成功但业务响应失败时，Sidecar 会重试。订单被拍下、商品状态/价格/库存变化均由闲管家官方
推送进入 Java 的 `/lxj/goofish/{accountId}/.../push`，不会经过本回调。

## Java 侧职责

1. `XianyuClient` 只调用 Sidecar 的账号状态、建聊和发消息接口。
2. `goofish-sdk` 调用闲管家商品列表/详情、订单列表/详情和改价接口，金额使用分，时间使用 Unix 秒。
3. LXJ Controller 只负责校验并落库商品/订单推送，持久化成功后立即返回；详情查询、匹配商品、建聊和发送短链在后台处理。
4. 订单推送以“账号 + 订单号 + 修改时间”去重，乱序推送不能覆盖较新数据。
5. 普通 `CHAT_MESSAGE` 不触发自动建链；订单首次推送且命中商品白名单时，才创建短链并发送消息。

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
  parser.py                  私信结构化解析
  outbox.py / callback.py    可靠聊天事件回调
  manager.py / runtime.py    账号连接与生命周期
tests/                       离线单元测试，不触发真实闲鱼写操作
```

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile goofish_live.py xianyu_bridge/*.py tests/*.py
```

原始项目及署名信息请参考 Git 历史。
