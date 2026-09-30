"""抖音互关/粉丝追踪 —— 主入口。

在项目根目录运行::

    python src/main.py

流程：读取 ``data/raw/`` 里最新的采集文件 → 与 ``data/state/history.json``
里的上一次快照对比 → 生成 ``output/抖音好友关注_YYYY-MM-DD.xlsx``。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import demo
import excel
from models import User, parse_many
from store import (append_events, append_run, last_run, load_history,
                   load_json, load_remarks, save_history, save_remarks)

try:  # Windows 控制台默认 cp936，遇到生僻字别直接抛 UnicodeEncodeError
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
HISTORY_PATH = ROOT / "data" / "state" / "history.json"
OUTPUT_DIR = ROOT / "output"
REMARKS_PATH = ROOT / "remarks.csv"

KINDS = ("following", "fans")


# ------------------------------------------------------------ 数据源发现

def discover_sources(raw_dir: Path) -> Dict[str, Tuple[Path, float, str]]:
    """扫描 ``raw_dir``，为 following / fans 各挑出最新的一份。

    兼容两种文件：采集器按标签分别导出的（``following`` / ``fans`` 键），
    以及两类合并在一起的。返回 ``{kind: (路径, mtime, 采集日期)}``。
    """
    found: Dict[str, Tuple[Path, float, str]] = {}
    if not raw_dir.exists():
        return found

    for path in sorted(raw_dir.glob("*.json")):
        data = load_json(path, None)
        if not isinstance(data, dict):
            continue
        mtime = path.stat().st_mtime
        run_date = str(data.get("date") or "") or datetime.fromtimestamp(
            mtime
        ).strftime("%Y-%m-%d")

        if isinstance(data.get("following"), list):
            _keep_newest(found, "following", path, mtime, run_date)

        fans = data.get("fans")
        if fans is None:
            fans = data.get("followers")
        if isinstance(fans, list):
            _keep_newest(found, "fans", path, mtime, run_date)

    return found


def _keep_newest(found, kind, path, mtime, run_date) -> None:
    """保留「最新」的那份：先比数据自带的日期，再比文件修改时间。

    只比 mtime 是不够的 —— 同时写出的多个文件 mtime 可能分不出先后，
    而且重新下载一个旧文件会让它的 mtime 变新、反而盖掉新数据。
    """
    cur = found.get(kind)
    if cur is None or (run_date, mtime) > (cur[2], cur[1]):
        found[kind] = (path, mtime, run_date)


# ------------------------------------------------------------ 时间顺序还原

def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _collector_reversed(data: Dict[str, Any], raw_meta: Dict[str, Any]) -> bool:
    """v<=6 的采集器会自己猜方向并整体翻转数组，这里判断它当时翻没翻。

    复刻的是采集器里那段旧逻辑：拿「第一页」和「has_more 变 false 的那页」
    的游标比大小。那段逻辑本身是错的（用户从中间往上滚时，列表顶部也会报
    has_more=false，会被误当成末尾），但用它还原当时的动作是准确的。
    """
    if _num(data.get("v")) > 6:
        return False
    first, end = raw_meta.get("firstCursor"), raw_meta.get("endCursor")
    if not first or not end:
        return False
    return not (_num(end) < _num(first))


def _traversal_ascending(raw_meta: Dict[str, Any]) -> bool:
    """列表是不是按「最早关注」排的？看前两个不同的页面游标是否递增。"""
    seq: List[int] = []
    for entry in raw_meta.get("log") or []:
        c = entry.get("max_time")
        if c and c not in seq:
            seq.append(c)
        if len(seq) >= 2:
            break
    return len(seq) >= 2 and _num(seq[1]) > _num(seq[0])


def _page_start_offsets(raw_meta: Dict[str, Any]) -> Dict[int, int]:
    """从翻页日志推出每一页的起始 offset。

    抖音自 2026-09 起改用 offset 分页：响应里的 ``offset`` 是「已返回的总条数」，
    减掉本页条数就是本页的起始位置 —— 也就是这一页在整个列表里的真实位置，
    比数组下标可靠得多（用户滚两遍会把数组顺序打乱）。

    只统计有数据的页，和采集器里的页号（用户记录上的 ``_p``）对齐。
    """
    out: Dict[int, int] = {}
    page = 0
    for entry in raw_meta.get("log") or []:
        n = _num(entry.get("n"))
        if n <= 0:
            continue
        page += 1
        offset = (entry.get("fields") or {}).get("offset")
        if offset is None:
            continue
        out[page] = _num(offset) - n
    return out


def order_by_api(
    users: List[User], raw_meta: Dict[str, Any]
) -> Tuple[List[User], str]:
    """按接口自己的顺序排好，统一成「最新 → 最早」。返回 ``(列表, 依据)``。

    抖音这个接口前后换过三种分页方式，按可靠性依次尝试：

    1. ``offset``（2026-09 起）—— 响应直接给出已返回条数，最可靠
    2. ``max_time`` / ``min_time`` 时间游标（更早）—— 后来恒为 0，已废弃
    3. 数组顺序 —— 兜底；用户中途才滚动、或翻了两遍时这个顺序会乱

    统一成「最新在前」之后，显示方向交给 ``--sort`` 决定。
    """

    page_starts = _page_start_offsets(raw_meta)
    if page_starts and any(u.page_no for u in users):
        def offset_key(u: User):
            start = page_starts.get(u.page_no)
            if start is None:
                return (1, 0, 0)  # 没有页信息的排到最后
            return (0, start, u.page_index)

        return sorted(users, key=offset_key), "offset"

    if any(u.cursor_max for u in users):
        ascending = raw_meta.get("ascending")
        if ascending is None:
            ascending = _traversal_ascending(raw_meta)
        # order_by_time 给的是「最早 → 最新」，这里翻成「最新 → 最早」
        return list(reversed(order_by_time(users, bool(ascending)))), "time_cursor"

    return list(users), "array"


def order_by_time(users: List[User], ascending: bool) -> List[User]:
    """按时间升序（最早 -> 最新）重排。

    每条记录都带着它所在页的翻页游标 ``cursor_max``——游标就是关注时间戳。
    页与页之间按游标升序；页内保持采集时的相对顺序，方向由 ``ascending``
    （列表是「最早关注」还是「最近关注」排序）决定。

    这样得到的顺序**与用户怎么滚、滚了几遍完全无关**，解决了「页面重复拉取
    或中途才开始滚动，导致数组顺序和时间顺序对不上」的问题。
    """
    pairs = list(enumerate(users))
    pairs.sort(key=lambda p: (p[1].cursor_max or 0, p[0] if ascending else -p[0]))
    return [u for _, u in pairs]


# ------------------------------------------------------------ 快照构建

def build_snapshot(
    sources: Dict[str, Tuple[Path, float, str]],
    source_filter: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把采集文件读成一份统一快照，返回 ``(users, lists)``。"""
    users: Dict[str, Dict[str, Any]] = {}
    lists: Dict[str, Optional[List[User]]] = {k: None for k in KINDS}
    meta: Dict[str, Any] = {}

    for kind in KINDS:
        src = sources.get(kind)
        if src is None:
            continue
        path, _mtime, run_date = src
        if source_filter and path.name != source_filter:
            continue

        data = load_json(path, {})
        raw = data.get("following") if kind == "following" else (
            data.get("fans") if data.get("fans") is not None else data.get("followers")
        )
        raw_meta = (data.get("meta") or {}).get(kind) or {}

        # 旧版采集器可能整体翻转了数组，先还原成采集顺序
        if isinstance(raw, list) and _collector_reversed(data, raw_meta):
            raw = list(reversed(raw))

        # 统一按接口自己的顺序排成「最新 -> 最早」，follow_order 即位次
        parsed, basis = order_by_api(parse_many(raw), raw_meta)
        lists[kind] = parsed
        meta[kind] = {
            "order_basis": basis,
            "file": path.name,
            "date": run_date,
            "count": len(parsed),
            "api_total": raw_meta.get("total"),
            "has_more": raw_meta.get("hasMore"),
        }

        for idx, user in enumerate(parsed):
            entry = user.to_dict()
            entry["home_url"] = user.home_url
            entry["cursor_max"] = user.cursor_max
            entry["cursor_min"] = user.cursor_min
            entry["created_time"] = user.created_time
            # 数组顺序 = 接口返回顺序 = 时间倒序（采集器已归一化成「最近的在前」）。
            # 接口不返回每个人的关注时间，这个顺序就是唯一的排序依据。
            entry["follow_order"] = idx
            old = users.get(user.key)
            if old is None:
                users[user.key] = entry
                continue
            # 同一人在两个列表都出现时，补齐空字段
            for f in ("nickname", "douyin_id", "signature", "remark",
                      "custom_verify", "uid"):
                if not old.get(f) and entry.get(f):
                    old[f] = entry[f]
            for f in ("follower_count", "following_count", "aweme_count"):
                if not old.get(f) and entry.get(f):
                    old[f] = entry[f]

    return users, {"lists": lists, "meta": meta}


# ------------------------------------------------------------ 差异对比

def _diff(current: set, previous: set) -> set:
    return current - previous


def annotate_relations(
    users: Dict[str, Dict[str, Any]],
    following_users: List[User],
    fans_users: List[User],
) -> None:
    """给每个人标注和你的关系，结果写进 ``users[key]["relation"]``。

    优先用抖音自己在列表里标的 ``follow_status``，它**不受粉丝列表拿不全的影响**：

    - 关注列表里：``2`` = 互关，``1`` = 我单向关注对方
    - 粉丝列表里：``2`` = 互关，``0`` = 对方单向关注我

    实测中，粉丝列表因为已注销账号等原因少采了 34 人，其中 22 人其实和你互关；
    用「关注 ∩ 粉丝」求交集只能得到 123，而 ``follow_status`` 能正确给出 145。

    只有当 ``follow_status`` 缺失时，才退回用「是否出现在另一个列表」来判断。
    """
    in_following = {u.key for u in following_users}
    in_fans = {u.key for u in fans_users}

    for key, entry in users.items():
        status = entry.get("follow_status")
        if (key in in_following and key in in_fans) or status == 2:
            entry["relation"] = "互关"
        elif key in in_following:
            entry["relation"] = "单向关注"
        elif key in in_fans:
            entry["relation"] = "单向粉丝"
        else:
            entry["relation"] = ""


def compute_delta(
    *,
    run: Dict[str, Any],
    prev: Optional[Dict[str, Any]],
    warnings: List[str],
) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    """对比本次与上次快照，返回 ``(delta 计数, 事件流水)``。"""
    delta: Dict[str, int] = {}
    events: List[Dict[str, Any]] = []

    if prev is None:
        warnings.append(
            # 注意：这里写进的是 Excel 单元格，不支持 markdown，别用 ** 加粗
            "这是首次采集，所以「取关了我 / 新粉丝」这类变化表还没有内容——"
            "它们需要两次采集对比才有。但表格的排序是准确的："
            "首次采集就已经按关注时间排好，不依赖任何前置数据。"
        )
        return delta, events

    def mk_events(keys, info_from, etype, date):
        out = []
        for k in sorted(keys):
            info = info_from.get(k, {})
            out.append({
                "date": date,
                "type": etype,
                "key": k,
                "nickname": info.get("nickname", ""),
                "douyin_id": info.get("douyin_id", ""),
                "remark": info.get("remark", ""),
                "follower_count": info.get("follower_count", 0),
            })
        return out

    cur_users = run["users"]
    prev_users = prev.get("users", {})

    # --- 粉丝侧：需要两次都有粉丝数据，否则会把「上次没采」误判成「全部取关」 ---
    if run["has_fans"] and prev.get("has_fans"):
        cur_fans, prev_fans = set(run["fans"]), set(prev["fans"])
        lost = _diff(prev_fans, cur_fans)
        gained = _diff(cur_fans, prev_fans)
        delta["unfollowed_me"] = len(lost)
        delta["new_fans"] = len(gained)
        events += mk_events(lost, prev_users, "unfollowed_me", run["date"])
        events += mk_events(gained, cur_users, "new_fan", run["date"])
    elif run["has_fans"] and not prev.get("has_fans"):
        warnings.append("上次采集没有粉丝数据，本次跳过粉丝侧对比（只做基线）。")

    # --- 关注侧 ---
    if run["has_following"] and prev.get("has_following"):
        cur_fol, prev_fol = set(run["following"]), set(prev["following"])
        dropped = _diff(prev_fol, cur_fol)
        added = _diff(cur_fol, prev_fol)
        delta["i_unfollowed"] = len(dropped)
        delta["i_followed"] = len(added)
        events += mk_events(dropped, prev_users, "i_unfollowed", run["date"])
        events += mk_events(added, cur_users, "i_followed", run["date"])
    elif run["has_following"] and not prev.get("has_following"):
        warnings.append("上次采集没有关注数据，本次跳过关注侧对比（只做基线）。")

    return delta, events


# ------------------------------------------------------------ 备注

def resolve_remarks(
    *,
    users: Dict[str, Dict[str, Any]],
    csv_remarks: Dict[str, str],
    harvested: Dict[str, str],
    warnings: List[str],
) -> Dict[str, str]:
    """把备注写到用户上，并返回要落盘的 remarks.csv 内容。

    优先级：Excel 报表（人工最新编辑） > remarks.csv > 接口自带 remark_name。
    """
    merged = dict(csv_remarks)

    for did, remark in harvested.items():
        if remark:
            merged[did] = remark
        else:
            # 表格里被清空的备注，同步清掉，避免「删不掉」
            merged.pop(did, None)

    for user in users.values():
        did = user.get("douyin_id") or ""
        if not did:
            continue
        if did in merged:
            user["remark"] = merged[did]
        elif user.get("remark"):
            # 接口/采集器自带的备注，收进 CSV 首次建档
            merged[did] = user["remark"]

    if not csv_remarks and not harvested:
        warnings.append("首次生成 remarks.csv，可在此文件中统一维护备注，或直接写在 Excel 的「备注」列里（下次运行会自动回收）。")

    return merged


# ------------------------------------------------------------ 主流程

def run_once(
    *,
    raw_dir: Path,
    out_dir: Path,
    state_path: Path,
    remarks_path: Path,
    date_override: Optional[str] = None,
    reset: bool = False,
    sort: str = "earliest",
    open_after: bool = False,
) -> int:
    """跑一次完整流程：读采集文件 → 对比历史 → 出报表。"""

    # 1) 找到数据源
    sources = discover_sources(raw_dir)
    if not sources:
        print(f"[!] 在 {raw_dir} 里没找到可用的采集文件。", file=sys.stderr)
        print("    请先在浏览器里用 collector/douyin_collector.js 采集，", file=sys.stderr)
        print("    把下载的 JSON 放进 data/raw/ 再运行本脚本。", file=sys.stderr)
        return 1

    # 2) 建快照
    users, bundle = build_snapshot(sources)
    lists = bundle["lists"]
    meta = bundle["meta"]

    run_date = date_override or max(m["date"] for m in meta.values())
    warnings: List[str] = []

    label_of = {"following": "关注", "fans": "粉丝"}

    basis_name = {"offset": "offset 分页", "time_cursor": "时间游标", "array": "数组顺序"}

    for kind, info in meta.items():
        extra = f"（接口报总数 {info['api_total']}）" if info.get("api_total") else ""
        basis = basis_name.get(info.get("order_basis"), "未知")
        print(f"[+] {label_of[kind]}：{info['count']} 人{extra}"
              f"  排序依据：{basis}  <- {info['file']}")
        # 把排序依据写进报表的「提示」页 —— 第一次用的人也能看到顺序是可依据的
        warnings.append(
            f"「{label_of[kind]}」的排序依据：{basis}。顺序由接口返回的分页位置还原，"
            f"与你采集时怎么滚动、翻了几遍无关，首次采集就是准的。"
        )

    for kind in (k for k in KINDS if k not in meta):
        warnings.append(f"本次没有采集到「{label_of[kind]}」数据，相关对比与分表已跳过。")

    # 采集完整性：接口自己说的「还有更多」比任何猜测都可靠
    for kind, info in meta.items():
        label = label_of[kind]
        if info.get("order_basis") == "array":
            warnings.append(
                f"「{label}」的接口返回里既没有 offset 也没有时间游标，排序只能退回采集顺序。"
                f"如果你采集时中途才开始滚动、或者来回翻了两遍，这个顺序会是乱的 —— "
                f"建议重新采集：打开页面后先滚到最上面，再一路滚到底。"
            )
        if info.get("has_more") is True:
            warnings.append(
                f"「{label}」采集时接口仍报告还有下一页（has_more=1），列表很可能没拉完，"
                f"建议重新采集并滚到最底部。"
            )
        elif info.get("api_total") and info["count"] < info["api_total"]:
            gap = info["api_total"] - info["count"]
            warnings.append(
                f"「{label}」采集到 {info['count']} 人，接口报总数 {info['api_total']}，差 {gap} 人。"
                f"这部分通常是已注销/被封禁或设为私密的账号，抖音的列表本身就不会列出它们，"
                f"所以总数看着虚高，属正常现象。"
            )

    # 3) 备注
    harvested = excel.harvest_remarks(_newest_xlsx(out_dir))
    csv_remarks = load_remarks(remarks_path)
    merged_remarks = resolve_remarks(
        users=users, csv_remarks=csv_remarks, harvested=harvested, warnings=warnings
    )

    # 4) 历史与差异
    history = {"version": 1, "runs": [], "events": []} if reset else load_history(state_path)
    if reset:
        print("[*] 已按 --reset 清空历史。")

    following_users = lists["following"] or []
    fans_users = lists["fans"] or []

    # 关系以抖音自己标的 follow_status 为准，不靠两个列表求交集
    annotate_relations(users, following_users, fans_users)

    run: Dict[str, Any] = {
        "date": run_date,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "has_following": lists["following"] is not None,
        "has_fans": lists["fans"] is not None,
        "following": [u.key for u in following_users],
        "fans": [u.key for u in fans_users],
        "users": users,
        "sources": {k: v["file"] for k, v in meta.items()},
    }
    run["counts"] = {
        "following": len(following_users),
        "fans": len(fans_users),
        "mutual": sum(1 for u in users.values() if u.get("relation") == "互关"),
    }

    # 同一天重复采集：先把这一天的旧记录连同它产生的事件撤掉，
    # 否则对比基准会变成「它自己」，算出来永远是 0 变化；
    # 事件流水也会被同一批人重复累积。
    runs = history.get("runs") or []
    if runs and runs[-1].get("date") == run_date:
        runs.pop()
        history["events"] = [e for e in (history.get("events") or [])
                             if e.get("date") != run_date]

    prev = last_run(history)
    delta, events = compute_delta(run=run, prev=prev, warnings=warnings)
    run["delta"] = delta

    append_run(history, run)
    append_events(history, events)

    # 5) 落盘
    save_history(state_path, history)
    save_remarks(remarks_path, merged_remarks)

    # 6) 出报表
    by_key = users
    ledger = history.get("events", [])

    def pick(etype: str) -> List[Dict[str, Any]]:
        rows = [e for e in ledger if e.get("type") == etype]
        rows.sort(key=lambda e: e.get("date", ""), reverse=True)
        return rows

    sort_key = make_sort_key(earliest_first=(sort == "earliest"))

    def rows_for(user_list: List[User]) -> List[Dict[str, Any]]:
        """把 User 换成带 home_url 的 dict，并按关注时间排序。"""
        out = [by_key[u.key] for u in user_list if u.key in by_key]
        out.sort(key=sort_key)
        return out

    out_path = out_dir / f"抖音好友关注_{run_date}.xlsx"
    written = excel.build_workbook(
        mutual=sorted(
            (u for u in users.values() if u.get("relation") == "互关"),
            key=sort_key,
        ),
        fans=rows_for(fans_users),
        following=rows_for(following_users),
        lost_fans=pick("unfollowed_me"),
        new_fans=pick("new_fan"),
        i_unfollowed=pick("i_unfollowed"),
        runs_summary=history.get("runs", []),
        has_fans=run["has_fans"],
        has_following=run["has_following"],
        warnings=warnings,
        out_path=out_path,
    )

    print()
    print(f"[√] 报表已生成：{written}")
    print(f"    关注 {run['counts']['following']} · 粉丝 {run['counts']['fans']} · 互关 {run['counts']['mutual']}")
    if delta:
        print(f"    本次变化：新增粉丝 {delta.get('new_fans', 0)} · 取关了我 {delta.get('unfollowed_me', 0)}"
              f" · 我新关注 {delta.get('i_followed', 0)} · 我取关 {delta.get('i_unfollowed', 0)}")
    for w in warnings:
        print(f"    [!] {w}")

    if open_after:
        try:
            os.startfile(written)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - 平台相关
            print(f"    [!] 自动打开失败：{exc}")

    return 0


# ------------------------------------------------------------ 命令行入口

def run_demo(args) -> int:
    """``--demo``：用虚构数据把「隔几天采两次」演一遍。

    全部读写都在 ``demo/`` 目录下，**不会碰你的真实数据**。
    """
    demo_dir = ROOT / "demo"
    raw_dir = demo_dir / "raw"
    state_path = demo_dir / "state" / "history.json"
    remarks_path = demo_dir / "remarks.csv"
    out_dir = demo_dir / "output"

    print("=" * 62)
    print(" DEMO 模式：使用虚构数据，不读取也不修改你的真实数据")
    print("=" * 62)

    print("\n--- 第一次采集（2026-08-01）：建立基线 ---")
    demo.write_demo_dataset(raw_dir, upto=1)
    run_once(raw_dir=raw_dir, out_dir=out_dir, state_path=state_path,
             remarks_path=remarks_path, reset=True, sort=args.sort)

    print("\n--- 第二次采集（2026-08-20）：对比出变化 ---")
    demo.write_demo_dataset(raw_dir, upto=2)
    code = run_once(raw_dir=raw_dir, out_dir=out_dir, state_path=state_path,
                    remarks_path=remarks_path, sort=args.sort, open_after=args.open)

    print(f"\n演示数据保留在 {demo_dir}")
    print(f"报表在 {out_dir}，可以直接打开看看「取关了我」那几张表。")
    return code


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="抖音互关/粉丝追踪：把关注和粉丝列表存成 Excel，并对比出谁取关了你"
    )
    parser.add_argument("--raw-dir", default=str(RAW_DIR), help="采集 JSON 所在目录")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="报表输出目录")
    parser.add_argument("--state", default=str(HISTORY_PATH),
                        help="历史快照文件（对比基准，别删）")
    parser.add_argument("--remarks", default=str(REMARKS_PATH), help="备注文件")
    parser.add_argument("--date", default=None, help="覆盖快照日期 (YYYY-MM-DD)")
    parser.add_argument("--reset", action="store_true", help="清空历史，本次重新建立基线")
    parser.add_argument(
        "--sort", choices=("earliest", "recent"), default="earliest",
        help="表格排序：earliest=最早关注的在最上面（默认），recent=最近关注的在最上面",
    )
    parser.add_argument("--open", action="store_true", help="生成后用系统默认程序打开报表")
    parser.add_argument("--demo", action="store_true",
                        help="用虚构数据跑一遍演示，不需要登录抖音，也不碰你的真实数据")
    args = parser.parse_args(argv)

    if args.demo:
        return run_demo(args)

    return run_once(
        raw_dir=Path(args.raw_dir),
        out_dir=Path(args.output_dir),
        state_path=Path(args.state),
        remarks_path=Path(args.remarks),
        date_override=args.date,
        reset=args.reset,
        sort=args.sort,
        open_after=args.open,
    )


def make_sort_key(earliest_first: bool):
    """按关注时间排列表格的排序键。

    依据是**所在页的翻页游标上界 ``cursor_max``** —— 游标本身就是关注时间戳，
    越大表示关注得越晚。同一页内的先后由稳定排序保持。

    ⚠️ 不能用数组下标当排序依据：页面可能被重复拉取，或者用户中途才滚动、
    导致最新的记录被追加到数组末尾，下标顺序和时间顺序就对不上了。
    """

    def key(d: Dict[str, Any]):
        # follow_order 已由 order_by_api 归一化成「最新 -> 最早」的位次：
        # 越小 = 关注得越晚。「最早在前」就是把它倒过来。
        order = d.get("follow_order")
        if order is None:
            return (1, 0, d.get("nickname") or "")
        return (0, -order if earliest_first else order, "")

    return key


def _newest_xlsx(out_dir: Path) -> Optional[Path]:
    if not out_dir.exists():
        return None
    files = sorted(out_dir.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


if __name__ == "__main__":
    raise SystemExit(main())
