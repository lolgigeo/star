#!/usr/bin/env python3
"""公众号后台（mp.weixin.qq.com）数据源：用号主登录态拉文章列表。

原理：登录公众号后台后，携带 cookie + token 调用
  - searchbiz        按名称搜索公众号，拿 fakeid
  - appmsg/list_ex   按 fakeid 分页拉取任意公众号的文章列表
以此取代取阅等第三方 RSS 服务做新文章发现（正文抓取仍由 sync.py 本地完成）。

凭据文件 credentials/mp_credentials.json（已 gitignore，不入库）：
  {
    "cookie": "<浏览器复制的完整 Cookie 请求头>",
    "token": "<登录后地址栏 token= 后面的数字>",
    "accounts": [
      {"name": "文森和他的孩子们", "fakeid": "Mz...", "own": true, "username": "gh_xxx"}
    ]
  }

一份登录态可以管理多个公众号（accounts 配多条 own 记录）：会话上下文绑定在
cookie 的 slave_bizuin 上，后台接口只返回当前号的列表；抓取前按账号自动调
switchacct 切换并回存 cookie/token，多仓库共享同一份凭据时谁用谁切。

登录态几天会过期，症状是所有请求 ret!=0；重新从浏览器复制覆盖
cookie/token 两个字段即可（accounts 不用动）。

用法：
  .venv/bin/python tools/mp_list.py check            # 验证登录态是否有效
  .venv/bin/python tools/mp_list.py search <名称>     # 搜公众号，打印 fakeid
  .venv/bin/python tools/mp_list.py add <名称>        # 把搜索结果第一条加入 accounts
  .venv/bin/python tools/mp_list.py fetch            # 手动拉一次所有账号的最新文章
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
# 测试可用 MP_CRED_PATH 指到临时文件，避免动到真实凭据
CRED_PATH = Path(os.environ.get("MP_CRED_PATH") or (ROOT / "credentials" / "mp_credentials.json"))

PAGE_COUNT = 5   # list_ex 单页上限 5 条，日常增量足够；历史补齐仍走 JSON 导出
OWN_PAGE_COUNT = 10   # appmsgpublish 单页条数（发布记录数，一记录可含多篇）
OWN_MAX_PAGES = 2     # 最多翻 2 页（20 条发布记录），日常增量足够
ACCOUNT_DELAY = 1.5  # 多账号之间间隔，别贴着频控线
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 微信后台请求一律本机直连：走系统/环境代理会改变出口 IP（微信风控敏感），且本地
# 代理客户端（clash 等）时好时坏，2026-11-26 系统代理 503 曾把夜间同步整个炸穿
NO_PROXY = {"http": None, "https": None}

RET_MSG = {
    -100: "未登录或登录态已失效",
    200002: "登录态已失效",
    200003: "登录态已失效（200003）",
    200012: "登录态已失效",
    200013: "触发频率限制",
    65530: "登录态已失效（65530）",
}
AUTH_RETS = {-100, 200002, 200003, 200012, 65530}  # 登录态失效类错误码

log = logging.getLogger("mp")


class MpError(Exception):
    """MP 接口调用失败（凭据缺失/频率限制等），可按账号跳过。"""


class MpAuthError(MpError):
    """登录态失效，重试其他账号也没意义，应整体跳过 MP 源。"""


# ---------------- 凭据 ----------------

def load_credentials() -> dict:
    if not CRED_PATH.exists():
        where = CRED_PATH.relative_to(ROOT) if CRED_PATH.is_relative_to(ROOT) else CRED_PATH
        raise MpError(
            f"缺少凭据文件 {where}："
            "先登录 mp.weixin.qq.com 复制 cookie 和 token（步骤见 README）"
        )
    cred = json.loads(CRED_PATH.read_text())
    if not cred.get("cookie") or not cred.get("token"):
        raise MpError("凭据文件缺少 cookie 或 token 字段")
    return cred


def save_credentials(cred: dict) -> None:
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRED_PATH.write_text(json.dumps(cred, ensure_ascii=False, indent=2) + "\n")


# ---------------- 跨进程互斥 ----------------
# 多仓库共享同一份凭据（同一登录态）时，两个同步进程若并发跑，switchacct 会互相
# 覆盖上下文、回存互相踩踏（2026-11-27 08:13 两仓库定时任务撞车实测）。用 flock
# 锁在共享凭据目录里串行化；进程崩溃时内核自动释放锁，不会留死锁。

LOCK_PATH = CRED_PATH.parent / ".mp_sync.lock"
LOCK_TIMEOUT = 900  # 最多等 15 分钟（另一仓库抓多篇文章的情况）


@contextmanager
def sync_lock():
    fh = open(LOCK_PATH, "w")
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                fh.close()
                raise MpError(f"另一仓库的同步持有 MP 锁超过 {LOCK_TIMEOUT}s，本次跳过 MP 源")
            time.sleep(5)
    try:
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


# ---------------- 一份登录态管多个号：切换上下文 ----------------
# 登录态绑定在「当前公众号上下文」上（cookie 的 slave_bizuin），后台接口只返回
# 当前号的已发表列表。一个微信管理多个公众号时，用 switchacct 接口切换上下文：
# POST switchacct?action=switch（带目标号 username）→ 服务端换发 cookie →
# GET / 302 找回新 token。多仓库共享同一份凭据文件时，谁用谁切，用完回存。

def parse_cookie_header(header: str) -> dict[str, str]:
    """把 Cookie 请求头解析成 {name: value}。"""
    out: dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if part and "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def fakeid_to_bizuin(fakeid: str) -> str:
    """fakeid 即 bizuin 数字的 base64（MzIwMjM0NDM1Nw== ↔ 3202344357）。"""
    try:
        return base64.b64decode(fakeid).decode()
    except Exception:
        return fakeid


def cookie_bizuin(cookie: str) -> str | None:
    """当前会话上下文对应的公众号 bizuin（cookie 的 slave_bizuin）。"""
    m = re.search(r"slave_bizuin=(\d+)", cookie)
    return m.group(1) if m else None


def get_acct_list(cred: dict) -> list[dict]:
    """列出本登录态可切换的公众号（biz_list.list 的元素可能是 JSON 字符串）。"""
    data = api_get("switchacct", {"action": "get_acct_list"}, cred)
    bl = data.get("biz_list") or {}
    items = bl.get("list") if isinstance(bl, dict) else (bl or [])
    out: list[dict] = []
    for raw in items or []:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        if isinstance(raw, dict):
            out.append(raw)
    return out


def refresh_token(cred: dict) -> None:
    """切换后 token 随上下文变化：带 cookie GET / 会 302 到带 token= 的 home 地址。"""
    try:
        resp = requests.get("https://mp.weixin.qq.com/",
                            headers={"User-Agent": UA, "Cookie": cred["cookie"]},
                            timeout=30, allow_redirects=False, proxies=NO_PROXY)
    except requests.RequestException as exc:
        raise MpError(f"找回 token 失败（本机直连 mp.weixin.qq.com 不通）：{exc}") from exc
    m = re.search(r"token=(\d+)", resp.headers.get("Location", "") or "")
    if not m:
        raise MpAuthError("切换账号后无法找回 token（GET / 未跳转到 home，登录态可能已失效）")
    cred["token"] = m.group(1)


def switch_account(cred: dict, acc: dict) -> dict:
    """把会话上下文切到 acc 对应的公众号，更新并回存 cookie/token。"""
    name = acc.get("name") or "?"
    # token 绑定当前上下文：cookie 被手动更新（换号/重新登录）后文件里的旧 token
    # 属于旧上下文，直接拿去 switch 会 ret=200040，所以切换前先按 cookie 找回 token
    refresh_token(cred)
    session = requests.Session()
    session.trust_env = False  # 强制直连，绕过系统/环境代理
    session.cookies.update(parse_cookie_header(cred["cookie"]))
    try:
        resp = session.post(
            "https://mp.weixin.qq.com/cgi-bin/switchacct",
            params={"action": "switch", "token": cred["token"], "lang": "zh_CN", "f": "json", "ajax": 1},
            data={"username": acc["username"]},
            headers={"User-Agent": UA,
                     "Referer": "https://mp.weixin.qq.com/cgi-bin/frame?t=frame"},
            timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        # 登录态失效时该接口常返回登录页 HTML，json() 会抛 ValueError
        raise MpAuthError(f"switchacct（{name}）调用失败，登录态可能已失效：{exc}") from exc
    ret = (payload.get("base_resp") or {}).get("ret")
    if ret != 0:
        msg = {201501: "未授权使用切换账号能力，请退出后重新扫码登录"}.get(ret, "")
        raise MpError(f"switchacct（{name}）：ret={ret}{msg}")
    cred["cookie"] = "; ".join(f"{k}={v}" for k, v
                               in requests.utils.dict_from_cookiejar(session.cookies).items())
    refresh_token(cred)
    save_credentials(cred)
    log.info("MP 已切换到公众号「%s」（bizuin=%s）", name, cookie_bizuin(cred["cookie"]))
    return cred


def ensure_own_context(cred: dict, acc: dict) -> dict:
    """own 账号抓取前确认会话上下文在自己号上，不在就切过去（多号共用一份凭据）。"""
    target = fakeid_to_bizuin(acc.get("fakeid") or "")
    if not target or cookie_bizuin(cred["cookie"]) == target:
        return cred
    name = acc.get("name") or "?"
    username = acc.get("username")
    if not username:
        for b in get_acct_list(cred):
            if b.get("nickname") == name and b.get("username"):
                username = b["username"]
                acc["username"] = username  # 记下来，下次不用再查
                break
    if not username:
        raise MpError(f"无法确定「{name}」的切换 username（get_acct_list 无匹配昵称，"
                      "可在凭据 accounts 里手填）")
    return switch_account(cred, acc)


# ---------------- 接口 ----------------

def api_get(cgi: str, params: dict, cred: dict) -> dict:
    """调用 mp.weixin.qq.com 的 cgi 接口并校验 base_resp.ret。"""
    full = dict(params)
    full.setdefault("token", cred["token"])
    full.setdefault("lang", "zh_CN")
    full.setdefault("f", "json")
    full.setdefault("ajax", 1)
    try:
        resp = requests.get(
            f"https://mp.weixin.qq.com/cgi-bin/{cgi}",
            params=full,
            headers={
                "User-Agent": UA,
                "Referer": "https://mp.weixin.qq.com/cgi-bin/frame?t=frame",
                "Cookie": cred["cookie"],
            },
            timeout=30,
            proxies=NO_PROXY,
        )
    except requests.RequestException as exc:
        # 网络不通（本地代理挂了等）按可跳过错误处理，绝不中断夜间定时同步
        raise MpError(f"{cgi} 网络错误（本机直连失败）:{exc}") from exc
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise MpAuthError(f"{cgi} 返回非 JSON（登录态大概率已失效）") from exc
    ret = (data.get("base_resp") or {}).get("ret")
    if ret != 0:
        msg = RET_MSG.get(ret, f"ret={ret}")
        if ret in AUTH_RETS:
            raise MpAuthError(f"{cgi}：{msg}，请重新获取 cookie/token")
        raise MpError(f"{cgi}：{msg}")
    return data


def searchbiz(query: str, cred: dict) -> list[dict]:
    """按名称/微信号搜索公众号。"""
    data = api_get("searchbiz", {
        "action": "search_biz",
        "begin": 0,
        "count": 5,
        "query": query,
    }, cred)
    return data.get("list") or []


def list_ex(fakeid: str, cred: dict, begin: int = 0) -> list[dict]:
    """拉取某公众号文章列表第 begin 页（每页 PAGE_COUNT 条）。"""
    data = api_get("appmsg", {
        "action": "list_ex",
        "begin": begin,
        "count": PAGE_COUNT,
        "fakeid": fakeid,
        "type": 9,
        "query": "",
    }, cred)
    return data.get("app_msg_list") or []


def fetch_own(cred: dict, acc: dict) -> list[tuple[str, dict]]:
    """号主自己账号的已发表文章：appmsgpublish 接口（后台「已发表」页同款）。

    相比 list_ex 的优势：无 fakeid 频控、最多可翻页。返回与 fetch_all 相同的
    [(名称, 条目)] 契约，条目归一化成 list_ex 风格字段；content_url 是 /s/ 短链，
    由 sync.py 侧解析成规范链接（解析不出 sn 时用 biz+mid+idx 兜底）。
    一份登录态管多个号：抓取前先确认上下文在自己号上（不对则 switchacct 切换）。
    """
    name = acc.get("name") or "?"
    cred = ensure_own_context(cred, acc)
    out: list[tuple[str, dict]] = []
    for begin in range(0, OWN_MAX_PAGES * OWN_PAGE_COUNT, OWN_PAGE_COUNT):
        data = api_get("appmsgpublish", {
            "sub": "list",
            "search_field": "null",
            "begin": begin,
            "count": OWN_PAGE_COUNT,
            "query": "",
            "type": "101_1",
        }, cred)
        page = json.loads(data.get("publish_page") or "{}")
        records = page.get("publish_list") or []
        for rec in records:
            info = rec.get("publish_info")
            info = json.loads(info) if isinstance(info, str) else info or {}
            sent_time = int((info.get("sent_info") or {}).get("time") or 0)
            for art in info.get("appmsg_info") or []:
                link = art.get("content_url") or ""
                if not link:
                    continue
                out.append((name, {
                    "title": (art.get("title") or "").strip(),
                    "link": link,
                    "create_time": sent_time,
                    "digest": (art.get("digest") or "").strip(),
                    "cover": art.get("cover") or art.get("pic_cdn_url_235_1") or "",
                    "appmsgid": art.get("appmsgid"),
                    "itemidx": art.get("itemidx"),
                    "biz": acc.get("fakeid"),
                }))
        if len(records) < OWN_PAGE_COUNT:
            break
    log.info("MP %s（appmsgpublish）返回 %d 条", name, len(out))
    return out


def fetch_all(name_filter: str | None = None) -> list[tuple[str, dict]]:
    """拉取凭据里公众号的最新文章，返回 [(公众号名, 条目)]。

    accounts 里标了 "own": true 的号走 appmsgpublish（自己号的发表记录，
    不受 list_ex 频控）；其余走 list_ex。一份登录态管多个号时，own 账号抓取前
    会自动 switchacct 切换上下文；全程持有跨进程锁，多仓库并发时串行执行。
    name_filter：只拉指定名称的号（多仓库共享凭据时各仓库传自己的号，
    避免空拉别人的列表、也避免别的号失败连带中止本仓库的源）。
    登录态失效抛 MpAuthError（上层整体跳过 MP 源）；单个账号失败只告警继续。
    """
    with sync_lock():
        return _fetch_all_locked(name_filter)


def _fetch_all_locked(name_filter: str | None = None) -> list[tuple[str, dict]]:
    cred = load_credentials()
    accounts = cred.get("accounts") or []
    if not accounts:
        raise MpError("凭据文件未配置 accounts（用 mp_list.py add <名称> 添加）")
    out: list[tuple[str, dict]] = []
    own_names: set[str] = set()
    for acc in accounts:
        if acc.get("own"):
            name = acc.get("name") or "?"
            if name_filter and name != name_filter:
                continue
            if own_names:
                time.sleep(ACCOUNT_DELAY)
            own_names.add(name)
            try:
                out.extend(fetch_own(cred, acc))
            except MpAuthError:
                raise
            except MpError as exc:
                log.warning("MP %s 拉取失败，跳过：%s", name, exc)
    others = [acc for acc in accounts if not acc.get("own")]
    for i, acc in enumerate(others):
        fakeid = acc.get("fakeid")
        name = acc.get("name") or fakeid or "?"
        if name_filter and name != name_filter:
            continue
        if not fakeid:
            log.warning("MP 账号缺少 fakeid，跳过：%s", name)
            continue
        if i:
            time.sleep(ACCOUNT_DELAY)
        try:
            items = list_ex(fakeid, cred)
        except MpAuthError:
            raise  # 登录态失效，换账号也没用，整体放弃
        except MpError as exc:
            log.warning("MP %s 拉取失败，跳过：%s", name, exc)
            continue
        log.info("MP %s 返回 %d 条", name, len(items))
        out.extend((name, item) for item in items if item.get("link"))
    return out


# ---------------- CLI ----------------

def cmd_check(_args) -> int:
    cred = load_credentials()
    rows = searchbiz("人民日报", cred)
    log.info("登录态有效，searchbiz 返回 %d 条结果", len(rows))
    print("登录态有效 ✓")
    return 0


def cmd_search(args) -> int:
    cred = load_credentials()
    rows = searchbiz(args.query, cred)
    if not rows:
        print("没有搜索到结果，试试更精确的名称或微信号")
        return 1
    for row in rows:
        print(f"昵称={row.get('nickname')}  微信号={row.get('alias') or '-'}  fakeid={row.get('fakeid')}")
    return 0


def cmd_add(args) -> int:
    cred = load_credentials()
    rows = searchbiz(args.query, cred)
    if not rows:
        print("没有搜索到结果，先用 search 子命令确认名称")
        return 1
    row = rows[0]
    if any(a.get("fakeid") == row["fakeid"] for a in cred.get("accounts", [])):
        print(f"已在 accounts 里，无需重复添加：{row.get('nickname')}")
        return 0
    acc = {"name": row.get("nickname"), "fakeid": row["fakeid"]}
    # fakeid 恰是 bizuin 的 base64：与 cookie 里 slave_bizuin 一致即为号主本人，
    # 标记 own 后走 appmsgpublish 接口（不受 list_ex 频控）
    m = re.search(r"slave_bizuin=(\d+)", cred.get("cookie", ""))
    if m and base64.b64encode(m.group(1).encode()).decode() == row["fakeid"]:
        acc["own"] = True
        note = "（自己的号，已标记 own，走 appmsgpublish）"
    else:
        note = "（别人的号，走 list_ex，若遇 200013 频控等冷却后自动恢复）"
    cred.setdefault("accounts", []).append(acc)
    save_credentials(cred)
    print(f"已添加：{row.get('nickname')}（fakeid={row['fakeid']}）{note}")
    return 0


def cmd_fetch(_args) -> int:
    rows = fetch_all()
    if not rows:
        print("没有拉到任何条目（接口被限流或账号未配置）", file=sys.stderr)
        return 1
    for name, item in rows:
        ts = item.get("create_time") or 0
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
        print(f"[{name}] {when}  {item.get('title')}\n           {item['link']}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="公众号后台数据源工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="验证登录态是否有效").set_defaults(fn=cmd_check)
    p_search = sub.add_parser("search", help="搜索公众号，打印 fakeid")
    p_search.add_argument("query")
    p_search.set_defaults(fn=cmd_search)
    p_add = sub.add_parser("add", help="把搜索结果第一条加入 accounts")
    p_add.add_argument("query")
    p_add.set_defaults(fn=cmd_add)
    sub.add_parser("fetch", help="手动拉一次所有账号的最新文章").set_defaults(fn=cmd_fetch)
    args = parser.parse_args()
    try:
        return args.fn(args)
    except MpError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
