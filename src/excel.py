"""把快照渲染成 Excel 报表。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
BODY_FONT = Font(size=10)
_SIDE = Side(style="thin", color="D9D9D9")
BORDER = Border(left=_SIDE, right=_SIDE, top=_SIDE, bottom=_SIDE)

COLOR_MUTUAL = "2E7D32"    # 互关 - 绿
COLOR_FANS = "1565C0"      # 粉丝 - 蓝
COLOR_FOLLOWING = "6A1B9A"  # 关注 - 紫
COLOR_LOST = "C62828"      # 取关了我 - 红
COLOR_NEW = "00897B"       # 新粉丝 - 青
COLOR_LEFT = "EF6C00"      # 我取关了 - 橙
COLOR_HISTORY = "455A64"   # 概览 - 灰

def fmt_date(ts: Any) -> str:
    """秒级时间戳 -> ``YYYY-MM-DD``；无效值返回空串。"""
    try:
        n = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    try:
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


# 用户信息表的列：(表头, 取值函数)
USER_COLUMNS = [
    ("备注", lambda u: u.get("remark", "")),
    ("昵称", lambda u: u.get("nickname", "")),
    ("抖音号", lambda u: u.get("douyin_id", "")),
    ("关系", lambda u: u.get("relation", "")),
    ("账号注册时间", lambda u: fmt_date(u.get("created_time"))),
    ("粉丝数", lambda u: u.get("follower_count", 0)),
    ("关注数", lambda u: u.get("following_count", 0)),
    ("作品数", lambda u: u.get("aweme_count", 0)),
    ("认证", lambda u: u.get("custom_verify", "")),
    ("简介", lambda u: u.get("signature", "")),
]


def _display_width(value: Any) -> int:
    """中文按 2 个字符宽计算。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(value))


def write_sheet(
    ws,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    color: str,
    links: Optional[Sequence[Optional[str]]] = None,
    link_col: Optional[int] = None,
) -> None:
    """写一个表：表头着色、冻结首行、加筛选、按内容自适应列宽。"""
    ws.append(list(headers))

    for idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=idx)
        cell.font = HEADER_FONT
        cell.fill = PatternFill("solid", fgColor=color)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
    ws.freeze_panes = "A2"

    for r_idx, row in enumerate(rows, start=2):
        ws.append(list(row))
        for c_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=r_idx, column=c_idx)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center", wrap_text=False)
        if links and link_col:
            url = links[r_idx - 2] if r_idx - 2 < len(links) else None
            if url:
                cell = ws.cell(row=r_idx, column=link_col)
                cell.hyperlink = url
                cell.font = Font(size=10, color="0563C1", underline="single")

    # 列宽：表头与内容取最大值，夹在 8~52 之间
    for c_idx, header in enumerate(headers, start=1):
        width = _display_width(header) + 4
        for row in rows[:400]:
            if c_idx - 1 < len(row):
                width = max(width, _display_width(row[c_idx - 1]) + 3)
        ws.column_dimensions[get_column_letter(c_idx)].width = min(max(width, 8), 52)

    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"


def _user_rows(users: Sequence[Dict[str, Any]]):
    headers = [c[0] for c in USER_COLUMNS]
    rows = [[fn(u) for _, fn in USER_COLUMNS] for u in users]
    links = [u.get("home_url") or None for u in users]
    return headers, rows, links


def _event_rows(events: Sequence[Dict[str, Any]], date_label: str):
    headers = ["备注", "昵称", "抖音号", date_label, "粉丝数"]
    rows = [
        [
            e.get("remark", ""),
            e.get("nickname", ""),
            e.get("douyin_id", ""),
            e.get("date", ""),
            e.get("follower_count", 0),
        ]
        for e in events
    ]
    return headers, rows


def build_workbook(
    *,
    mutual: List[Dict[str, Any]],
    fans: List[Dict[str, Any]],
    following: List[Dict[str, Any]],
    lost_fans: List[Dict[str, Any]],
    new_fans: List[Dict[str, Any]],
    i_unfollowed: List[Dict[str, Any]],
    runs_summary: List[Dict[str, Any]],
    has_fans: bool,
    has_following: bool,
    warnings: List[str],
    out_path,
) -> Path:
    """生成报表并保存，返回实际写入路径。"""
    wb = Workbook()
    wb.remove(wb.active)

    headers, rows, links = _user_rows(mutual)
    write_sheet(wb.create_sheet("互关好友"), headers, rows, COLOR_MUTUAL, links, link_col=3)

    if has_fans:
        headers, rows, links = _user_rows(fans)
        write_sheet(wb.create_sheet("我的粉丝"), headers, rows, COLOR_FANS, links, link_col=3)

    if has_following:
        headers, rows, links = _user_rows(following)
        write_sheet(wb.create_sheet("我的关注"), headers, rows, COLOR_FOLLOWING, links, link_col=3)

    if lost_fans:
        headers, rows = _event_rows(lost_fans, "取关于")
        write_sheet(wb.create_sheet("取关了我"), headers, rows, COLOR_LOST)

    if new_fans:
        headers, rows = _event_rows(new_fans, "首次发现")
        write_sheet(wb.create_sheet("新粉丝"), headers, rows, COLOR_NEW)

    if i_unfollowed:
        headers, rows = _event_rows(i_unfollowed, "我取关于")
        write_sheet(wb.create_sheet("我取关了"), headers, rows, COLOR_LEFT)

    overview_headers = ["采集日期", "关注数", "粉丝数", "互关数", "新增粉丝", "取关了我", "我新关注", "我取关"]
    overview_rows = [
        [
            r.get("date", ""),
            r.get("counts", {}).get("following", 0),
            r.get("counts", {}).get("fans", 0),
            r.get("counts", {}).get("mutual", 0),
            r.get("delta", {}).get("new_fans", ""),
            r.get("delta", {}).get("unfollowed_me", ""),
            r.get("delta", {}).get("i_followed", ""),
            r.get("delta", {}).get("i_unfollowed", ""),
        ]
        for r in runs_summary
    ]
    write_sheet(wb.create_sheet("历史概览"), overview_headers, overview_rows, COLOR_HISTORY)

    if warnings:
        ws = wb.create_sheet("提示")
        write_sheet(ws, ["说明"], [[w] for w in warnings], COLOR_HISTORY)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def harvest_remarks(xlsx_path) -> Dict[str, str]:
    """从已有报表里回收用户手填的「备注」，返回 ``{抖音号: 备注}``。

    同一个人会出现在「互关好友 / 我的粉丝 / 我的关注」多个表里，用户可能在任何
    一个表里填备注，所以按并集回收：**非空值优先**，空值只在该人从未出现过非空
    备注时占位（这样既不会让空单元格盖掉已填的备注，被清空的人也不会残留旧值）。

    要彻底删除某个人的备注，请删掉 ``remarks.csv`` 里对应行。
    """
    if not xlsx_path:
        return {}
    p = Path(xlsx_path)
    if not p.exists():
        return {}
    out: Dict[str, str] = {}
    try:
        wb = load_workbook(p, read_only=True, data_only=True)
    except Exception:
        return {}
    try:
        for name in ("互关好友", "我的粉丝", "我的关注"):
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            rows = ws.iter_rows(values_only=True)
            try:
                header = next(rows)
            except StopIteration:
                continue
            try:
                remark_i = header.index("备注")
                douyin_i = header.index("抖音号")
            except ValueError:
                continue
            for row in rows:
                if douyin_i >= len(row) or remark_i >= len(row):
                    continue
                did = row[douyin_i]
                if not did:
                    continue
                key = str(did).strip()
                raw_remark = row[remark_i]
                remark = str(raw_remark).strip() if raw_remark is not None else ""
                if remark:
                    out[key] = remark          # 非空值优先，覆盖先前的空占位
                else:
                    out.setdefault(key, "")    # 空值不覆盖已回收到的备注
    finally:
        wb.close()
    return out
