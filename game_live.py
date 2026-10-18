#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推门 · 麦记穿越局 · 实时数据版（单文件：前端 + 后端）

- 后端：Flask，直连麦当劳官方 MCP（https://mcp.mcd.cn），实时拉取真实门店菜单
- 选店：从「城市＋关键词」搜索结果中自动挑选营业中的门店（闭店自动切换下一家）
- 前端：像素风「麦记穿越局」游戏（内嵌于本文件），菜单/价格来自上面的实时数据
- 降级：MCP 不可用时自动使用内置快照，游戏不中断

运行：
    python3 game_live.py            # 默认 http://127.0.0.1:8081
    PORT=9000 python3 game_live.py  # 指定端口

环境变量：MCD_MCP_TOKEN / MCD_CITY / MCD_KEYWORD / MCD_STORE / MCD_TTL / PORT
依赖：flask（通常已预装：pip install flask）
"""
import json
import os
import threading
import time
import urllib.request

from flask import Flask, Response, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE, "game_assets")

MCD_URL = os.environ.get("MCD_MCP_URL", "https://mcp.mcd.cn").strip()
TOKEN = os.environ.get("MCD_MCP_TOKEN", "").strip()
CITY = os.environ.get("MCD_CITY", "北京市")
KEYWORD = os.environ.get("MCD_KEYWORD", "中关村")
STORE_FALLBACK = os.environ.get("MCD_STORE", "1950640")
STORE_FALLBACK_NAME = "麦当劳北京善缘街(中关村5号)餐厅"
TTL = float(os.environ.get("MCD_TTL", "30"))
PORT = int(os.environ.get("PORT", "8081"))


# ==================== MCP 调用（JSON-RPC over HTTP） ====================
def mcp_call(name, args=None, timeout=20):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }).encode()
    req = urllib.request.Request(MCD_URL, data=payload, method="POST", headers={
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    j, last = None, None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                j = json.loads(resp.read())
            break
        except Exception as e:
            last = e
            time.sleep(0.6)
    if j is None:
        raise last
    if j.get("error"):
        raise RuntimeError(j["error"].get("message", "mcp error"))
    r = j.get("result") or {}
    if r.get("isError"):
        raise RuntimeError((r.get("content") or [{}])[0].get("text", "tool error"))
    sc = r.get("structuredContent")
    if isinstance(sc, dict) and sc.get("success") is False:
        raise RuntimeError(sc.get("message") or "MCP 接口返回失败")
    return sc


# ==================== 实时菜单 -> 游戏数据 ====================
# (item_id, 真实品名关键词, 清末民初译名, category, icon)
GAME_ITEMS = [
    ("big_mac",   "巨无霸",       "双叠洋牛肉饼夹馍", "MAIN",  "icon_bigmac.png"),
    ("mcspicy",   "麦辣鸡腿汉堡", "香辣酥皮鸡腿面饼", "MAIN",  "icon_mcspicy.png"),
    ("mcchicken", "麦香鸡",       "香酥鸡块馍",       "MAIN",  "icon_mcchicken.png"),
    ("fries_m",   "薯条",         "现炸香脆洋芋条",   "SNACK", "icon_fries.png"),
    ("corn",      "玉米杯",       "御田甜粟米盏",     "SNACK", "icon_corn.png"),
    ("sundae",    "新地",         "焦糖奶冻一盏",     "SNACK", "icon_sundae.png"),
    ("cola_m",    "可乐",         "冰镇荷兰发泡甜水", "DRINK", "icon_cola.png"),
    ("milk_m",    "纯牛奶",       "温热牛乳一盏",     "DRINK", "icon_milk.png"),
]

# 内置快照（MCP 不可用时的兜底，与纯前端版数据一致）
SNAPSHOT_ITEMS = [
    {"item_id": "big_mac",   "name_cn": "双叠洋牛肉饼夹馍", "name_en": "巨无霸",        "price": 26, "price_real": 25.5, "category": "MAIN",  "icon": "icon_bigmac.png"},
    {"item_id": "mcspicy",   "name_cn": "香辣酥皮鸡腿面饼", "name_en": "麦辣鸡腿汉堡",  "price": 23, "price_real": 22.5, "category": "MAIN",  "icon": "icon_mcspicy.png"},
    {"item_id": "mcchicken", "name_cn": "香酥鸡块馍",       "name_en": "麦香鸡",        "price": 17, "price_real": 17.0, "category": "MAIN",  "icon": "icon_mcchicken.png"},
    {"item_id": "fries_m",   "name_cn": "现炸香脆洋芋条",   "name_en": "薯条",          "price": 14, "price_real": 13.5, "category": "SNACK", "icon": "icon_fries.png"},
    {"item_id": "corn",      "name_cn": "御田甜粟米盏",     "name_en": "玉米杯",        "price": 13, "price_real": 13.0, "category": "SNACK", "icon": "icon_corn.png"},
    {"item_id": "sundae",    "name_cn": "焦糖奶冻一盏",     "name_en": "新地",          "price": 12, "price_real": 12.0, "category": "SNACK", "icon": "icon_sundae.png"},
    {"item_id": "cola_m",    "name_cn": "冰镇荷兰发泡甜水", "name_en": "可乐",          "price": 10, "price_real": 9.5,  "category": "DRINK", "icon": "icon_cola.png"},
    {"item_id": "milk_m",    "name_cn": "温热牛乳一盏",     "name_en": "纯牛奶(盒装)",  "price": 11, "price_real": 10.5, "category": "DRINK", "icon": "icon_milk.png"},
]

BAD_WORDS = ("周边", "毛绒", "玩具", "礼盒", "贴纸", "券", "套餐", "分享", "桶")


def best_match(catalog, kw):
    hits = []
    for n, info in catalog.items():
        if kw == n:
            score = 0
        elif n.startswith(kw):
            score = 1
        elif kw in n:
            score = 2
        else:
            continue
        if any(b in n for b in BAD_WORDS):
            score += 10
        hits.append((score, n, info))
    if not hits:
        return None
    hits.sort(key=lambda t: t[0])
    return hits[0][1], hits[0][2]


def fetch_stores():
    sc = mcp_call("query-nearby-stores",
                  {"searchType": 2, "city": CITY, "keyword": KEYWORD, "beType": 1})
    stores = sc.get("data") or []
    return [(str(s["storeCode"]), s.get("storeName") or "") for s in stores]


def fetch_catalog(store_code):
    sc = mcp_call("query-meals", {"storeCode": str(store_code), "orderType": 1, "beType": 1})
    meals = ((sc.get("data") or {}).get("meals")) or {}
    catalog = {}
    for code, info in meals.items():
        name = info.get("name")
        if name and name not in catalog:
            catalog[name] = {**info, "code": code}
    if not catalog:
        raise RuntimeError("该门店菜单为空")
    return catalog


def build_items(catalog, store_code, store_name):
    items, missing = [], []
    for iid, kw, cn, cat, icon in GAME_ITEMS:
        hit = best_match(catalog, kw)
        if not hit:
            missing.append(kw)
            continue
        real_name, info = hit
        real_price = float(info.get("currentPrice") or 0)
        items.append({
            "item_id": iid, "name_cn": cn, "name_en": real_name,
            "price": int(real_price + 0.5), "price_real": real_price,
            "category": cat, "icon": icon,
            "store_code": store_code, "product_code": info.get("code") or "",
        })
    return items, missing


_cache = {"data": None, "ts": 0.0}
_lock = threading.Lock()


def get_live_menu(force=False):
    with _lock:
        now = time.time()
        if not force and _cache["data"] and (now - _cache["ts"]) < TTL:
            return _cache["data"]
        warnings = []
        source = "mcp"
        store_code, store_name = STORE_FALLBACK, STORE_FALLBACK_NAME
        items = None
        if not TOKEN:
            warnings.append("未配置 MCD_MCP_TOKEN 环境变量，已使用内置快照数据")
        else:
            candidates = []
            try:
                candidates = fetch_stores()[:6]
            except Exception as e:
                warnings.append("门店搜索失败：" + str(e))
            if not candidates:
                candidates = [(STORE_FALLBACK, STORE_FALLBACK_NAME)]
            for code, name in candidates:
                try:
                    catalog = fetch_catalog(code)
                    built, missing = build_items(catalog, code, name)
                    if missing:
                        warnings.append(name + "：未匹配菜品 " + "、".join(missing))
                    if len(built) >= 4:
                        store_code, store_name, items = code, name, built
                        break
                    warnings.append(name + "：可匹配菜品不足，切换下一家门店")
                except Exception as e:
                    warnings.append(name + "：" + str(e))
        if items is None:
            items = SNAPSHOT_ITEMS
            source = "snapshot"
        data = {
            "ok": True,
            "source": source,
            "store": {"code": store_code, "name": store_name},
            "updated_at": time.strftime("%H:%M:%S"),
            "ts": int(now),
            "ttl": TTL,
            "items": items,
            "warnings": warnings,
        }
        _cache["data"], _cache["ts"] = data, now
        return data


# ==================== HTTP 服务（前端 + API） ====================
app = Flask(__name__)
try:
    app.json.ensure_ascii = False
except Exception:
    app.config["JSON_AS_ASCII"] = False


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


@app.get("/api/menu")
def api_menu():
    return jsonify(get_live_menu(force=request.args.get("force") == "1"))


@app.get("/game_assets/<path:fname>")
def assets(fname):
    return send_from_directory(ASSETS_DIR, fname)


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>推门 · 麦记穿越局</title>
<style>
:root{
  --gold:#f7c948; --gold-hi:#ffe08a; --gold-dim:#c8942a;
  --red:#ff5b4d; --neon:#ff4fd8; --cyan:#39e6ff;
  --wood:#6b4526; --wood-dk:#2a170e; --ink:#1a0f08; --paper:#f2e2c4;
  --pxfont:"Zpix","Fusion Pixel 12px","Press Start 2P","SimSun","宋体",ui-monospace,"Courier New",monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
[hidden]{display:none!important}
html,body{width:100%;height:100%;overflow:hidden;background:#05030a;font-family:var(--pxfont);color:#f5e6c8}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:999;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.16) 0 1px,transparent 1px 3px);opacity:.5}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:998;
  box-shadow:inset 0 0 180px 60px rgba(0,0,0,.75)}
#stage{position:absolute;left:50%;top:50%;width:1280px;height:720px;margin-left:-640px;margin-top:-360px;
  transform:scale(var(--s,1));transform-origin:center center;overflow:hidden;background:#0a0508;
  box-shadow:0 0 0 4px #000,0 0 80px rgba(255,79,216,.18)}
img.pixel{image-rendering:pixelated}
.bg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.scene{position:absolute;inset:0;opacity:0;pointer-events:none;transition:opacity .35s}
.scene.active{opacity:1;pointer-events:auto}
.dim{position:absolute;inset:0;background:radial-gradient(ellipse at center,rgba(0,0,0,.25),rgba(0,0,0,.74))}
.dim.soft{background:linear-gradient(180deg,rgba(4,2,6,.55) 0%,rgba(4,2,6,.35) 40%,rgba(4,2,6,.78) 100%)}
.chapter-tag{position:absolute;left:50%;top:16px;transform:translateX(-50%);font-size:15px;letter-spacing:8px;color:#ffcf7a;
  text-shadow:0 0 14px rgba(255,180,60,.7),1px 1px 0 #000;z-index:6;padding:6px 26px;border-top:2px solid rgba(255,190,90,.55);border-bottom:2px solid rgba(255,190,90,.55)}
.scan{position:absolute;inset:0;pointer-events:none;z-index:75;opacity:.35;
  background:repeating-linear-gradient(0deg,rgba(255,255,255,.03) 0 1px,transparent 1px 4px)}

.btn{font-family:inherit;font-size:18px;letter-spacing:3px;color:#3a1f00;
  background:linear-gradient(180deg,#ffdf8a,#e8b03c);border:3px solid #7a4a12;padding:10px 26px;cursor:pointer;
  box-shadow:0 4px 0 #4a2a08,0 0 0 3px rgba(20,10,4,.6),inset 0 2px 0 rgba(255,255,255,.5);
  transition:transform .08s,filter .15s}
.btn:hover{filter:brightness(1.08)}
.btn:active{transform:translateY(3px);box-shadow:0 1px 0 #4a2a08,0 0 0 3px rgba(20,10,4,.6)}
.btn.primary{background:linear-gradient(180deg,#ffe08a,#f0a52c,#c87a12)}
.btn.big{font-size:24px;padding:14px 44px;letter-spacing:6px}
.btn.pulse{animation:btnPulse 1s steps(2) infinite alternate}
@keyframes btnPulse{from{filter:brightness(1)}to{filter:brightness(1.25) drop-shadow(0 0 10px rgba(247,201,72,.7))}}

/* ---------- S0 start ---------- */
.start-wrap{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px;text-align:center;z-index:5}
.game-title{font-size:66px;letter-spacing:8px;color:var(--gold-hi);
  text-shadow:0 0 20px rgba(255,120,220,.5),0 5px 0 #7a3a00,0 0 70px rgba(255,190,60,.4);
  animation:titleFlicker 4s infinite}
.game-title span{color:var(--neon)}
@keyframes titleFlicker{0%,93%,100%{opacity:1}94%{opacity:.72}96%{opacity:1}97%{opacity:.8}}
.game-sub{font-size:20px;letter-spacing:6px;color:var(--cyan);text-shadow:0 0 14px rgba(57,230,255,.55)}
.how{display:flex;gap:14px;margin-top:6px}
.how-item{background:rgba(12,6,10,.78);border:2px solid #8a5a28;padding:10px 16px;font-size:15px;letter-spacing:1px;color:#ffd98a;
  box-shadow:0 0 0 2px rgba(0,0,0,.6)}
.how-item b{color:var(--neon);margin-right:8px}
.start-note{position:absolute;bottom:20px;width:100%;text-align:center;font-size:14px;letter-spacing:6px;color:rgba(255,224,170,.55);
  text-shadow:1px 1px 0 #000}

/* ---------- S1 door ---------- */
#sc-door{overflow:hidden}
.door-img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transform-origin:50% 55%}
.door-img.zoom{animation:doorZoom 2.4s cubic-bezier(.5,.05,.9,.6) forwards}
@keyframes doorZoom{0%{transform:scale(1)}70%{transform:scale(1.14)}100%{transform:scale(1.42)}}
.door-word{position:absolute;left:50%;bottom:44px;transform:translateX(-50%);font-size:20px;letter-spacing:8px;
  color:#fff;text-shadow:0 0 16px rgba(255,255,255,.9);animation:doorWord 2.2s steps(6) forwards}
@keyframes doorWord{0%{opacity:0}30%{opacity:1}100%{opacity:1}}
.flash{position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;z-index:90}
.flash.go{animation:flash .55s ease-out forwards}
@keyframes flash{0%{opacity:0}18%{opacity:.96}100%{opacity:0}}

/* ---------- S2 dialog ---------- */
.xiaoer{position:absolute;right:36px;bottom:-24px;height:560px;filter:drop-shadow(0 0 24px rgba(255,79,216,.35))}
.dialog-box{position:absolute;left:44px;bottom:44px;width:730px;background:linear-gradient(180deg,rgba(34,18,10,.96),rgba(18,9,5,.97));
  border:3px solid var(--wood);box-shadow:0 0 0 3px var(--wood-dk),inset 0 0 40px rgba(0,0,0,.6),0 0 60px rgba(255,79,216,.15);
  padding:22px 26px 20px;z-index:6}
.dialog-name{position:absolute;top:-20px;left:22px;background:linear-gradient(180deg,#ff8a3d,#d8481f);color:#fff3d8;
  padding:4px 16px;font-size:16px;letter-spacing:4px;border:2px solid #7a2a10;box-shadow:0 0 0 2px rgba(0,0,0,.6)}
.dialog-text{min-height:104px;font-size:21px;line-height:1.75;letter-spacing:1.5px;color:#ffe9c2;text-shadow:1px 1px 0 #000}
.dialog-box .btn{position:absolute;right:20px;bottom:14px}
.dialog-caret{position:absolute;right:150px;bottom:22px;color:var(--gold);animation:caret .7s steps(2) infinite}
@keyframes caret{50%{opacity:0}}

/* ---------- S3/S4/S5 table ---------- */
.hud{position:absolute;top:14px;left:24px;right:24px;height:64px;display:flex;align-items:center;gap:18px;z-index:10}
#hourglass{width:56px;height:56px;filter:drop-shadow(0 0 10px rgba(57,230,255,.45));animation:hgBob 2.4s ease-in-out infinite}
@keyframes hgBob{50%{transform:translateY(4px) rotate(2deg)}}
#hourglass.danger{filter:drop-shadow(0 0 12px rgba(255,60,40,.9)) hue-rotate(-90deg) saturate(2.2);animation:hgShake .3s steps(2) infinite}
@keyframes hgShake{50%{transform:translateX(2px) rotate(-3deg)}}
.bar{flex:0 0 440px;height:20px;border:2px solid var(--wood);background:#140a04;box-shadow:0 0 0 2px var(--wood-dk),inset 0 2px 6px rgba(0,0,0,.8)}
.bar i{display:block;height:100%;width:100%;background:linear-gradient(90deg,#2fc8e8,#8ef4ff);transition:width .12s linear}
.bar.danger i{background:linear-gradient(90deg,#d8231a,#ff7a55);animation:barBlink .4s steps(2) infinite alternate}
@keyframes barBlink{from{filter:brightness(1)}to{filter:brightness(1.5)}}
.time{font-size:26px;color:var(--cyan);text-shadow:0 0 12px rgba(57,230,255,.6);min-width:110px}
.time.danger{color:var(--red);text-shadow:0 0 12px rgba(255,70,50,.8)}
.hud-spacer{flex:1}
.target-chip{display:flex;align-items:center;gap:8px;background:var(--paper);color:#3a2317;border:3px solid #b28c52;
  padding:6px 16px;font-size:19px;letter-spacing:2px;box-shadow:0 0 0 3px rgba(20,10,4,.6),0 4px 0 rgba(0,0,0,.45);transform:rotate(-2deg)}
.target-chip img{width:30px;height:30px}
.target-chip b{font-size:26px;color:#a3210f;text-shadow:none}
.hintbar{position:absolute;left:50%;top:88px;transform:translateX(-50%);font-size:23px;letter-spacing:7px;color:#ffdf9e;
  text-shadow:0 0 16px rgba(255,180,60,.6),1px 1px 0 #000;z-index:9;animation:hintBlink 1.6s steps(2) infinite alternate}
@keyframes hintBlink{from{opacity:.85}to{opacity:1}}
.grid{position:absolute;left:50%;top:124px;transform:translateX(-50%);display:grid;grid-template-columns:repeat(3,344px);gap:20px;z-index:5}
.dish{position:relative;width:344px;height:198px;display:flex;flex-direction:column;align-items:center;padding:8px 10px 6px;
  background:linear-gradient(180deg,rgba(40,22,11,.94),rgba(22,11,6,.95));border:3px solid var(--wood);
  box-shadow:0 0 0 3px var(--wood-dk),inset 0 0 28px rgba(0,0,0,.65);transition:transform .1s,border-color .1s,box-shadow .1s}
.dish-icon{width:76px;height:76px;image-rendering:pixelated;filter:drop-shadow(0 3px 0 rgba(0,0,0,.5))}
.dish-name{font-size:18px;color:#f7ecd2;letter-spacing:1px;margin-top:4px;text-shadow:1px 1px 0 #000;white-space:nowrap}
.dish-en{font-size:12px;color:rgba(255,255,255,.45);letter-spacing:2px;margin-top:2px}
.dish-price{position:relative;margin-top:auto;font-size:34px;font-weight:700;color:var(--gold);line-height:1;
  text-shadow:0 0 12px rgba(247,201,72,.4),2px 2px 0 #4a2a00}
.dish-price span{font-size:16px;color:#d8b36a;margin-left:6px;font-weight:400}
.dish-price.dissolving{pointer-events:none;animation:dissolve 1.5s steps(10) forwards}
@keyframes dissolve{0%{opacity:1;transform:none;filter:none}
  25%{opacity:.9;transform:translateY(-2px)}
  45%{opacity:.6;transform:translateY(-6px);filter:blur(1px)}
  65%{opacity:.35;transform:translateY(-12px) scale(1.04);filter:blur(2px)}
  85%{opacity:.12;filter:blur(3px)}
  100%{opacity:0;transform:translateY(-20px) scale(1.1);filter:blur(4px)}}
.dust{position:absolute;pointer-events:none;z-index:6}
[data-mode="picking"] .dish{cursor:pointer}
[data-mode="picking"] .dish:hover{transform:translateY(-4px);border-color:var(--gold-dim)}
[data-mode="picking"] .dish.sel{border-color:var(--gold);box-shadow:0 0 0 3px var(--wood-dk),0 0 26px rgba(247,201,72,.55),inset 0 0 30px rgba(247,201,72,.14)}
.dish-count{display:none;position:absolute;top:8px;right:10px;min-width:38px;height:34px;padding:0 6px;align-items:center;justify-content:center;
  background:var(--gold);color:#3a1f00;font-size:18px;font-weight:800;border:2px solid #7a4a12;box-shadow:0 2px 0 rgba(0,0,0,.5)}
.dish.sel .dish-count{display:flex}
.dish-pop{animation:pop .18s steps(2)}
@keyframes pop{50%{transform:scale(1.05)}}
.bill{position:absolute;left:22px;right:22px;bottom:16px;height:150px;display:flex;flex-direction:column;gap:8px;padding:12px 16px;
  background:linear-gradient(180deg,rgba(32,17,8,.95),rgba(15,7,3,.97));border:3px solid var(--wood);
  box-shadow:0 0 0 3px var(--wood-dk),inset 0 0 34px rgba(0,0,0,.6);z-index:12}
.bill-list{flex:1;display:flex;flex-wrap:wrap;gap:8px;align-content:flex-start;overflow:hidden}
.bill-list .empty{color:rgba(255,255,255,.35);font-size:16px;letter-spacing:3px;padding-top:6px}
.chip{background:var(--paper);color:#3a2317;border:2px solid #b28c52;padding:4px 10px;font-size:15px;letter-spacing:1px;cursor:pointer;
  box-shadow:2px 2px 0 rgba(0,0,0,.55)}
.chip:hover{background:#ffd98a}
.chip b{color:#a3210f}
.bill-row{display:flex;align-items:center;gap:14px}
.bill-count{font-size:15px;color:#c9a06a;letter-spacing:2px}
.bill-warn{font-size:16px;color:var(--red);letter-spacing:2px;animation:barBlink .4s steps(2) infinite alternate}
.bill-right{margin-left:auto;display:flex;align-items:baseline;gap:8px;font-size:17px;color:#d8b36a}
.bill-right .total{font-size:34px;color:var(--gold);text-shadow:0 0 14px rgba(247,201,72,.5)}
.bill-right .total.over{color:var(--red);text-shadow:0 0 16px rgba(255,70,50,.75)}
.bill-right em{font-style:normal;font-size:14px;color:#c9a06a;letter-spacing:1px}
.bill-right .btn{margin-left:16px}
.target-splash{position:absolute;left:50%;top:47%;transform:translate(-50%,-50%) ;z-index:30;text-align:center;padding:22px 44px;
  background:linear-gradient(180deg,rgba(46,24,10,.97),rgba(24,11,5,.97));border:4px solid var(--gold-dim);
  box-shadow:0 0 0 4px var(--wood-dk),0 0 70px rgba(247,201,72,.5),inset 0 0 44px rgba(0,0,0,.6)}
.target-splash.show{animation:splashIn .5s steps(7) both}
@keyframes splashIn{0%{transform:translate(-50%,-50%) scale(2.4);opacity:0}60%{opacity:1}100%{transform:translate(-50%,-50%) scale(1);opacity:1}}
.ts-label{font-size:18px;letter-spacing:6px;color:#e8c98a}
.ts-num{margin:6px 0 2px;line-height:1}
.ts-num b{font-size:88px;color:var(--gold);text-shadow:0 0 26px rgba(247,201,72,.75),3px 3px 0 #6b3800}
.ts-num span{font-size:26px;color:#d8b36a;margin-left:8px}
.ts-sub{font-size:15px;letter-spacing:3px;color:rgba(255,255,255,.55)}
.vignette{position:absolute;inset:0;pointer-events:none;opacity:0;z-index:35;transition:opacity .2s;
  box-shadow:inset 0 0 150px 46px rgba(255,40,30,.62)}
.vignette.on{opacity:1;animation:vig .45s steps(2) infinite alternate}
@keyframes vig{from{opacity:.45}to{opacity:1}}
.scene.shaking{animation:shake .4s steps(2)}
@keyframes shake{0%{transform:translate(0,0)}25%{transform:translate(-6px,3px)}50%{transform:translate(5px,-3px)}75%{transform:translate(-3px,-2px)}100%{transform:translate(0,0)}}

/* ---------- judge ---------- */
.judge{position:absolute;inset:0;z-index:60;display:none;align-items:center;justify-content:center}
.judge.show{display:flex}
.judge-dim{position:absolute;inset:0;background:rgba(6,2,1,.8)}
.stamp{width:430px;height:430px;image-rendering:pixelated;filter:drop-shadow(0 16px 30px rgba(255,60,40,.5))}
.stamp.drop{animation:stampIn .5s cubic-bezier(.2,1.6,.4,1) both}
@keyframes stampIn{0%{transform:translateY(-460px) scale(1.7) rotate(-10deg);opacity:0}
  55%{transform:translateY(0) scale(1) rotate(-8deg);opacity:1}
  68%{transform:translateY(-16px) scale(.97) rotate(-8deg)}
  100%{transform:translateY(0) scale(1) rotate(-8deg);opacity:1}}
.judge-text{position:absolute;bottom:96px;left:50%;transform:translateX(-50%);font-size:46px;letter-spacing:14px;color:var(--gold);
  text-shadow:0 0 22px rgba(247,201,72,.7),3px 3px 0 #4a2a00}
.judge-text.bad{color:var(--red);text-shadow:0 0 22px rgba(255,70,50,.8),3px 3px 0 #4a0000}

/* ---------- S7 win ---------- */
#sc-win .bg{filter:brightness(.72) saturate(1.1)}
.win-glow{position:absolute;left:50%;top:44%;width:820px;height:560px;transform:translate(-50%,-50%);pointer-events:none;
  background:radial-gradient(ellipse at center,rgba(255,206,110,.28),transparent 62%)}
.doorhome{position:absolute;left:50%;top:38px;width:230px;height:230px;transform:translateX(-50%);image-rendering:pixelated;
  filter:drop-shadow(0 0 32px rgba(255,190,80,.85));animation:doorFloat 2.6s ease-in-out infinite}
@keyframes doorFloat{50%{transform:translateX(-50%) translateY(-10px)}}
.doorhome-cap{position:absolute;left:50%;top:276px;transform:translateX(-50%);font-size:15px;letter-spacing:5px;color:#ffe1a1;text-shadow:0 0 12px rgba(255,190,80,.7)}
.zhanggui{position:absolute;left:56px;bottom:0;height:470px;image-rendering:pixelated;filter:drop-shadow(0 0 24px rgba(255,190,80,.32));animation:zgBob 3.2s ease-in-out infinite}
@keyframes zgBob{50%{transform:translateY(-8px)}}
.result-card{position:absolute;right:86px;top:50%;transform:translateY(-50%);width:560px;padding:26px 30px 24px;text-align:center;
  background:linear-gradient(180deg,rgba(44,23,10,.97),rgba(20,9,4,.98));border:4px solid var(--gold-dim);
  box-shadow:0 0 0 4px var(--wood-dk),0 0 80px rgba(247,201,72,.4),inset 0 0 60px rgba(0,0,0,.6)}
.seal-mini{position:absolute;top:-46px;right:-30px;width:96px;height:96px;image-rendering:pixelated;transform:rotate(14deg);
  filter:drop-shadow(0 6px 10px rgba(0,0,0,.6))}
.grade{font-size:44px;letter-spacing:8px;color:var(--gold-hi);text-shadow:0 0 24px rgba(247,201,72,.7),3px 3px 0 #6b3800}
.grade-line{margin-top:10px;font-size:21px;letter-spacing:2px;color:#ffe9c2;line-height:1.6}
.discount{margin:16px auto 0;display:inline-block;padding:8px 30px;font-size:26px;letter-spacing:8px;color:#fff3d8;
  background:linear-gradient(180deg,#ff8a3d,#d8481f);border:3px solid #7a2a10;box-shadow:0 4px 0 rgba(0,0,0,.5),0 0 26px rgba(255,120,40,.5);transform:rotate(-2deg)}
.stats{display:flex;justify-content:center;gap:12px;margin-top:18px}
.stats div{flex:1;background:rgba(0,0,0,.4);border:2px solid #6b4526;padding:10px 4px}
.stats span{display:block;font-size:13px;color:#c9a06a;letter-spacing:2px}
.stats b{display:block;margin-top:6px;font-size:22px;color:var(--gold)}
#btnAgainWin{margin-top:22px}
.confetti{position:absolute;inset:0;pointer-events:none;overflow:hidden;z-index:20}
.confetti span{position:absolute;top:-24px;width:9px;height:9px;animation:fall linear forwards}
@keyframes fall{0%{transform:translateY(0) rotate(0)}100%{transform:translateY(780px) rotate(540deg);opacity:.9}}

/* ---------- S7.5 modern return ---------- */
#sc-modern .bg{filter:brightness(.94) saturate(1.06)}
.modern-dim{background:linear-gradient(180deg,rgba(3,5,12,.62),rgba(3,5,12,.3) 42%,rgba(3,5,12,.82))}
.modern-nar{position:absolute;top:30px;left:50%;transform:translateX(-50%);font-size:16px;letter-spacing:5px;z-index:6;
  color:rgba(198,232,255,.8);text-shadow:0 0 14px rgba(57,230,255,.45),1px 1px 0 #000}
.modern-kiosk{position:absolute;right:280px;bottom:96px;height:400px;z-index:4;
  filter:drop-shadow(0 16px 26px rgba(0,0,0,.6)) drop-shadow(0 0 30px rgba(57,230,255,.16))}
.modern-clerk{position:absolute;right:24px;bottom:0;height:560px;z-index:5;
  filter:drop-shadow(0 0 26px rgba(57,230,255,.16));animation:clerkBob 3.6s ease-in-out infinite}
@keyframes clerkBob{50%{transform:translateY(-7px)}}
.dialog-box.modern-box{width:700px;border-color:#3d4657;box-shadow:0 0 0 3px #090b10,inset 0 0 40px rgba(0,0,0,.6),0 0 60px rgba(57,230,255,.14)}
.modern-box .dialog-caret{right:200px}
.dialog-name.modern{background:linear-gradient(180deg,#3bb8e8,#1f6fd8);border-color:#144a8c;color:#eaf7ff}

/* ---------- S8 lose / wash qte ---------- */
#sc-lose .bg{filter:brightness(.85) saturate(1.05)}
.lose-head{position:absolute;top:34px;left:50%;transform:translateX(-50%);text-align:center;z-index:6}
.lose-head h2{font-size:34px;letter-spacing:6px;color:#ffc9a0;text-shadow:0 0 18px rgba(255,110,60,.6),2px 2px 0 #3a0d00}
.lose-head p{margin-top:10px;font-size:17px;letter-spacing:3px;color:#ffdf9e}
.zhanggui-sm{position:absolute;right:40px;bottom:60px;height:330px;image-rendering:pixelated;opacity:.92;
  filter:drop-shadow(0 0 18px rgba(255,190,80,.3));animation:zgBob 3.4s ease-in-out infinite}
.qte-zone{position:absolute;left:50%;bottom:40px;transform:translateX(-50%);display:flex;flex-direction:column;align-items:center;gap:14px;z-index:8}
.basin{position:relative;width:280px;height:280px;border-radius:50%;cursor:pointer;
  background:radial-gradient(circle at 50% 42%,#8a5a2e 0 33%,#6f4423 33% 38%,#8a5a2e 38% 52%,#5a3418 52% 58%,#7a4e28 58% 73%,#40230f 73% 100%);
  box-shadow:0 16px 0 -8px #1d0f05,0 0 60px rgba(255,120,40,.35),inset 0 0 46px rgba(0,0,0,.6);
  transition:transform .06s}
.basin:active{transform:scale(.97)}
.basin::after{content:"";position:absolute;left:13%;top:11%;width:74%;height:74%;border-radius:50%;
  background:radial-gradient(circle at 50% 38%,#e8f7fc 0 8%,#a9d3e2 32%,#6f9fb8 62%,#3f688a 100%);
  box-shadow:inset 0 8px 20px rgba(255,255,255,.55),inset 0 -12px 22px rgba(16,50,80,.85)}
.basin .wash-label{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:3;font-size:22px;font-weight:800;letter-spacing:6px;
  color:#0e3b52;text-shadow:0 1px 0 rgba(255,255,255,.65)}
.ripple{position:absolute;left:50%;top:50%;width:40px;height:40px;margin:-20px 0 0 -20px;border-radius:50%;border:3px solid rgba(255,255,255,.85);
  pointer-events:none;z-index:4}
.bubble{position:absolute;border-radius:50%;background:rgba(255,255,255,.85);pointer-events:none;z-index:4}
.qte-meta{font-size:19px;letter-spacing:3px;color:#ffe9c2;text-shadow:1px 1px 0 #000}
.qte-meta b{color:var(--gold);font-size:24px}
.qte-bar{width:420px;height:16px;border:2px solid var(--wood);background:#140a04;box-shadow:0 0 0 2px var(--wood-dk)}
.qte-bar i{display:block;height:100%;width:100%;background:linear-gradient(90deg,#ff8a3d,#ffd98a)}
.dots{display:flex;flex-wrap:wrap;width:432px;justify-content:center}
.dots i{display:inline-block;width:13px;height:13px;margin:3px;background:#3a2317;border:2px solid #6b4526}
.dots i.on{background:var(--gold);box-shadow:0 0 9px rgba(247,201,72,.85)}
.pass-banner{position:absolute;left:50%;top:42%;transform:translate(-50%,-50%);z-index:40;text-align:center;padding:20px 40px;
  background:rgba(14,7,3,.95);border:4px solid var(--gold-dim);box-shadow:0 0 0 4px var(--wood-dk),0 0 60px rgba(247,201,72,.5)}
.pass-banner h3{font-size:34px;letter-spacing:6px;color:var(--gold-hi);text-shadow:0 0 20px rgba(247,201,72,.7)}
.pass-banner p{margin-top:10px;font-size:17px;letter-spacing:4px;color:#ffe9c2}
.gameover{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:45;text-align:center;padding:30px 54px;
  background:linear-gradient(180deg,rgba(40,16,8,.97),rgba(16,6,3,.98));border:4px solid #8a3a1a;
  box-shadow:0 0 0 4px var(--wood-dk),0 0 70px rgba(255,70,40,.4)}
.gameover h3{font-size:36px;letter-spacing:8px;color:#ffb59a;text-shadow:0 0 20px rgba(255,90,50,.7)}
.gameover p{margin:14px 0 22px;font-size:18px;letter-spacing:3px;color:#ffdf9e}
.toast{position:absolute;left:50%;bottom:22px;transform:translateX(-50%);z-index:95;max-width:960px;display:none;
  background:rgba(88,22,10,.96);border:3px solid #d8481f;color:#ffe9c2;padding:12px 22px;font-size:15px;letter-spacing:1px;
  box-shadow:0 0 0 3px rgba(20,10,4,.7),0 0 44px rgba(255,80,40,.45)}
.toast.show{display:block}
</style>
</head>
<body>
<div id="stage">
  <div class="scan"></div>

  <section id="sc-start" class="scene active">
    <img class="bg pixel" src="game_assets/bg_start_modern.png?v=3" alt="">
    <div class="dim"></div>
    <div class="chapter-tag">序章 · 考价关</div>
    <div class="start-wrap">
      <h1 class="game-title">推门<span>·</span>麦记穿越局</h1>
      <p class="game-sub">百年老号 · 番菜馆奇谈</p>
      <div class="how">
        <div class="how-item"><b>壹</b>看菜单记价格 · 10 秒</div>
        <div class="how-item"><b>贰</b>价码消散后照数凑单 · 30 秒</div>
        <div class="how-item"><b>叁</b>不超目标一成，即可推门回家</div>
      </div>
      <p id="liveStatus" style="font-size:14px;letter-spacing:2px;color:#9fe8ff;text-shadow:0 0 12px rgba(57,230,255,.5);cursor:pointer" title="点击刷新实时菜单">📡 正在连接麦当劳 MCP 实时菜单…</p>
      <button class="btn primary big pulse" id="btnStart">推门进店</button>
    </div>
    <p class="start-note">门开一瞬 · 百年一梦</p>
  </section>

  <section id="sc-door" class="scene">
    <img class="bg pixel door-img" id="doorImg" src="game_assets/bg_door_glow.png?v=3" alt="">
    <div class="door-word">门后，是一百年前……</div>
  </section>

  <section id="sc-dialog" class="scene">
    <img class="bg pixel" src="game_assets/bg_restaurant_main.png?v=3" alt="">
    <div class="dim"></div>
    <img class="xiaoer pixel" src="game_assets/char_xiaoer.png?v=3" alt="">
    <div class="dialog-box" id="dialogBox">
      <div class="dialog-name">小二 · 上前招呼</div>
      <p class="dialog-text" id="dialogText"></p>
      <span class="dialog-caret">▌</span>
      <button class="btn" id="btnKnow">晓得了</button>
    </div>
  </section>

  <section id="sc-table" class="scene" data-mode="memorize">
    <img class="bg pixel" src="game_assets/bg_restaurant_main.png?v=3" alt="">
    <div class="dim soft"></div>
    <div class="hud">
      <img id="hourglass" src="game_assets/prop_hourglass.png?v=3" alt="">
      <div class="bar" id="timeBarBox"><i id="timeBar"></i></div>
      <b class="time" id="timeText">10.0s</b>
      <div class="hud-spacer"></div>
      <div class="target-chip" id="targetChip" hidden>
        <img src="game_assets/prop_coin.png?v=3" alt=""><span>目标</span><b id="targetText">0</b><span>文</span>
      </div>
    </div>
    <div class="hintbar" id="hintBar">记住价格！</div>
    <div class="grid" id="grid"></div>
    <div class="target-splash" id="targetSplash" hidden>
      <div class="ts-label">掌柜出题 · 凑足</div>
      <div class="ts-num"><b id="splashNum">0</b><span>文</span></div>
      <div class="ts-sub">误差一成之内，即可推开回家的门</div>
    </div>
    <div class="bill" id="billBar" hidden>
      <div class="bill-list" id="billList"></div>
      <div class="bill-row">
        <span class="bill-count" id="billCount">水单 0 件</span>
        <span class="bill-warn" id="overWarn" hidden>已超一成！掌柜要来收碗了！</span>
        <div class="bill-right">
          <span>累计</span><b class="total" id="totalText">0</b><span>文</span>
          <em>/ 目标 <span id="totalTarget">0</span> 文</em>
          <button class="btn primary" id="btnSubmit">提交水单</button>
        </div>
      </div>
    </div>
    <div class="vignette" id="vignette"></div>
  </section>

  <section id="sc-win" class="scene">
    <img class="bg pixel" src="game_assets/bg_restaurant_main.png?v=3" alt="">
    <div class="win-glow"></div>
    <img class="doorhome" src="game_assets/prop_door_home.png?v=3" alt="">
    <div class="doorhome-cap">百年后的门 · 已开</div>
    <img class="zhanggui" src="game_assets/char_zhanggui.png?v=3" alt="">
    <div class="result-card">
      <img class="seal-mini" src="game_assets/prop_seal_qi.png?v=3" alt="">
      <div class="grade" id="gradeTitle">神算子</div>
      <p class="grade-line" id="gradeLine"></p>
      <div class="discount" id="discountTag">全单免费</div>
      <div class="stats">
        <div><span>提交总价</span><b id="wPicked">0 文</b></div>
        <div><span>掌柜目标</span><b id="wTarget">0 文</b></div>
        <div><span>误差</span><b id="wRate">0%</b></div>
      </div>
      <button class="btn primary" id="btnGoHome">推开回家的门</button>
    </div>
    <div class="confetti" id="confetti"></div>
  </section>

  <section id="sc-modern" class="scene">
    <img class="bg pixel" id="modernBg" src="game_assets/bg_modern_hall.png?v=3" alt="">
    <div class="dim modern-dim"></div>
    <div class="modern-nar">场景 · 百年后的麦记门店 · 自助点餐机旁</div>
    <img class="modern-kiosk pixel" src="game_assets/prop_kiosk.png?v=3" alt="">
    <img class="modern-clerk pixel" src="game_assets/char_clerk.png?v=3" alt="">
    <div class="dialog-box modern-box" id="modernBox">
      <div class="dialog-name modern">店员 · 小美</div>
      <p class="dialog-text" id="modernText"></p>
      <span class="dialog-caret">▌</span>
      <button class="btn" id="btnModern">…</button>
    </div>
  </section>

  <section id="sc-lose" class="scene">
    <img class="bg pixel" src="game_assets/bg_wash_corner.png?v=3" alt="">
    <div class="lose-head">
      <h2 id="loseLine">客官这箭法……还是先用饭吧。</h2>
      <p id="washPrompt">连点木盆洗碗 20 次，可再考一局！</p>
    </div>
    <img class="zhanggui-sm pixel" src="game_assets/char_zhanggui.png?v=3" alt="">
    <div class="qte-zone" id="qteZone">
      <div class="basin" id="basin"><span class="wash-label">洗碗</span></div>
      <div class="dots" id="dots"></div>
      <div class="qte-meta">已洗 <b id="qteCount">0</b> / <b id="qteNeed">20</b> 只 ｜ 剩 <b id="qteTime">10.0</b> 秒</div>
      <div class="qte-bar"><i id="qteBar"></i></div>
    </div>
    <div class="pass-banner" id="passBanner" hidden>
      <h3>洗净喽！</h3>
      <p>掌柜：罢了，再给你一局，价码重看！</p>
    </div>
    <div class="gameover" id="gameOver" hidden>
      <h3>掌柜摇了摇头……</h3>
      <p>今夜，就留下洗碗到天亮吧。</p>
      <button class="btn primary big" id="btnAgainLose">再来一局</button>
    </div>
  </section>

  <div class="judge" id="judgeLayer">
    <div class="judge-dim"></div>
    <img class="stamp" id="stampImg" src="game_assets/prop_seal_qi.png?v=3" alt="">
    <div class="judge-text" id="judgeText">验讫…</div>
  </div>

  <div class="flash" id="flash"></div>
  <div class="toast" id="toast"></div>
</div>

<script>
console.log("[麦记穿越局·实时版] build live v1 · 数据源：麦当劳 MCP");
/* 菜单数据内联：单文件运行，无需外部依赖 */
const MENU_ITEMS = [];
const LEVEL_CONFIG = {
  level_id:1, menu_count:6, memorize_time:10, vanish_time:1.5, pick_time:30,
  target_ratio_min:0.8, target_ratio_max:0.95, over_tolerance:0.1,
  retry_count:1, wash_qte_clicks:20, wash_qte_time:10
};
const UI_TEXT = {
  dialog_rule:"客官，这门是单向的。想回百年后，得过了掌柜的『考价关』——菜单只看十息，价码散后，照数凑单，超出一成，便留下来洗碗！",
  memorize_hint:"记住价格！",
  vanish_hint:"价格已散——凑单吧！",
  submit_btn:"提交水单",
  win_perfect:"神了！客官这嘴是算盘做的？",
  win_smart:"好俊的功夫，差点就满环！",
  win_ok:"罢了罢了，图个彩头。",
  lose_line:"客官这箭法……还是先用饭吧。",
  wash_prompt:"连点木盆洗碗 20 次，可再考一局！",
  again_btn:"再来一局"
};
</script>
<script>
(function(){
"use strict";
var CFG = (typeof LEVEL_CONFIG !== "undefined") ? LEVEL_CONFIG : {menu_count:6,memorize_time:10,vanish_time:1.5,pick_time:30,over_tolerance:.1,retry_count:1,wash_qte_clicks:20,wash_qte_time:10};
var TEXT = (typeof UI_TEXT !== "undefined") ? UI_TEXT : {};
var MENU = (typeof MENU_ITEMS !== "undefined") ? MENU_ITEMS : [];

var $ = function(s){ return document.querySelector(s); };
var stage=$("#stage"), grid=$("#grid"), hourglass=$("#hourglass"), timeBar=$("#timeBar"),
    timeText=$("#timeText"), timeBarBox=$("#timeBarBox"), hintBar=$("#hintBar"),
    targetChip=$("#targetChip"), targetText=$("#targetText"), splash=$("#targetSplash"), splashNum=$("#splashNum"),
    billBar=$("#billBar"), billList=$("#billList"), billCount=$("#billCount"), overWarn=$("#overWarn"),
    totalText=$("#totalText"), totalTarget=$("#totalTarget"), btnSubmit=$("#btnSubmit"),
    vignette=$("#vignette"), judgeLayer=$("#judgeLayer"), stampImg=$("#stampImg"), judgeText=$("#judgeText"),
    flash=$("#flash"), doorImg=$("#doorImg"), dialogText=$("#dialogText"), btnKnow=$("#btnKnow"),
    dots=$("#dots"), basin=$("#basin"), qteCount=$("#qteCount"), qteNeed=$("#qteNeed"), qteTime=$("#qteTime"),
    qteBar=$("#qteBar"), passBanner=$("#passBanner"), goPanel=$("#gameOver"), qteZone=$("#qteZone"),
    modernText=$("#modernText"), btnModern=$("#btnModern");

var SCENES={IDLE:"sc-start",DOOR:"sc-door",DIALOG:"sc-dialog",TABLE:"sc-table",WIN:"sc-win",MODERN:"sc-modern",LOSE:"sc-lose"};
var G={ state:"IDLE", timers:[], menu:[], target:0, answer:0, picked:[], counts:{}, pickedTotal:0,
        memDeadline:0, pickDeadline:0, qteDeadline:0, qteClicks:0, retries:CFG.retry_count,
        result:null, wasOver:false, vanishEnd:0 };

function fit(){
  var s=Math.min(window.innerWidth/1280, window.innerHeight/720);
  document.documentElement.style.setProperty("--s", s);
}
window.addEventListener("resize", fit); fit();

function after(ms, fn){
  var id=setTimeout(function(){ var i=G.timers.indexOf(id); if(i>=0) G.timers.splice(i,1); fn(); }, ms);
  G.timers.push(id); return id;
}
function clearTimers(){ G.timers.forEach(clearTimeout); G.timers=[]; }
function scene(id){ Object.keys(SCENES).forEach(function(k){ document.getElementById(SCENES[k]).classList.toggle("active", SCENES[k]===id); }); }
function iconOf(it){ return "game_assets/" + String(it.icon||"").replace(/^icon_/,"item_") + "?v=3"; }
function byId(id){ for(var i=0;i<MENU.length;i++){ if(MENU[i].item_id===id) return MENU[i]; } return null; }
function sample(arr,k){ var a=arr.slice(); for(var i=a.length-1;i>0;i--){ var j=Math.floor(Math.random()*(i+1)); var t=a[i]; a[i]=a[j]; a[j]=t; } return a.slice(0,k); }
function toast(msg){ var t=$("#toast"); t.textContent=msg; t.classList.add("show"); }
function flashOnce(){ flash.classList.remove("go"); void flash.offsetWidth; flash.classList.add("go"); }
function stageShake(){
  var el=document.querySelector(".scene.active");
  if(!el) return;
  el.classList.remove("shaking"); void el.offsetWidth; el.classList.add("shaking");
  after(450, function(){ el.classList.remove("shaking"); });
}

function genTarget(){
  var k=2+Math.floor(Math.random()*3);
  var combo=sample(G.menu,k), T=0;
  combo.forEach(function(it){ T+=it.price; });
  var ratio=CFG.target_ratio_min+Math.random()*(CFG.target_ratio_max-CFG.target_ratio_min);
  var target=Math.round(T*ratio);
  var sorted=G.menu.slice().sort(function(a,b){ return a.price-b.price; });
  var floor=Math.min(sorted[1]?sorted[1].price:sorted[0].price, T);
  if(target<floor) target=floor;
  G.target=target; G.answer=T;
}

/* ---------- table ---------- */
function setTableMode(m, hint){
  var table=$("#sc-table");
  table.setAttribute("data-mode",m);
  hintBar.textContent=hint;
}

function renderMenu(){
  grid.innerHTML="";
  G.menu.forEach(function(it){
    var el=document.createElement("div");
    el.className="dish"; el.dataset.id=it.item_id;
    el.innerHTML='<img class="dish-icon" src="'+iconOf(it)+'" alt="">'+
      '<div class="dish-name">'+it.name_cn+'</div>'+
      '<div class="dish-en">'+it.name_en+'</div>'+
      '<div class="dish-price">'+it.price+'<span>文</span></div>'+
      '<div class="dish-count">×0</div>';
    el.addEventListener("click", function(){ onDishClick(it, el); });
    grid.appendChild(el);
  });
}

function onDishClick(it, el){
  if(G.state!=="PICKING") return;
  G.picked.push(it.item_id);
  G.counts[it.item_id]=(G.counts[it.item_id]||0)+1;
  el.classList.remove("dish-pop"); void el.offsetWidth; el.classList.add("dish-pop");
  syncDishCards(); updateBill();
}

function syncDishCards(){
  grid.querySelectorAll(".dish").forEach(function(el){
    var n=G.counts[el.dataset.id]||0;
    el.classList.toggle("sel", n>0);
    el.querySelector(".dish-count").textContent="×"+n;
  });
}

function updateBill(){
  var total=0; G.picked.forEach(function(id){ var it=byId(id); if(it) total+=it.price; });
  G.pickedTotal=total;
  var order=[];
  G.menu.forEach(function(it){ if(G.counts[it.item_id]>0) order.push(it); });
  if(order.length===0){
    billList.innerHTML='<span class="empty">水单空空如也……快点点菜凑数</span>';
  }else{
    billList.innerHTML="";
    order.forEach(function(it){
      var c=document.createElement("span");
      c.className="chip"; c.dataset.id=it.item_id;
      c.innerHTML=it.name_cn+' <b>×'+G.counts[it.item_id]+'</b>';
      c.addEventListener("click", function(){ removeDish(it.item_id); });
      billList.appendChild(c);
    });
  }
  billCount.textContent="水单 "+G.picked.length+" 件";
  totalText.textContent=total;
  var over=total>G.target*(1+CFG.over_tolerance);
  totalText.classList.toggle("over", over);
  overWarn.hidden=!over;
  if(over && !G.wasOver) stageShake();
  G.wasOver=over;
}

function removeDish(id){
  if(G.state!=="PICKING") return;
  var idx=G.picked.lastIndexOf(id);
  if(idx<0) return;
  G.picked.splice(idx,1);
  G.counts[id]--;
  if(G.counts[id]<=0) delete G.counts[id];
  syncDishCards(); updateBill();
}

/* ---------- HUD ---------- */
function updTimer(remain, total, dangerAt){
  var ratio=Math.max(0, Math.min(1, remain/total));
  timeBar.style.width=(ratio*100)+"%";
  timeText.textContent=remain.toFixed(1)+"s";
  var danger=remain<=dangerAt;
  timeText.classList.toggle("danger",danger);
  timeBarBox.classList.toggle("danger",danger);
  hourglass.classList.toggle("danger",danger);
  vignette.classList.toggle("on",danger);
}

/* ---------- flow ---------- */
function startGame(){
  if(MENU.length<2){ toast("菜单数据缺失：请确认 game_demo.html 文件完整（数据已内联在页面内）。"); return; }
  clearTimers();
  G.state="IDLE"; G.result=null; G.retries=CFG.retry_count;
  G.picked=[]; G.counts={}; G.pickedTotal=0; G.wasOver=false;
  G.menu=sample(MENU, Math.min(CFG.menu_count, MENU.length));
  genTarget();
  enterDoor();
}

function enterDoor(){
  G.state="DOOR"; scene("sc-door");
  doorImg.classList.remove("zoom"); void doorImg.offsetWidth; doorImg.classList.add("zoom");
  after(1900, flashOnce);
  after(2300, enterDialog);
}

var typeState=null;
function typeInto(el, text, onDone){
  el.textContent="";
  typeState={i:0, text:text, el:el, done:false, cb:onDone};
  (function tick(){
    if(!typeState) return;
    typeState.i++;
    el.textContent=text.slice(0,typeState.i);
    if(typeState.i>=text.length){ typeState.done=true; if(onDone) onDone(); return; }
    after(30, tick);
  })();
}

function finishTyping(){
  if(typeState && !typeState.done){
    typeState.i=typeState.text.length; typeState.done=true;
    typeState.el.textContent=typeState.text;
    if(typeState.cb) typeState.cb();
    return true;
  }
  return false;
}

function enterDialog(){
  G.state="DIALOG"; scene("sc-dialog");
  btnKnow.classList.remove("pulse");
  typeInto(dialogText, TEXT.dialog_rule||"", function(){ btnKnow.classList.add("pulse"); });
}
btnKnow.addEventListener("click", function(){
  if(finishTyping()) return;
  enterMemorize();
});
$("#dialogBox").addEventListener("click", function(e){ if(e.target===btnKnow) return; finishTyping(); });

/* ---------- S7.5 modern return ---------- */
var MODERN_LINES=[
  "先生？先生——您要点什么呀？在这点餐机前站了好一会儿，怎么一直在发呆？",
  "噗，看您笑得那么开心，是遇上什么好事啦？您慢慢看，想好了喊我一声就行～"
];
var modernStep=0;
function enterModern(){
  clearTimers();
  G.state="MODERN"; scene("sc-modern");
  modernStep=0;
  btnModern.classList.remove("pulse");
  btnModern.textContent="…";
  typeInto(modernText, MODERN_LINES[0], function(){ btnModern.classList.add("pulse"); });
}
btnModern.addEventListener("click", function(){
  if(finishTyping()) return;
  if(modernStep<1){
    modernStep=1;
    btnModern.classList.remove("pulse");
    btnModern.textContent="再来一局";
    typeInto(modernText, MODERN_LINES[1], function(){ btnModern.classList.add("pulse"); });
  }else{
    startGame();
  }
});
$("#modernBox").addEventListener("click", function(e){ if(e.target===btnModern) return; finishTyping(); });


function enterMemorize(){
  clearTimers();
  G.state="MEMORIZE"; scene("sc-table");
  setTableMode("memorize", TEXT.memorize_hint||"记住价格！");
  G.memDeadline=performance.now()+CFG.memorize_time*1000;
  splash.hidden=true; targetChip.hidden=true; billBar.hidden=true;
  overWarn.hidden=true; vignette.classList.remove("on");
  renderMenu(); syncDishCards(); updateBill();
  updTimer(CFG.memorize_time, CFG.memorize_time, 3);
}

function startVanish(){
  G.state="VANISH";
  setTableMode("vanish", TEXT.vanish_hint||"价格已散——凑单吧！");
  vignette.classList.remove("on");
  hourglass.classList.remove("danger"); timeBarBox.classList.remove("danger"); timeText.classList.remove("danger");
  grid.querySelectorAll(".dish-price").forEach(function(p){ p.classList.add("dissolving"); });
  grid.querySelectorAll(".dish").forEach(function(card){ spawnDust(card); });
  splashNum.textContent=G.target;
  after(420, function(){ splash.hidden=false; splash.classList.remove("show"); void splash.offsetWidth; splash.classList.add("show"); });
  G.vanishEnd=performance.now()+CFG.vanish_time*1000+700;
}

function spawnDust(card){
  for(var i=0;i<14;i++){
    (function(){
      var d=document.createElement("i");
      d.className="dust";
      var sz=2+Math.floor(Math.random()*4);
      d.style.width=sz+"px"; d.style.height=sz+"px";
      d.style.left=(30+Math.random()*40)+"%";
      d.style.top=(72+Math.random()*16)+"%";
      d.style.background=Math.random()<.5?"#f7c948":"#fff2c0";
      card.appendChild(d);
      var dx=(Math.random()-.5)*110, dy=-(20+Math.random()*80);
      var anim=d.animate(
        [{transform:"translate(0,0)",opacity:1},{transform:"translate("+dx+"px,"+dy+"px)",opacity:0}],
        {duration:500+Math.random()*800, delay:Math.random()*350, easing:"steps(8)", fill:"forwards"});
      anim.onfinish=function(){ d.remove(); };
    })();
  }
}

function enterPicking(){
  if(G.state!=="VANISH") return;
  G.state="PICKING";
  setTableMode("picking", "价码已散 · 凭记忆凑单");
  targetChip.hidden=false; targetText.textContent=G.target;
  totalTarget.textContent=G.target;
  splash.hidden=true;
  billBar.hidden=false;
  G.wasOver=false; G.pickDeadline=performance.now()+CFG.pick_time*1000;
  updateBill();
  updTimer(CFG.pick_time, CFG.pick_time, 5);
}

function submitBill(){
  if(G.state!=="PICKING") return;
  G.state="JUDGE";
  doJudge();
}

function doJudge(){
  clearTimers();
  var total=G.pickedTotal, t=G.target, limit=t*(1+CFG.over_tolerance);
  var success=G.picked.length>0 && total<=limit;
  var rate=t>0?(total-t)/t:0;
  var grade="FAIL";
  if(success) grade = rate<=0?"PERFECT": (rate<=0.05?"SMART":"OK");
  G.result={success:success, grade:grade, rate:rate, total:total, target:t};
  judgeLayer.classList.add("show");
  judgeText.textContent="验讫…"; judgeText.classList.remove("bad");
  stampImg.classList.remove("drop"); void stampImg.offsetWidth; stampImg.classList.add("drop");
  after(1000, function(){
    judgeText.textContent=success?"准！":"逾了！";
    judgeText.classList.toggle("bad",!success);
    flashOnce(); stageShake();
  });
  after(2000, function(){
    judgeLayer.classList.remove("show");
    if(success) showWin();
    else if(G.retries>0) startWash();
    else showGameOver();
  });
}

var GRADE_META={
  PERFECT:{title:"🏹 神算子", line:TEXT.win_perfect||"神了！", discount:"全单免费"},
  SMART:{title:"🎯 精明客", line:TEXT.win_smart||"好俊的功夫！", discount:"8 折"},
  OK:{title:"🟡 糊涂客", line:TEXT.win_ok||"罢了罢了，图个彩头。", discount:"原价放行"}
};

function showWin(){
  G.state="WIN"; scene("sc-win");
  var r=G.result, meta=GRADE_META[r.grade]||GRADE_META.OK;
  $("#gradeTitle").textContent=meta.title;
  $("#gradeLine").textContent=meta.line;
  $("#discountTag").textContent=meta.discount;
  $("#wPicked").textContent=r.total+" 文";
  $("#wTarget").textContent=r.target+" 文";
  var sign=r.rate>0?"+":"";
  $("#wRate").textContent=sign+(r.rate*100).toFixed(1)+"%";
  confetti();
}

function confetti(){
  var box=$("#confetti"); box.innerHTML="";
  var colors=["#f7c948","#ff4fd8","#39e6ff","#fff2c0","#ff8a3d"];
  for(var i=0;i<44;i++){
    var s=document.createElement("span");
    s.style.left=(Math.random()*100)+"%";
    s.style.background=colors[i%colors.length];
    s.style.width=s.style.height=(6+Math.random()*6)+"px";
    s.style.animationDuration=(1.6+Math.random()*1.6)+"s";
    s.style.animationDelay=(Math.random()*.8)+"s";
    box.appendChild(s);
  }
  after(3600, function(){ box.innerHTML=""; });
}

function startWash(){
  G.retries--;
  G.state="WASH"; scene("sc-lose");
  $("#loseLine").textContent=TEXT.lose_line||"客官这箭法……还是先用饭吧。";
  $("#washPrompt").textContent=TEXT.wash_prompt||"连点木盆洗碗 20 次，可再考一局！";
  $("#washPrompt").hidden=false;
  passBanner.hidden=true; goPanel.hidden=true; qteZone.hidden=false;
  G.qteClicks=0; qteNeed.textContent=CFG.wash_qte_clicks; qteCount.textContent="0";
  renderDots();
  qteTime.textContent=CFG.wash_qte_time.toFixed(1);
  qteBar.style.width="100%";
  G.qteDeadline=performance.now()+CFG.wash_qte_time*1000;
}

function renderDots(){
  dots.innerHTML="";
  for(var i=0;i<CFG.wash_qte_clicks;i++){
    var d=document.createElement("i");
    if(i<G.qteClicks) d.className="on";
    dots.appendChild(d);
  }
}

basin.addEventListener("click", function(){
  if(G.state!=="WASH") return;
  G.qteClicks++;
  qteCount.textContent=G.qteClicks;
  var ds=dots.children;
  if(ds[G.qteClicks-1]) ds[G.qteClicks-1].className="on";
  var r=document.createElement("span");
  r.className="ripple";
  basin.appendChild(r);
  r.animate([{transform:"scale(.4)",opacity:.9},{transform:"scale(2.6)",opacity:0}],{duration:420,easing:"ease-out"}).onfinish=function(){ r.remove(); };
  var b=document.createElement("span");
  b.className="bubble";
  var bs=4+Math.random()*8;
  b.style.width=b.style.height=bs+"px";
  b.style.left=(24+Math.random()*52)+"%";
  b.style.top=(30+Math.random()*40)+"%";
  basin.appendChild(b);
  b.animate([{transform:"translateY(6px)",opacity:.95},{transform:"translateY(-26px)",opacity:0}],{duration:520,easing:"steps(7)"}).onfinish=function(){ b.remove(); };
  if(G.qteClicks>=CFG.wash_qte_clicks) washPass();
});

function washPass(){
  G.state="WASH_DONE";
  passBanner.hidden=false;
  after(1500, function(){
    passBanner.hidden=true;
    enterMemorize();
  });
}

function washFail(){
  if(G.state!=="WASH") return;
  showGameOver();
}

function showGameOver(){
  G.state="GAMEOVER";
  scene("sc-lose");
  $("#loseLine").textContent=TEXT.lose_line||"客官这箭法……还是先用饭吧。";
  $("#washPrompt").hidden=true;
  qteZone.hidden=true;
  passBanner.hidden=true;
  goPanel.hidden=false;
}

$("#btnStart").addEventListener("click", startGame);
$("#btnGoHome").addEventListener("click", enterModern);
$("#btnAgainLose").addEventListener("click", startGame);
btnSubmit.addEventListener("click", submitBill);

/* ---------- master loop ---------- */
function loop(now){
  if(G.state==="MEMORIZE"){
    var r=Math.max(0,(G.memDeadline-now)/1000);
    updTimer(r, CFG.memorize_time, 3);
    if(r<=0) startVanish();
  }else if(G.state==="VANISH"){
    if(now>=G.vanishEnd) enterPicking();
  }else if(G.state==="PICKING"){
    var p=Math.max(0,(G.pickDeadline-now)/1000);
    updTimer(p, CFG.pick_time, 5);
    if(p<=0) submitBill();
  }else if(G.state==="WASH"){
    var w=Math.max(0,(G.qteDeadline-now)/1000);
    qteTime.textContent=w.toFixed(1);
    qteBar.style.width=((w/CFG.wash_qte_time)*100)+"%";
    if(w<=0) washFail();
  }
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);

(function assetProbe(){
  var probe=new Image();
  probe.onerror=function(){ toast("素材未找到：请把 game_assets 文件夹与 game_demo.html 放在同一目录，再刷新页面。"); };
  probe.src="game_assets/bg_start_modern.png?v=3";
})();
})();
</script>
<script>
/* 实时数据层：由后端 /api/menu 注入麦当劳 MCP 实时菜单 */
(function(){
  "use strict";
  var statusEl = document.getElementById("liveStatus");
  var busy = false;
  function setStatus(t){ statusEl.textContent = t; }
  window.mcdLoadLive = function(force){
    if (busy) return Promise.resolve(false);
    busy = true;
    setStatus("📡 正在连接麦当劳 MCP 拉取实时菜单…");
    return fetch("/api/menu" + (force ? "?force=1" : ""), {cache:"no-store"})
      .then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function(j){
        if(!j.ok) throw new Error(j.error || "接口异常");
        MENU_ITEMS.length = 0;
        for (var i = 0; i < j.items.length; i++) MENU_ITEMS.push(j.items[i]);
        var tag = j.source === "mcp" ? "实时" : "快照降级";
        statusEl.textContent = "📡 麦当劳 MCP · " + j.store.name + " · " + j.updated_at + " 更新（" + tag + "）· 点此刷新";
        return true;
      })
      .catch(function(e){
        statusEl.textContent = "⚠ 实时菜单获取失败（" + e.message + "）· 点此重试";
        return false;
      })
      .then(function(ok){ busy = false; return ok; });
  };
  statusEl.addEventListener("click", function(){ window.mcdLoadLive(true); });
  document.addEventListener("click", function(e){
    var t = e.target;
    while (t && t !== document) { if (t.id === "btnStart") break; t = t.parentNode; }
    if (t && t.id === "btnStart" && MENU_ITEMS.length < 2) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (busy) { setStatus("📡 实时菜单加载中，稍候再点…"); }
      else { setStatus("⚠ 菜单未就绪，正在重新拉取…"); window.mcdLoadLive(); }
    }
  }, true);
  window.mcdLoadLive();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("推门 · 麦记穿越局 · 实时版  ->  http://127.0.0.1:" + str(PORT))
    print("数据源：麦当劳 MCP " + MCD_URL + " | 门店搜索：" + CITY + "/" + KEYWORD)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
