"""Shared stock/values fetch for Telegram + Rubika bots."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("bloxpulse.core")

API_BASE = "https://bloxfruitsvalues.com/api/ws"
SEED_PATH = Path(__file__).with_name("values_seed.json")

S = requests.Session()
S.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "trpc-accept": "application/json",
        "Origin": "https://bloxfruitsvalues.com",
        "Referer": "https://bloxfruitsvalues.com/stock",
    }
)

FRUITS = [
    "Rocket", "Spin", "Blade", "Chop", "Spring", "Bomb", "Smoke", "Spike",
    "Flame", "Falcon", "Eagle", "Ice", "Sand", "Dark", "Diamond", "Light",
    "Rubber", "Barrier", "Ghost", "Magma", "Quake", "Buddha", "Love",
    "Creation", "Spider", "Sound", "Phoenix", "Portal", "Rumble", "Lightning",
    "Pain", "Blizzard", "Gravity", "Mammoth", "T-Rex", "Dough", "Shadow",
    "Venom", "Control", "Spirit", "Dragon", "West Dragon", "East Dragon",
    "Leopard", "Tiger", "Kitsune", "Yeti", "Gas", "Magnet",
]
LOOKUP = {x.lower(): x for x in FRUITS}
LOOKUP.update({"trex": "T-Rex", "t-rex": "T-Rex", "west dragon": "West Dragon", "east dragon": "East Dragon"})

BASE_VALUES: dict[str, float] = {}
try:
    if SEED_PATH.is_file():
        BASE_VALUES = {k: float(v) for k, v in json.loads(SEED_PATH.read_text(encoding="utf-8")).items()}
except Exception:
    log.warning("values_seed.json missing")

IMAGES: dict[str, str] = {}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fruit_name(text: str) -> str | None:
    t = " ".join(str(text or "").split()).lower()
    for k in sorted(LOOKUP, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", t):
            return LOOKUP[k]
    return None


def parse_num(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*([KMBT])?\b", str(s), re.I)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get((m.group(2) or "").upper(), 1)
    return n * mult


def fmt_num(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= scale:
            v = n / scale
            if v >= 10 or abs(v - int(v)) < 1e-9:
                return f"{int(round(v))}{unit}" if v >= 10 else f"{int(v)}{unit}"
            return f"{v:.2f}".rstrip("0").rstrip(".") + unit
    return f"{int(n)}" if abs(n - int(n)) < 1e-9 else f"{n:g}"


def trpc_get(procedure: str) -> Any:
    url = f"{API_BASE}/{procedure}"
    params = {"batch": "1", "input": json.dumps({"0": {"json": None}}, separators=(",", ":"))}
    r = S.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Empty tRPC for {procedure}")
    item = data[0]
    if "error" in item:
        msg = item["error"].get("json", {}).get("message", str(item["error"]))
        raise RuntimeError(f"tRPC {procedure}: {msg}")
    return item["result"]["data"]["json"]


def fetch_stock() -> dict[str, Any]:
    payload = trpc_get("stock.get")
    out: dict[str, Any] = {"sections": {}, "meta": {}}
    for key, title in (("normal", "معمولی"), ("mirage", "میراژ")):
        fruits = (payload.get(key) or {}).get("fruits") or []
        items = []
        for f in fruits:
            name = fruit_name(f.get("name") or "") or f.get("name") or "?"
            img = f.get("image") or ""
            if img:
                IMAGES[name] = img
            bp, rp = f.get("beliPrice"), f.get("robuxPrice")
            items.append({
                "name": name,
                "image": img,
                "rarity": f.get("rarity") or "",
                "type": f.get("fruitType") or "",
                "beli": bp,
                "robux": rp,
                "value": f.get("regularValue"),
                "value_fmt": fmt_num(f.get("regularValue")),
                "demand": f.get("regularDemand"),
            })
        out["sections"][title] = items
        win = (payload.get(key) or {}).get("window") or {}
        if win.get("resetsAt"):
            out["meta"][f"{key}_resets_at"] = win["resetsAt"]
    if not out["sections"]:
        raise RuntimeError("Stock empty")
    log.info("Stock OK n=%s m=%s", len(out["sections"].get("معمولی", [])), len(out["sections"].get("میراژ", [])))
    return out


def fetch_values() -> dict[str, str]:
    values: dict[str, float] = dict(BASE_VALUES)
    try:
        payload = trpc_get("stock.get")
        for section_key in ("normal", "mirage"):
            for f in (payload.get(section_key) or {}).get("fruits") or []:
                name = fruit_name(f.get("name") or "") or f.get("name")
                if not name:
                    continue
                val = f.get("regularValue")
                if val is not None and val > 0:
                    values[name] = float(val)
                if f.get("image"):
                    IMAGES[name] = f["image"]
    except Exception as e:
        log.warning("live overlay: %s", e)
    if len(values) < 5:
        raise RuntimeError(f"Only {len(values)} values")
    formatted = {k: fmt_num(v) for k, v in values.items()}
    return dict(sorted(formatted.items(), key=lambda x: parse_num(x[1]) or 0, reverse=True))


def format_stock_message(data: dict[str, Any]) -> list[str]:
    """Pretty multi-message stock for Telegram/Rubika."""
    msgs: list[str] = []
    meta = data.get("meta") or {}
    head = f"📦 استاک فعلی\n🕐 {now()}"
    if meta.get("normal_resets_at"):
        try:
            ts = int(meta["normal_resets_at"]) / 1000
            head += f"\n⏱ ریست بعدی: {datetime.fromtimestamp(ts, timezone.utc).strftime('%H:%M')} UTC"
        except Exception:
            pass
    head += "\nریست معمولی ۴س · میراژ ۲س"
    msgs.append(head)

    for title, items in (data.get("sections") or {}).items():
        lines = [f"{'🍏' if title == 'معمولی' else '🌊'} استاک {title} ({len(items)})"]
        for i, it in enumerate(items, 1):
            line = f"\n{i}. {it['name']}"
            if it.get("rarity"):
                line += f" · {it['rarity']}"
            if it.get("value_fmt"):
                line += f"\n   💎 {it['value_fmt']}"
            if it.get("beli") is not None:
                line += f" · 💰 {int(it['beli']):,}"
            if it.get("robux") is not None:
                line += f" · R{int(it['robux']):,}"
            if it.get("demand") is not None:
                line += f" · 📈{it['demand']}/10"
            lines.append(line)
        msgs.append("\n".join(lines))
    return msgs


def format_values_message(data: dict[str, str], limit: int = 90) -> list[str]:
    lines = [f"{i}. {n} — {v}" for i, (n, v) in enumerate(list(data.items())[:limit], 1)]
    header = f"💎 ولیوها ({min(limit, len(data))} آیتم)\n🕐 {now()}\nارزش معاملاتی فیزیکی\n"
    chunks: list[str] = []
    cur = header
    for line in lines:
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = line + "\n"
        else:
            cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks

