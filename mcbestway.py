#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
别单点 · McBestWay
================================================================
你报出想吃的几样，它用麦当劳 MCP 的真实菜单与真实报价，穷举所有买法，
告出最便宜的那一种 —— 而且经常比你想的更便宜，因为你漏掉了套餐。

一个真实例子（上海某店，可在 fixtures/store-snapshot.json 里核对）：
    想吃：巨无霸 + 中薯条 + 可乐中杯
    单点：¥25.50 + ¥13.50 + ¥9.50 = ¥48.50
    改买「巨无霸三件套」：¥36.50
    → 省 ¥12.00，东西一样多。

为什么它值得做成工具
  · 同一批商品，单点与套餐的价差最大能到 25%，但没人会手算 43 个套餐的组合；
  · 套餐还经常「多送」东西（你只要两样，三件套里那杯可乐是白得的）；
  · 所以这是一个精确的组合优化问题，而不是拍脑袋推荐。

它怎么算
  1. query-meals           拿到门店在售的全部餐品编码
  2. calculate-price       批量定价，一次拿到所有餐品的名称与单价
  3. query-meal-detail     解析每个套餐的默认组成（rounds[].choices[]）
  4. 子集 DP              在上述结果上求「覆盖全部想吃的商品」的最低成本买法
  5. calculate-price       对最终买法再报一次价，用平台口径复核

零依赖：只用 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

__version__ = "1.0.0"
DEFAULT_URL = "https://mcp.mcd.cn"
HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(HERE, "fixtures", "store-snapshot.json")

COMBO_WORDS = ("件套", "套餐", "组合", "拼盘", "双人", "三人", "桶", "分享")


# ════════════════════════════════════════════════════════════════
# 1. MCP 客户端（JSON-RPC 2.0 over Streamable HTTP）
# ════════════════════════════════════════════════════════════════

class McpError(RuntimeError):
    pass


class McpClient:
    """麦当劳 MCP 最小客户端。

    · POST https://mcp.mcd.cn，Body 为单个 JSON-RPC 2.0 消息
    · 头：Authorization: Bearer <token>；Accept 需同时声明 json 与 sse
    · 响应可能是 JSON，也可能是 SSE 流，两种都处理
    · 握手：initialize → notifications/initialized → tools/call
    """

    def __init__(self, url=DEFAULT_URL, token="", timeout=25):
        if not token:
            raise McpError("缺少 MCP Token：请设置环境变量 MCD_MCP_TOKEN 或使用 --token。\n"
                           "申请地址 https://open.mcd.cn/mcp")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self._seq = 0
        self._ready = False

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "McBestWay/" + __version__,
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 401:
                raise McpError("MCP Token 无效或已过期（401）。") from None
            if e.code == 429:
                raise McpError("触发限流（429，上限 600 次/分钟），请稍后重试。") from None
            raise McpError("HTTP %d：%s" % (e.code, detail)) from None
        except urllib.error.URLError as e:
            raise McpError("连不上 MCP 服务：%s" % e.reason) from None

        if not raw.strip():
            return None
        s = raw.lstrip()
        if "text/event-stream" in ctype or s.startswith("event:") or s.startswith("data:"):
            return self._parse_sse(raw, payload.get("id"))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise McpError("无法解析返回：%s" % raw[:300]) from None

    @staticmethod
    def _parse_sse(raw, want_id):
        fallback = None
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                msg = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg
            fallback = msg
        return fallback

    def _rpc(self, method, params):
        self._seq += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params})
        if msg is None:
            raise McpError("%s 无返回" % method)
        if msg.get("error"):
            raise McpError("%s 失败：%s" % (method, msg["error"].get("message")))
        return msg.get("result") or {}

    def connect(self):
        if self._ready:
            return
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "McBestWay", "version": __version__}})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except McpError:
            pass
        self._ready = True

    def list_tools(self):
        self.connect()
        return self._rpc("tools/list", {}).get("tools", [])

    def call_raw(self, name, arguments):
        self.connect()
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def call_json(self, name, arguments):
        """优先用 structuredContent；否则从「说明文字 + JSON」的混合文本里抠 JSON。"""
        result = self.call_raw(name, arguments)
        if result.get("isError"):
            raise McpError("%s 返回错误：%s" % (name, _text(result)[:300]))
        payload = result.get("structuredContent")
        if payload in (None, {}, []):
            blob = _text(result)
            payload = _loads_loose(blob) if blob.strip() else result
        if isinstance(payload, dict) and payload.get("success") is False:
            raise McpError("%s 未成功：%s（code=%s）"
                           % (name, payload.get("message"), payload.get("code")))
        return payload


def _text(result):
    return "\n".join(p.get("text") or "" for p in (result.get("content") or [])
                     if isinstance(p, dict) and p.get("type") == "text")


_DEC = json.JSONDecoder()


def _loads_loose(blob):
    """从混合文本中抠出真正的 JSON。

    麦当劳 MCP 的文本返回常以一段 Markdown 说明开头（里面有 `data[].storeName`
    这种字面量），所以不能按「第一个括号」切，必须用 raw_decode 逐个起点试，
    取解析得最长的那个。
    """
    text = (blob or "").strip()
    if not text:
        return text
    if text.startswith("```"):
        text = "\n".join(x for x in text.splitlines() if not x.strip().startswith("```")).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    best, best_len = None, -1
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, end = _DEC.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if end > best_len:
            best, best_len = obj, end
    return best if best_len > 0 else blob


def unwrap(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def yuan(cents):
    return Decimal(int(cents)) / Decimal(100)


def money(cents):
    return str(yuan(cents).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ════════════════════════════════════════════════════════════════
# 2. 从 MCP 构建门店快照
# ════════════════════════════════════════════════════════════════

BATCH = 25          # 单次 calculate-price 的商品上限实测为 30，留余量


def build_snapshot(client, store_code, store_name="", order_type=1, be_type=1,
                   be_code=None, want=None, progress=None):
    """把一家门店「菜单 + 单价 + 套餐组成」抓成一份结构化快照。

    关键技巧：calculate-price 一次可对多件商品报价，返回的 productList 里同时
    带有 productName 与 subtotal —— 于是「拿全菜单的名称和价格」只需几次调用，
    不必对 123 个餐品逐个查详情。
    """
    def log(msg):
        if progress:
            progress(msg)

    base_args = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
    if be_code and be_type != 1:
        base_args["beCode"] = be_code

    meals = unwrap(client.call_json("query-meals", dict(base_args)))
    codes, seen = [], set()
    for cat in (meals.get("categories") or []):
        for meal in (cat.get("meals") or []):
            code = meal.get("code")
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    log("菜单编码 %d 个" % len(codes))
    if not codes:
        raise McpError("query-meals 没返回任何餐品编码。")

    priced = {}
    for i in range(0, len(codes), BATCH):
        chunk = [{"productCode": c, "quantity": 1} for c in codes[i:i + BATCH]]
        try:
            payload = unwrap(client.call_json("calculate-price",
                                              dict(base_args, items=chunk)))
        except McpError:
            continue
        for row in (payload.get("productList") or []):
            code = row.get("productCode")
            if code:
                priced[code] = row
    log("定价成功 %d 个" % len(priced))

    items = []
    for code in codes:
        row = priced.get(code)
        if not row or not row.get("subtotal"):
            continue
        items.append({
            "code": code,
            "name": row.get("productName") or code,
            "priceCents": int(row["subtotal"]),
            "originalCents": int(row.get("originalSubtotal") or row["subtotal"]),
        })

    # 只对「名字像套餐」的餐品查详情，避免 123 次无谓调用
    suspects = [it for it in items if any(w in it["name"] for w in COMBO_WORDS)]
    log("疑似套餐 %d 个，开始解析组成" % len(suspects))

    combos, best_by_name = [], {}
    for it in suspects:
        try:
            detail = unwrap(client.call_json("query-meal-detail",
                                             dict(base_args, code=it["code"])))
        except McpError:
            continue
        comp = []
        for rnd in (detail.get("rounds") or []):
            choices = rnd.get("choices") or []
            picked = [c for c in choices if c.get("isDefault") == 1]
            if not picked:
                picked = [c for c in choices if (c.get("diffPrice") in (None, "", "+ ¥0"))]
            if picked and picked[0].get("code"):
                comp.append({"code": picked[0]["code"],
                             "name": picked[0].get("name") or picked[0]["code"]})
        if len(comp) < 2:
            continue
        combo = {"code": it["code"], "name": it["name"],
                 "priceCents": it["priceCents"], "composition": comp}
        old = best_by_name.get(it["name"])
        if old is None or combo["priceCents"] < old["priceCents"]:
            best_by_name[it["name"]] = combo
    combos = list(best_by_name.values())
    log("解析出 %d 个可用套餐" % len(combos))

    return {
        "storeCode": store_code,
        "storeName": store_name or store_code,
        "orderType": order_type,
        "beType": be_type,
        "capturedAt": time.strftime("%Y-%m-%d %H:%M"),
        "source": "麦当劳 MCP 实时数据",
        "items": items,
        "combos": combos,
    }


# ════════════════════════════════════════════════════════════════
# 3. 最优买法求解（子集 DP）
# ════════════════════════════════════════════════════════════════

def match_wanted(snapshot, wanted_text):
    """把「巨无霸,中薯条」匹配到菜单条目（严格优先，其次包含匹配）。"""
    words = [w.strip() for w in wanted_text.replace("，", ",").replace("、", ",").split(",")
             if w.strip()]
    items = snapshot["items"]
    picked, missing = [], []

    for word in words:
        exact = [it for it in items if it["name"] == word]
        fuzzy = [it for it in items if word in it["name"]]
        # 反向包含：用户写「可乐中杯」而菜单里叫「可乐」时也能命中
        if not exact and not fuzzy:
            fuzzy = [it for it in items if len(it["name"]) >= 2 and it["name"] in word]
        pool = exact or fuzzy
        if not pool:
            missing.append(word)
            continue
        # 包含匹配时优先「最短名字」，避免「巨无霸」命中「巨无霸三件套」
        hit = min(pool, key=lambda x: (len(x["name"]), x["priceCents"]))
        if all(p["code"] != hit["code"] for p in picked):
            picked.append(hit)
    return picked, missing


def solve(wanted, snapshot):
    """求覆盖全部 wanted 的最低成本买法。

    建模：把 wanted 的每一个商品看成必须被覆盖的一个二进制位。
    可选动作有「单点某商品」（覆盖一位）与「买某个套餐」（覆盖若干位，
    多出来的算白得）。对 2^n 个状态做记忆化 DP，取最低价 —— 这是精确解，
    不是启发式。
    """
    catalog = {it["code"]: it for it in snapshot["items"]}
    n = len(wanted)
    if n == 0:
        raise McpError("没有要买的商品。")
    if n > 10:
        raise McpError("一次最多比较 10 件商品（搜索空间 2^10），请减少 --want 的数量。")

    bit = {it["code"]: 1 << i for i, it in enumerate(wanted)}
    full = (1 << n) - 1

    singles = [{"kind": "single", "code": it["code"], "name": it["name"],
                "price": it["priceCents"], "mask": bit[it["code"]]} for it in wanted]

    combo_opts = []
    for cb in snapshot["combos"]:
        mask, covered, extras = 0, [], []
        for comp in cb["composition"]:
            code = comp["code"]
            if code in bit:
                mask |= bit[code]
                covered.append(catalog.get(code, comp)["name"])
            else:
                extras.append(comp["name"])
        if mask == 0:
            continue
        combo_opts.append({"kind": "combo", "code": cb["code"], "name": cb["name"],
                           "price": cb["priceCents"], "mask": mask,
                           "covers": covered, "extras": extras})

    options = singles + combo_opts
    memo = {0: (0, [])}

    def best(mask):
        if mask in memo:
            return memo[mask]
        low = mask & (-mask)
        best_cost, best_plan = None, None
        for opt in options:
            if not (opt["mask"] & low):
                continue            # 必须覆盖当前最低位的未覆盖商品，避免重复枚举
            sub_cost, sub_plan = best(mask & ~opt["mask"])
            total = opt["price"] + sub_cost
            if best_cost is None or total < best_cost:
                best_cost, best_plan = total, [opt] + sub_plan
        memo[mask] = (best_cost, best_plan)
        return memo[mask]

    cost, plan = best(full)
    single_cost = sum(it["priceCents"] for it in wanted)

    return {
        "wanted": wanted,
        "single_cost": single_cost,
        "best_cost": cost,
        "plan": plan,
        "saving": single_cost - cost,
        "combos_considered": len(combo_opts),
    }


# ════════════════════════════════════════════════════════════════
# 4. HTML 报告
# ════════════════════════════════════════════════════════════════

def _bar_svg(single, best, width=880, height=132):
    """两根横向条形：单点 vs 最优买法，差额高亮。"""
    if single <= 0:
        return ""
    pad_l, pad_r = 96, 40
    inner = width - pad_l - pad_r
    top, gap, bar_h = 22, 26, 34
    w1 = inner
    w2 = max(inner * best / single, 4)
    parts = [
        '<rect x="0" y="0" width="%d" height="%d" fill="none"/>' % (width, height),
        '<text x="%d" y="%d" fill="#5B5B66" font-size="11" text-anchor="end">单点</text>'
        % (pad_l - 12, top + 22),
        '<rect x="%d" y="%d" width="%.1f" height="%d" rx="7" fill="#232330"/>'
        % (pad_l, top, w1, bar_h),
        '<text x="%d" y="%d" fill="#8C8C97" font-size="13" font-family="ui-monospace,monospace">¥%s</text>'
        % (pad_l + 14, top + 23, money(single)),
        '<text x="%d" y="%d" fill="#5B5B66" font-size="11" text-anchor="end">最优</text>'
        % (pad_l - 12, top + bar_h + gap + 22),
        '<defs><linearGradient id="g1" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#1FB877"/><stop offset="1" stop-color="#3FE39B"/>'
        '</linearGradient></defs>',
        '<rect x="%d" y="%d" width="%.1f" height="%d" rx="7" fill="url(#g1)"/>'
        % (pad_l, top + bar_h + gap, w2, bar_h),
        '<text x="%d" y="%d" fill="#06251A" font-size="13" font-weight="700" '
        'font-family="ui-monospace,monospace">¥%s</text>'
        % (pad_l + 14, top + bar_h + gap + 23, money(best)),
    ]
    if single > best:
        parts.append('<text x="%.1f" y="%d" fill="#2BD98A" font-size="12" '
                     'font-family="ui-monospace,monospace">省 ¥%s</text>'
                     % (pad_l + w2 + 10, top + bar_h + gap + 23, money(single - best)))
    return '<svg viewBox="0 0 %d %d" width="100%%" xmlns="http://www.w3.org/2000/svg">%s</svg>' \
           % (width, height, "".join(parts))


def render_report(result, snapshot, out_path, verified=None):
    single = result["single_cost"]
    best = result["best_cost"]
    plan = result["plan"]
    saving = result["saving"]
    combos = [p for p in plan if p["kind"] == "combo"]
    extras = []
    for c in combos:
        extras.extend(c.get("extras") or [])

    if combos and saving > 0:
        head = "别单点：省 ¥%s" % money(saving)
        lead = "你单点要花 <b>¥%s</b>。换成「%s」，只要 <b>¥%s</b>。" % (
            money(single), combos[0]["name"], money(best))
        if extras:
            lead += "而且套餐里还多送 %s —— 你本来没点。" % "、".join(extras[:3])
        tone = "good"
    elif saving > 0:
        head = "换个买法，省 ¥%s" % money(saving)
        lead = "单点要花 <b>¥%s</b>，按下表组合只要 <b>¥%s</b>。" % (money(single), money(best))
        tone = "good"
    else:
        head = "这次单点就是最便宜"
        lead = ("已经把 %d 个套餐都算过一遍，覆盖你这几样的最低价就是单点的 "
                "<b>¥%s</b>，放心点。" % (result["combos_considered"], money(single)))
        tone = "flat"

    rows = []
    rows.append('<tr class="base"><td class="n">单点（原方案）</td><td class="m">%s</td>'
                '<td class="p">¥%s</td></tr>'
                % ("、".join(i["name"] for i in result["wanted"]), money(single)))
    for opt in plan:
        if opt["kind"] == "combo":
            note = "覆盖 %s" % "、".join(opt["covers"])
            if opt.get("extras"):
                note += " ＋ 白得 %s" % "、".join(opt["extras"][:3])
            rows.append('<tr class="win"><td class="n">%s<span class="tag">套餐</span></td>'
                        '<td class="m">%s</td><td class="p">¥%s</td></tr>'
                        % (opt["name"], note, money(opt["price"])))
        else:
            rows.append('<tr><td class="n">%s<span class="tag s">单点</span></td>'
                        '<td class="m">套餐没法覆盖，单独买</td><td class="p">¥%s</td></tr>'
                        % (opt["name"], money(opt["price"])))

    verify_line = ""
    if verified is not None:
        same = "一致" if verified == best else "不一致（以平台为准 ¥%s）" % money(verified)
        verify_line = '<div><b>复核</b> 用 calculate-price 对最终买法再报一次价：¥%s，与推算%s</div>' \
                      % (money(verified), same)

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>别单点 · McBestWay</title>
<style>
:root{--bg:#0A0A0C;--card:#121216;--line:#26262E;--fg:#F2F0EB;--muted:#8C8C97;
--dim:#5B5B66;--yellow:#FFC72C;--green:#2BD98A;
--sans:"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(820px 460px at 10% -12%,#1d1608,rgba(10,10,12,0) 62%),var(--bg);
color:var(--fg);font-family:var(--sans);padding:34px 20px 40px}
.wrap{max-width:980px;margin:0 auto}
.brand{display:flex;align-items:center;gap:9px;font-size:11px;letter-spacing:.2em;color:var(--dim);
text-transform:uppercase;font-weight:700;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;overflow:hidden;
box-shadow:0 26px 60px rgba(0,0,0,.5)}
.head{padding:26px 30px 20px;border-bottom:1px solid var(--line);
background:linear-gradient(180deg,#15151A,#111115)}
h1{margin:0 0 10px;font-size:32px;letter-spacing:-.01em;line-height:1.22}
h1.good{background:linear-gradient(100deg,#FFD75E,#FFB800 52%,#FF9A3D);
-webkit-background-clip:text;background-clip:text;color:transparent}
h1.flat{color:#B9B4A6}
.lead{color:#B6B2A8;font-size:13.5px;line-height:1.85}
.lead b{color:var(--fg)}
.chart{padding:14px 22px 4px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-size:9.5px;letter-spacing:.14em;color:var(--dim);text-transform:uppercase;
font-weight:700;padding:9px 8px;border-bottom:1px solid var(--line)}
td{padding:10px 8px;border-bottom:1px solid #1B1B21;color:#B9B5AC;vertical-align:top}
td.n{color:var(--fg);font-weight:600}
td.m{color:#8C8C97;font-size:11.5px}
td.p{font-family:ui-monospace,monospace;text-align:right;white-space:nowrap}
tr.base td{color:#6E6E7A}
tr.base td.p{text-decoration:line-through;text-decoration-color:#4A4A56}
tr.win td{background:rgba(43,217,138,.06)}
tr.win td.n{color:var(--green)}
tr.win td.p{color:var(--green);font-weight:700}
.tag{display:inline-block;margin-left:7px;font-size:9.5px;padding:2px 6px;border-radius:5px;
background:rgba(43,217,138,.16);color:var(--green);font-weight:700;vertical-align:1px}
.tag.s{background:rgba(140,140,151,.14);color:#9A9AA5}
.body{padding:6px 30px 22px}
.meta{margin-top:18px;padding-top:14px;border-top:1px solid var(--line);
font-size:11px;color:var(--dim);line-height:1.95;font-family:ui-monospace,monospace}
.meta b{color:#8C8C97}
.foot{max-width:980px;margin:14px auto 0;font-size:11px;color:#3E3E48;text-align:center;line-height:1.8}
</style></head><body>
<div class="wrap">
  <div class="brand">🍔 <b style="color:#FFC72C;letter-spacing:.04em">McBestWay</b> · 别单点</div>
  <div class="card">
    <div class="head">
      <h1 class="@@TONE@@">@@HEAD@@</h1>
      <div class="lead">@@LEAD@@</div>
    </div>
    <div class="chart">@@BAR@@</div>
    <div class="body">
      <table>
        <tr><th>买法</th><th>说明</th><th style="text-align:right">金额</th></tr>
        @@ROWS@@
      </table>
      <div class="meta">
        <div><b>门店</b> @@STORE@@</div>
        <div><b>数据</b> @@SOURCE@@</div>
        <div><b>比价</b> 已枚举 @@NCOMBO@@ 个可覆盖套餐，子集 DP 求精确最低价</div>
        @@VERIFY@@
        <div><b>说明</b> 金额均来自麦当劳 MCP 的 calculate-price，价格与供应以官方实时结果为准。</div>
      </div>
    </div>
  </div>
</div>
<div class="foot">McBestWay · 麦当劳程序员创意开发大赛参赛作品，非麦当劳官方产品</div>
</body></html>"""

    for token, value in (
        ("@@TONE@@", tone), ("@@HEAD@@", head), ("@@LEAD@@", lead),
        ("@@BAR@@", _bar_svg(single, best)), ("@@ROWS@@", "\n".join(rows)),
        ("@@STORE@@", "%s（%s）" % (snapshot.get("storeName"), snapshot.get("storeCode"))),
        ("@@SOURCE@@", snapshot.get("source", "麦当劳 MCP")),
        ("@@NCOMBO@@", str(result["combos_considered"])),
        ("@@VERIFY@@", verify_line),
    ):
        html = html.replace(token, value)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


# ════════════════════════════════════════════════════════════════
# 5. 命令行
# ════════════════════════════════════════════════════════════════

def cmd_inspect(client):
    print("共 %d 个工具\n" % len(client.list_tools()))
    for tool in client.list_tools():
        print("─" * 66)
        print("%s — %s" % (tool.get("name"), (tool.get("description") or "")[:100]))
        schema = tool.get("inputSchema") or {}
        required = set(schema.get("required") or [])
        for key, spec in (schema.get("properties") or {}).items():
            print("    %-16s %s  %s" % (key, "必填" if key in required else "可选",
                                        (spec.get("description") or "")[:70]))


def run_live(args):
    token = args.token or os.environ.get("MCD_MCP_TOKEN", "")
    client = McpClient(args.url, token, timeout=args.timeout)
    if args.inspect:
        cmd_inspect(client)
        return 0

    store_code, store_name = args.store, ""
    if not store_code:
        if not args.keyword:
            raise McpError("请用 --store 指定门店编码，或给 --city/--keyword 让我先找店。")
        found = unwrap(client.call_json("query-nearby-stores", {
            "beType": 1, "searchType": 2, "city": args.city, "keyword": args.keyword}))
        if not found:
            raise McpError("没找到匹配门店，换个 --keyword 试试。")
        store_code = str(found[0].get("storeCode"))
        store_name = found[0].get("storeName") or ""
        print("→ 门店：%s（%s）" % (store_name, store_code))

    snapshot = build_snapshot(client, store_code, store_name,
                              want=args.want, progress=lambda m: print("  " + m))
    return _finish(args, snapshot, client, store_code)


def run_offline(args):
    if not os.path.exists(SNAPSHOT):
        raise McpError("找不到离线快照 %s" % SNAPSHOT)
    with open(SNAPSHOT, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    snapshot["source"] = "离线快照（真实门店数据，抓取于 %s）" % snapshot.get("capturedAt", "")
    print("→ 离线演示：使用 fixtures/store-snapshot.json 的真实门店快照")
    return _finish(args, snapshot, None, snapshot["storeCode"])


def _finish(args, snapshot, client, store_code):
    wanted, missing = match_wanted(snapshot, args.want)
    if missing:
        print("！菜单里没有：%s" % "、".join(missing), file=sys.stderr)
    if not wanted:
        raise McpError("一件都没匹配上，试试菜单里的名字，例如 --want \"巨无霸,中薯条\"。")
    print("→ 想买：%s" % "、".join(i["name"] for i in wanted))

    result = solve(wanted, snapshot)
    print("→ 单点 ¥%s / 最优 ¥%s / 省 ¥%s（比过 %d 个套餐）"
          % (money(result["single_cost"]), money(result["best_cost"]),
             money(result["saving"]), result["combos_considered"]))

    verified = None
    if client is not None:
        try:
            items = [{"productCode": p["code"], "quantity": 1} for p in result["plan"]]
            payload = unwrap(client.call_json("calculate-price", {
                "storeCode": store_code, "orderType": snapshot["orderType"],
                "beType": snapshot["beType"], "items": items}))
            if payload.get("price") is not None:
                verified = int(payload["price"])
                print("→ 复核：calculate-price 报 ¥%s" % money(verified))
        except McpError as exc:
            print("！复核失败（不影响结论）：%s" % exc, file=sys.stderr)

    out = args.out or "bestway-report.html"
    render_report(result, snapshot, out, verified)

    print("")
    for opt in result["plan"]:
        tag = "套餐" if opt["kind"] == "combo" else "单点"
        line = "  · [%s] %s ¥%s" % (tag, opt["name"], money(opt["price"]))
        if opt.get("extras"):
            line += "   （白得：%s）" % "、".join(opt["extras"][:3])
        print(line)
    print("  · 报告已写入 %s" % out)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="mcbestway.py",
        description="别单点 · McBestWay —— 用麦当劳 MCP 真实报价，算出最便宜的买法。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  离线演示（无需 Token，用真实门店快照）
    python3 mcbestway.py --offline

  真实门店（自动找店）
    python3 mcbestway.py --city 上海 --keyword 人民广场 --want "麦香鱼,小杯玉米杯,苹果片,锡兰红茶"

  查看全部工具与参数
    python3 mcbestway.py --inspect
""")
    p.add_argument("--want", default="麦香鱼,小杯玉米杯,苹果片,锡兰红茶", help="想买的餐品，逗号分隔")
    p.add_argument("--store", default="", help="门店编码；不填则按 --city/--keyword 查找")
    p.add_argument("--city", default="上海", help="城市（找店用）")
    p.add_argument("--keyword", default="人民广场", help="门店关键词（找店用）")
    p.add_argument("--offline", action="store_true", help="用内置真实快照演示，不需要 Token")
    p.add_argument("--inspect", action="store_true", help="打印全部工具及参数结构")
    p.add_argument("--out", default="", help="HTML 报告输出路径")
    p.add_argument("--token", default="", help="MCP Token，也可用环境变量 MCD_MCP_TOKEN")
    p.add_argument("--url", default=DEFAULT_URL, help="MCP 服务地址")
    p.add_argument("--timeout", type=int, default=25, help="单次请求超时秒数")
    p.add_argument("--version", action="version", version="McBestWay " + __version__)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run_offline(args) if args.offline else run_live(args)
    except McpError as exc:
        print("\n✗ %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
