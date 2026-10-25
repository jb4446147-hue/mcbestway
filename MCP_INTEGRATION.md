# MCP 集成说明

本文说明 McBestWay 实际使用了哪些麦当劳 MCP 能力、按什么顺序调用、以及为什么这件事必须依赖 MCP 才能成立。

---

## 1. 接入信息

| 项 | 值 |
|---|---|
| MCP Server | 麦当劳中国 MCP 服务，`https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0 over HTTP POST） |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 会话 | 服务端可能返回 `Mcp-Session-Id`，后续请求需回带 |
| 调用顺序 | `initialize` → `notifications/initialized` → `tools/list` / `tools/call` |
| Token 申请 | <https://open.mcd.cn/mcp> 手机号登录 → 控制台 → 激活 |
| 限流 | 600 次/分钟，超限返回 429 |

客户端实现见 `mcbestway.py` 的 `McpClient` 类（约 130 行，仅用标准库 `urllib`）。它没有依赖任何第三方 MCP SDK，原因是：这个项目希望"clone 下来就能跑"，不想让使用者在装依赖上卡住。

### 一个容易踩的坑：响应不是纯 JSON

麦当劳 MCP 的 `tools/call` 有三种返回形态，程序按优先级处理：

1. **`structuredContent`** —— 服务端给的结构化对象，最可靠，优先使用；
2. **`content[].text`** —— 一段**以 Markdown 说明开头、后面才是 JSON** 的文本。例如：

   ```
   每个门店的基本信息按单行展示：
   - **门店名称**: data[].storeName
   ...
   {"success":true,"code":200,"data":[{...}]}
   ```

   注意说明里就写着 `data[]`。如果按"找到第一个 `[` 就切分"的写法，会解析出一个空数组 —— 这个 bug 我踩过，`_loads_loose()` 里改成用 `json.JSONDecoder.raw_decode()` 对每个括号起点试解析、取"吃掉字符最多"的结果，才稳。
3. **业务失败** —— HTTP 200 但 `success: false`，必须读 `code`/`message` 抛出（如 `600019 商品超过数量限制`、`600012 促销规则不支持该商品`）。

---

## 2. 实际调用的 Tool 与调用流程

### 2.1 用到的 Tool

| Tool | 入参（实际使用的字段） | 在项目中的作用 |
|---|---|---|
| `query-nearby-stores` | `beType=1`, `searchType=2`, `city`, `keyword` | 定位门店，返回 `storeCode` / `storeName` |
| `query-meals` | `storeCode`, `orderType=1`, `beType=1` | 拿门店在售餐品清单：`categories[].meals[].code` |
| `calculate-price` | `storeCode`, `orderType`, `beType`, `items[]` | ① 批量报价取全菜单名称与单价 ② 对候选买法试算 ③ 最终复核 |
| `query-meal-detail` | `storeCode`, `orderType`, `beType`, `code` | 解析套餐默认组成：`rounds[].choices[]`（取 `isDefault=1`） |

### 2.2 一次完整运行的调用链

```
① query-nearby-stores(city, keyword)              → 1 次     拿 storeCode
② query-meals(storeCode)                          → 1 次     拿 123 个餐品编码
③ calculate-price(storeCode, items=编码分批 25 件)  → 5 次     拿全菜单「名称 + 单价」
④ query-meal-detail(每个疑似套餐的 code)           → 43 次    拿套餐默认组成
⑤ calculate-price(最终买法)                        → 1 次     复核价格

合计约 51 次调用，耗时约 40 秒，远低于 600 次/分钟的限流。
```

本地求解（子集 DP）不消耗任何调用 —— MCP 在这里只承担"提供真实事实"和"裁决价格"两个角色。

### 2.3 关键设计：把 `calculate-price` 当价格预言机

本项目的核心假设是：**优惠规则会变，但"这件东西到底多少钱"这个问题永远可以问平台。**

所以代码里**没有任何**硬编码的套餐折扣率、满减门槛或价格表。所有金额都来自 `calculate-price` 的实际返回：

```python
payload = client.call_json("calculate-price", {
    "storeCode": store_code,
    "orderType": 1,          # 1=到店
    "beType": 1,            # 1=到店取餐
    "items": [{"productCode": code, "quantity": 1}],
})
total_cents = payload["price"]     # 单位是「分」
```

平台改价、改活动、改套餐，程序不需要改一行代码。

### 2.4 参数上的三个实测结论

写在文档里，是因为这些都不在 schema 描述里，是踩出来的：

1. **`orderType` / `beType` 是整数枚举**（1=到店、2=外送；1=到店取餐、2=麦乐送、5=得来速、6=团餐），不是字符串。
2. **`beCode` 到店自取场景不能传**，传了会报错；外送/得来速/团餐场景必传。所以本项目默认走 `beType=1`，只需要 `storeCode`。
3. **`items[]` 的字段是 `productCode` + `quantity`**，不是 `code` + `qty`；涉及券时还必须同时给 `couponId` 与 `couponCode`，只给一个会报 `600010`。

---

## 3. 业务价值

### 3.1 它解决的是一个真实且高频的损失

麦当劳的定价结构里，**同一批商品，套餐价与单点价差可以达到 30% 以上**。但这个差价不写在任何一个产品页上：它分散在 42 个套餐里，需要用户自己把"我想吃的四样"和"某个套餐包含的四样"做集合比对。

人工做这件事的成本极高，所以绝大多数用户实际上在为信息差付费。本项目把这件比对工作自动化，并且给出的是**可验证的精确最优解**。

### 3.2 为什么必须用 MCP，而不是爬虫或静态数据

| 需求 | 只有 MCP 能给的 |
|---|---|
| 价格 | 门店 × 时段 × 会员状态相关，静态数据必然过期 |
| 套餐组成 | 每个门店的在售套餐不同，且套餐内可选品会替换 |
| 可信度 | 结论要能被平台自己的报价复核，否则用户不敢信 |

本项目最后一步会用 `calculate-price` 对推荐方案复算一次，把"我的算法"和"平台的账"对齐 —— 这正是 MCP 作为**权威价格源**的价值。

### 3.3 可以延伸的场景

- 多人拼单：把每人的 `--want` 合并后求解，找出"一起买更省"的组合；
- 券叠加：等 `query-promotions` 的满减规则向到店场景开放后，把券纳入 DP 的动作集；
- 个性化：结合 `order-list` 历史订单，识别"你每周都买这几样"，提示长期可省的金额。

---

## 4. 合规与边界

- **不替用户下单**：`create-order` 未接入。工具只输出建议，交易决策留给用户。
- **不存储个人信息**：程序不落盘任何用户标识，`store-snapshot.json` 只含公开的餐品名称与价格，不含地址、手机号、订单号。
- **Token 不入库**：仓库内只有 `mcp-config.example.json`，使用 `YOUR_MCP_TOKEN` 占位符；实际 Token 只从环境变量 `MCD_MCP_TOKEN` 读取。
- **价格以上述复核为准**：报告页明确标注"价格与供应以官方实时结果为准"。
