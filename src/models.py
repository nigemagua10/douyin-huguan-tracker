"""数据模型 + 原始字段归一化。

抖音不同接口返回的用户结构不完全一致（有时是裸 user 对象，有时包一层
``{"user": {...}, "stats": {...}}``），这里统一成 :class:`User`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, field
from typing import Any, Dict


def _pick(d: Any, *keys: str, default: Any = "") -> Any:
    """返回 ``d`` 中第一个存在且非空的值。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return default


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _clean_text(v: Any) -> str:
    """统一换行符、去掉首尾空白，但**保留简介内部的分行**。

    抖音的个性签名里确实带换行（实测 88 条非空简介中 19 条含 ``\\n``），
    有的还用连续换行做空行。这些是作者排版的一部分，不能压成一行。
    """
    if v is None:
        return ""
    return str(v).replace("\r\n", "\n").replace("\r", "\n").strip()


@dataclass
class User:
    uid: str = ""
    sec_uid: str = ""
    nickname: str = ""
    douyin_id: str = ""          # 抖音号
    signature: str = ""          # 个性签名
    remark: str = ""             # 备注（本地备注 / 接口 remark_name）
    follower_count: int = 0
    following_count: int = 0
    aweme_count: int = 0
    custom_verify: str = ""      # 认证信息
    follow_status: int = 0
    cursor_max: int = 0          # 所在页的翻页游标上界（= 关注时间上界）
    cursor_min: int = 0          # 所在页的翻页游标下界（= 关注时间下界）
    page_no: int = 0             # 采集时属于第几页（只数有数据的页）
    page_index: int = 0          # 在这一页里排第几个
    extra: Dict[str, Any] = field(default_factory=dict, repr=False)  # 接口返回的其余标量字段
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        """稳定主键：sec_uid -> uid -> 抖音号 -> 记录内容哈希。

        **绝不用昵称兜底**：抖音重名极多，用昵称当主键会把不同的人合并成一条，
        表现为「采到的人数比接口报的少」。没有 ID 时按整条记录做哈希，
        只有完全相同的记录才会被判为同一人。
        """
        if self.sec_uid:
            return self.sec_uid
        if self.uid:
            return self.uid
        if self.douyin_id:
            return self.douyin_id
        if self.raw:
            blob = json.dumps(self.raw, sort_keys=True, ensure_ascii=False)
            return "h" + hashlib.md5(blob.encode("utf-8")).hexdigest()[:12]
        return ""

    @property
    def home_url(self) -> str:
        return f"https://www.douyin.com/user/{self.sec_uid}" if self.sec_uid else ""

    @property
    def created_time(self) -> int:
        """对方**账号的注册时间**（秒级时间戳）。

        注意：这是 ``create_time``，跟「你什么时候关注他」无关，别混用。
        抖音的关注列表接口**不返回每个人的关注时间**。
        """
        n = _int(self.extra.get("create_time"))
        if n <= 0:
            return 0
        return n // 1000 if n > 10 ** 12 else n

    @property
    def follow_window(self) -> tuple:
        """关注时间所在的区间 ``(起, 止)``，秒级时间戳；未知返回 ``(0, 0)``。

        来源是接口的翻页游标（游标即关注时间）。列表按关注时间倒序返回，
        所以第 N 页的游标范围就框住了这一页所有人的关注时间。
        拿不到精确的单人时间，但足以判断先后与大致年份。
        """
        lo, hi = _int(self.cursor_min), _int(self.cursor_max)
        if lo <= 0 or hi <= 0:
            return (0, 0)
        return (min(lo, hi), max(lo, hi))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


def parse_user(item: Dict[str, Any]) -> User:
    """把一条接口/采集器记录转成 :class:`User`。"""
    if not isinstance(item, dict):
        return User()

    u = item.get("user") or item.get("user_info") or item
    if not isinstance(u, dict):
        u = item
    stats = item.get("stats") or u.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}

    remark = _pick(item, "remark_name", "remark", "alias") or _pick(
        u, "remark_name", "remark", "alias"
    )

    douyin_id = _pick(u, "unique_id", "douyin_id", "uniqueId")
    if not douyin_id:
        short_id = _pick(u, "short_id", "shortId")
        douyin_id = str(short_id) if short_id else ""

    return User(
        uid=str(_pick(u, "uid", "user_id") or ""),
        sec_uid=str(_pick(u, "sec_uid", "sec_user_id", "secUid") or ""),
        nickname=str(_pick(u, "nickname", "nick_name") or ""),
        douyin_id=str(douyin_id or ""),
        signature=_clean_text(_pick(u, "signature", "desc")),
        remark=str(remark or ""),
        follower_count=_int(_pick(stats, "follower_count") or _pick(u, "follower_count")),
        following_count=_int(_pick(stats, "following_count") or _pick(u, "following_count")),
        aweme_count=_int(_pick(stats, "aweme_count") or _pick(u, "aweme_count")),
        custom_verify=str(_pick(u, "custom_verify") or ""),
        follow_status=_int(_pick(item, "follow_status") or _pick(u, "follow_status")),
        cursor_max=_int(_pick(item, "cursor_max")),
        cursor_min=_int(_pick(item, "cursor_min")),
        page_no=_int(_pick(item, "_p")),
        page_index=_int(_pick(item, "_pi")),
        extra=item.get("extra") if isinstance(item.get("extra"), dict) else {},
        raw=item if isinstance(item, dict) else {},
    )


def parse_many(items: Any) -> list[User]:
    """批量归一化并按键去重（后出现的记录补齐先前记录的空字段）。"""
    out: Dict[str, User] = {}
    if not isinstance(items, list):
        return []
    for item in items:
        user = parse_user(item)
        if not user.key:
            continue
        old = out.get(user.key)
        if old is None:
            out[user.key] = user
        else:
            for f in ("uid", "sec_uid", "nickname", "douyin_id", "signature",
                      "remark", "custom_verify"):
                if not getattr(old, f) and getattr(user, f):
                    setattr(old, f, getattr(user, f))
            for f in ("follower_count", "following_count", "aweme_count",
                      "cursor_max", "cursor_min"):
                if not getattr(old, f) and getattr(user, f):
                    setattr(old, f, getattr(user, f))
    return list(out.values())
