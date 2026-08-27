"""美元 -> 人民币汇率, 用于站点币种换算。多源回退, 全部失败时用缓存或内置常数。"""
from __future__ import annotations

import requests

from .history import load_meta, utcnow

FALLBACK_RATE = 7.20


def _frankfurter() -> float:
    # 注意必须带 /v1 且用 frankfurter.dev 域名:
    # api.frankfurter.app/latest 已 301 到 cloudflare 拦截页(2026-08 实测)
    r = requests.get("https://api.frankfurter.dev/v1/latest",
                     params={"base": "USD", "symbols": "CNY"}, timeout=15)
    r.raise_for_status()
    return float(r.json()["rates"]["CNY"])


def _erapi() -> float:
    r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
    r.raise_for_status()
    return float(r.json()["rates"]["CNY"])


def update_fx() -> dict:
    prev = (load_meta().get("fx") or {})
    for source, fn in (("frankfurter.dev", _frankfurter),
                       ("open.er-api.com", _erapi)):
        try:
            rate = fn()
            if rate and 5 < rate < 12:  # 合理性区间, 防接口抽风
                return {"usd_cny": round(rate, 4), "source": source,
                        "fetched_at": utcnow()}
        except Exception:
            continue
    if prev.get("usd_cny"):
        prev["stale"] = True
        return prev
    return {"usd_cny": FALLBACK_RATE, "source": "内置常数", "fetched_at": utcnow(),
            "stale": True}
