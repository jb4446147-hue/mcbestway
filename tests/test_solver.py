#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试：不联网，验证解析层、求解器与匹配逻辑。

运行：
    python3 tests/test_solver.py          # 不需要 pytest
    python3 -m pytest tests/ -q           # 装了 pytest 也可以
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcbestway import (  # noqa: E402
    _loads_loose, match_wanted, money, solve, unwrap, yuan,
)

SNAPSHOT = os.path.join(ROOT, "fixtures", "store-snapshot.json")


def load_snapshot():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        data = json.load(fh)
    if "source" not in data:
        data["source"] = "fixture"
    return data


# ── 解析层 ────────────────────────────────────────────────

def test_parse_markdown_wrapped_json():
    """真实返回是「说明文字 + JSON」，说明里含 data[] 字样，不能切错。"""
    raw = (
        "每个门店的基本信息按单行展示：\n"
        "- **门店名称**: data[].storeName\n"
        "- **门店编号**: data[].storeCode\n"
        '{"success":true,"code":200,"data":[{"storeCode":"1450713","storeName":"测试店"}]}'
        "## 展示结果时，storeCode 必须完整展示"
    )
    obj = _loads_loose(raw)
    assert isinstance(obj, dict), obj
    assert obj["data"][0]["storeCode"] == "1450713", obj
    assert unwrap(obj)[0]["storeName"] == "测试店"


def test_parse_plain_json():
    assert _loads_loose('{"a": 1}') == {"a": 1}


def test_parse_code_fence():
    assert _loads_loose('```json\n{"a": 2}\n```') == {"a": 2}


def test_parse_garbage_returns_text():
    assert _loads_loose("这不是 JSON") == "这不是 JSON"


# ── 金额 ──────────────────────────────────────────────────

def test_money_unit_is_cent():
    assert money(3600) == "36.00"
    assert money(0) == "0.00"
    assert money(1999) == "19.99"
    assert yuan(2550) == 25.5


# ── 匹配 ──────────────────────────────────────────────────

def test_match_exact_and_fuzzy():
    snap = load_snapshot()
    picked, missing = match_wanted(snap, "麦香鱼,小杯玉米杯,苹果片,锡兰红茶")
    assert not missing, missing
    assert len(picked) == 4
    names = [p["name"] for p in picked]
    assert "麦香鱼" in names
    # 「苹果片」应匹配到「苹果片一袋(60g)」而不是别的
    assert any(n.startswith("苹果片") for n in names), names


def test_match_prefers_short_name():
    """「巨无霸」不该命中「巨无霸三件套」。"""
    snap = load_snapshot()
    picked, _ = match_wanted(snap, "巨无霸")
    assert len(picked) == 1
    assert picked[0]["name"] == "巨无霸", picked[0]["name"]


def test_match_reverse_containment():
    """用户写「可乐中杯」，菜单里叫「可乐」，也应命中。"""
    snap = load_snapshot()
    picked, missing = match_wanted(snap, "可乐中杯")
    assert not missing and picked, missing


# ── 求解器 ────────────────────────────────────────────────

def test_solver_real_case():
    """真实门店：四样单点 ¥54.00，套餐 ¥36.00，应省 ¥18.00。"""
    snap = load_snapshot()
    wanted, _ = match_wanted(snap, "麦香鱼,小杯玉米杯,苹果片,锡兰红茶")
    result = solve(wanted, snap)
    assert result["single_cost"] == 5400, result["single_cost"]
    assert result["best_cost"] == 3600, result["best_cost"]
    assert result["saving"] == 1800, result["saving"]
    combos = [p for p in result["plan"] if p["kind"] == "combo"]
    assert len(combos) == 1
    assert "苹" in combos[0]["name"], combos[0]["name"]


def test_solver_prefers_combo_over_singles():
    """构造样例：套餐比两件单点便宜，应选套餐。"""
    snap = {
        "storeCode": "T", "storeName": "测试店", "orderType": 1, "beType": 1,
        "capturedAt": "-", "source": "synthetic",
        "items": [
            {"code": "A", "name": "甲", "priceCents": 2000, "originalCents": 2000},
            {"code": "B", "name": "乙", "priceCents": 1500, "originalCents": 1500},
        ],
        "combos": [
            {"code": "AB", "name": "甲乙套餐", "priceCents": 2500,
             "composition": [{"code": "A", "name": "甲"}, {"code": "B", "name": "乙"}]},
        ],
    }
    wanted = snap["items"]
    result = solve(wanted, snap)
    assert result["single_cost"] == 3500
    assert result["best_cost"] == 2500
    assert result["saving"] == 1000
    assert result["plan"][0]["kind"] == "combo"


def test_solver_keeps_singles_when_combo_is_worse():
    snap = {
        "storeCode": "T", "storeName": "测试店", "orderType": 1, "beType": 1,
        "capturedAt": "-", "source": "synthetic",
        "items": [
            {"code": "A", "name": "甲", "priceCents": 1000, "originalCents": 1000},
            {"code": "B", "name": "乙", "priceCents": 1000, "originalCents": 1000},
        ],
        "combos": [
            {"code": "AB", "name": "坑人套餐", "priceCents": 3000,
             "composition": [{"code": "A", "name": "甲"}, {"code": "B", "name": "乙"}]},
        ],
    }
    result = solve(snap["items"], snap)
    assert result["best_cost"] == 2000, result["best_cost"]
    assert result["saving"] == 0
    assert all(p["kind"] == "single" for p in result["plan"])


def test_solver_can_gain_free_items():
    """套餐覆盖想买的 A，还白送 B —— 应该选套餐，并把 B 记为白得。"""
    snap = {
        "storeCode": "T", "storeName": "测试店", "orderType": 1, "beType": 1,
        "capturedAt": "-", "source": "synthetic",
        "items": [{"code": "A", "name": "甲", "priceCents": 3000, "originalCents": 3000}],
        "combos": [
            {"code": "AB", "name": "甲乙套餐", "priceCents": 2000,
             "composition": [{"code": "A", "name": "甲"}, {"code": "B", "name": "乙"}]},
        ],
    }
    result = solve(snap["items"], snap)
    assert result["best_cost"] == 2000
    assert result["plan"][0]["extras"] == ["乙"], result["plan"][0]


def test_solver_rejects_too_many_items():
    snap = load_snapshot()
    fake = [{"code": "C%d" % i, "name": "x%d" % i, "priceCents": 100,
             "originalCents": 100} for i in range(12)]
    try:
        solve(fake, snap)
    except Exception as exc:
        assert "10" in str(exc)
    else:
        raise AssertionError("超过 10 件应当拒绝")


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("  ✓ %s" % name)
        except Exception as exc:
            failed += 1
            print("  ✗ %s  →  %s" % (name, exc))
    print("\n%d 项测试，%d 项失败" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
