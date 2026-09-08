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
DRY_RUN = os.environ.get("DRY_RUN", "0").strip().lower() in ("1", "true", "yes")
# 在 GitHub 上手动运行时可勾选「强制推送一条」，用来验证微信能不能收到
# 注意：GitHub 的 boolean 输入传进来是字符串 "true"，所以要宽松匹配
TEST_PUSH = os.environ.get("TEST_PUSH", "0").strip().lower() in ("1", "true", "yes", "on")
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


def do_push(fresh, state):
    """推送并维护当日计数。返回 (ok, note)，ok 为 None 表示因额度被跳过。"""
    if state.get("sent", 0) >= DAILY_LIMIT:
        return None, f"今日免费额度已用尽（{DAILY_LIMIT} 条）"
    title = f"知乎·派大星皮皮 更新了{len(fresh)}条"
    ok, note = push(title, build_body(fresh))
    if ok and not DRY_RUN:
        state["sent"] = state.get("sent", 0) + 1
    return ok, note


def main():
    # GitHub 注入的环境变量：GITHUB_EVENT_NAME=schedule 表示定时自动跑，
    # workflow_dispatch 表示有人在网页上手动点的。用来确认定时是否真的生效。
    event = os.environ.get("GITHUB_EVENT_NAME", "本地运行")
    run_no = os.environ.get("GITHUB_RUN_NUMBER", "-")
    trigger = {"schedule": "定时自动",
               "workflow_dispatch": "手动点击",
               "本地运行": "本地运行"}.get(event, event)
    log(f"=== 第 {run_no} 次运行 | 触发方式：{trigger}（{event}） | 用户 {TOKEN} | 推送KEY "
        f"{'已设置' if KEY else '【未设置】'} | TEST_PUSH={TEST_PUSH} | DRY_RUN={DRY_RUN}")
    if not KEY and not DRY_RUN:
        log("[!] 没有拿到 SERVERCHAN_KEY，无法推送。"
            "请到仓库 Settings → Secrets and variables → Actions 里添加同名密钥。")

    today = datetime.now(CST).strftime("%Y-%m-%d")
    try:
        items = fetch_pins()
    except Exception as e:
        log(f"[x] 抓取失败: {e}")
        return
    log(f"拉到 {len(items)} 条想法")
    if not items:
        log("[!] 想法接口返回空，可能是网络或风控，本轮结束")
        return
    newest = max(items, key=lambda x: x["created"])
    log(f"    最新一条：{time.strftime('%Y-%m-%d %H:%M', time.localtime(newest['created']))}"
        f" | {(newest['excerpt'] or newest['title'])[:30]}")

    # 手动测试：不管有没有新动态，强推一条最新的，用来验证通道
    if TEST_PUSH:
        ok, note = push("知乎监控·云端测试", build_body(items[:1]))
        log(f"[{'√' if ok else 'x'}] 测试推送 -> {note}")
        log("    没收到就展开上面这行看返回内容；测试推送不改动已见列表，不会重复推。")
        return

    state = load_state()
    now_ts = time.time()

    # 首次运行 / 缓存丢失：建基线，只补推最近 24 小时内的（避免把历史想法全推给你）
    if state is None:
        new_state = {"ids": [i["id"] for i in items][:KEEP_IDS], "date": today, "sent": 0}
        log(f"[√] 基线已建立（{len(items)} 条）")
        recent = [i for i in items if i["created"] and now_ts - i["created"] <= 86_400]
        if recent:
            ok, note = do_push(recent, new_state)
            log(f"[{'√' if ok else 'x'}] 首次运行，补推最近 24h 内的 {len(recent)} 条 -> {note}")
        else:
            log("    最近 24 小时内没有想法，不推送。之后发新的才会提醒你。")
        save_state(new_state)
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
    ok, note = do_push(fresh, state)
    if ok:
        log(f"[√] 已推送 {len(fresh)} 条 -> {note}")
        state["ids"] = list(dict.fromkeys([i["id"] for i in items] + list(known)))[:KEEP_IDS]
    elif ok is None:
        log(f"[!] {note}，本条先不标记，次日额度重置后自动补推")
    else:
        log(f"[x] 推送失败 -> {note}，不标记为已读，下轮自动重试")

    save_state(state)
    log(f"状态已更新（今日已推 {state.get('sent', 0)}/{DAILY_LIMIT} 条）")


if __name__ == "__main__":
    sys.exit(0 if main() is None else 0)
