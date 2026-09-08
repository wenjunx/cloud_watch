#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端「想法」监控（GitHub Actions 版）

特点：
  - 免登录：只走 /api/v4/members/{token}/pins，不需要知乎 cookie，零维护
  - 无外部依赖：只用标准库 urllib，Actions 里不用 pip install，跑得更快
  - 状态存 seen.json（由 Actions cache 在每次运行间保留）
  - 首次运行只建基线不推送，避免把历史想法当成新动态轰炸

环境变量：
  SERVERCHAN_KEY  必填，Server酱 SendKey
  ZHIHU_TOKEN     选填，默认 xiao-peng-61-47
  SEEN_PATH       选填，默认 data/seen.json
  DRY_RUN         设为 1 时只打印不真推送（本地调试用）
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TOKEN = os.environ.get("ZHIHU_TOKEN", "xiao-peng-61-47")
KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
SEEN_PATH = Path(os.environ.get("SEEN_PATH", "data/seen.json"))
KEEP_IDS = 300
DAILY_LIMIT = 5  # Server酱免费版每天 5 条


def log(msg):
    print(f"[{datetime.now(CST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- 网络 ----------
def _get(url, data=None, timeout=20):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://www.zhihu.com/people/{TOKEN}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def fetch_pins(limit=20):
    url = f"https://www.zhihu.com/api/v4/members/{TOKEN}/pins?limit={limit}&offset=0"
    status, text = _get(url)
    if status != 200:
        raise RuntimeError(f"想法接口 HTTP {status}")
    js = json.loads(text)
    if js.get("error"):
        raise RuntimeError(f"想法接口业务错误: {js['error']}")
    out = []
    for it in js.get("data") or []:
        pid = str(it.get("id") or "")
        if not pid:
            continue
        blocks = it.get("content") or []
        text_ = ("".join(b.get("content", "") for b in blocks if isinstance(b, dict))
                 if isinstance(blocks, list) else str(blocks))
        out.append({
            "id": pid,
            "created": it.get("created") or it.get("updated") or 0,
            "title": clean(it.get("excerpt_title") or ""),
            "excerpt": clean(text_),
            "like": it.get("like_count"),
            "comment": it.get("comment_count"),
            "url": f"https://www.zhihu.com/pin/{pid}",
        })
    return out


def clean(s, limit=260):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()[:limit]


# ---------- 推送 ----------
def build_body(items):
    parts = []
    for it in items:
        t = time.strftime("%m-%d %H:%M", time.localtime(it["created"])) if it["created"] else ""
        seg = [f"### 发布了想法 · {t}"]
        if it["title"]:
            seg.append(f"**{it['title']}**")
        if it["excerpt"]:
            seg.append(it["excerpt"][:220])
        meta = []
        if it["like"] is not None:
            meta.append(f"赞同 {it['like']}")
        if it["comment"] is not None:
            meta.append(f"评论 {it['comment']}")
        if meta:
            seg.append(f"> {' / '.join(meta)}")
        seg.append(f"[打开原文]({it['url']})")
        parts.append("\n\n".join(seg))
    return (f"**派大星皮皮** 有新想法（{len(items)} 条）\n\n---\n\n"
            + "\n\n---\n\n".join(parts))


def push(title, body):
    """返回 (ok, 说明)"""
    if DRY_RUN:
        print("---- DRY RUN 推送内容 ----")
        print(f"标题: {title}\n{body}\n-------------------------")
        return True, "dry-run"
    if not KEY:
        return False, "缺少 SERVERCHAN_KEY"
    url = f"https://sctapi.ftqq.com/{KEY}.send"
    status, text = _get(url, {"title": title[:32], "desp": body})
    ok = status == 200 and '"code":0' in text.replace(" ", "")
    return ok, f"HTTP {status} {text[:120]}"


# ---------- 状态 ----------
def load_state():
    if not SEEN_PATH.exists():
        return None
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(state):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main():
    today = datetime.now(CST).strftime("%Y-%m-%d")
    items = fetch_pins()
    log(f"拉到 {len(items)} 条想法")
    if not items:
        log("[!] 想法接口返回空，可能是网络或风控，本轮结束")
        return

    state = load_state()

    # 首次：只建基线
    if state is None:
        save_state({"ids": [i["id"] for i in items], "date": today, "sent": 0})
        log(f"[√] 基线已建立（{len(items)} 条），不推送。之后的新想法才会提醒你。")
        return

    if state.get("date") != today:
        state["date"], state["sent"] = today, 0

    known = set(state.get("ids", []))
    fresh = [i for i in items if i["id"] not in known]
    if not fresh:
        log("无新想法")
        save_state({"ids": [i["id"] for i in items][:KEEP_IDS],
                    "date": today, "sent": state.get("sent", 0)})
        return

    fresh.sort(key=lambda x: x["created"])
    if state.get("sent", 0) >= DAILY_LIMIT:
        log(f"[!] 今日免费额度已用尽（{DAILY_LIMIT} 条），本条跳过")
    else:
        title = f"知乎·派大星皮皮 更新了{len(fresh)}条"
        ok, note = push(title, build_body(fresh))
        log(f"[{'√' if ok else 'x'}] 推送 {len(fresh)} 条 -> {note}")
        if ok and not DRY_RUN:
            state["sent"] = state.get("sent", 0) + 1

    ids = [i["id"] for i in items]
    merged = list(dict.fromkeys(ids + list(known)))[:KEEP_IDS]
    save_state({"ids": merged, "date": today, "sent": state.get("sent", 0)})
    log(f"状态已更新（今日已推 {state.get('sent', 0)}/{DAILY_LIMIT} 条）")


if __name__ == "__main__":
    sys.exit(0 if main() is None else 0)
