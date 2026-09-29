"""生成一份**完全虚构**的示例数据，供 ``python src/main.py --demo`` 使用。

目的是让 clone 下来的人不用登录抖音就能看到报表长什么样、差异怎么体现。
这里所有账号、昵称、抖音号、数字都是编出来的，与任何真实用户无关。
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

# 虚构昵称池（都很普通，不会指向真实账号）
_NICKNAMES = [
    "一只柴犬", "今天也要开心", "阿柚", "柠檬不酸", "山海不可平", "小满",
    "晚风", "陈二狗", "不吃香菜", "林深见鹿", "橘子汽水", "酸奶盖",
    "放空", "银喉长尾山雀", "半糖去冰", "风起时", "老张", "眠眠",
    "雪碧加冰", "cocoa", "不打烊", "简一", "董小姐", "北岛",
    "甜筒", "木木", "拾光", "远山", "夏目", "青柠",
    "秋刀鱼", "雾里看花", "小鹿乱撞", "糖醋排骨", "月半", "阿澈",
]

_VERIFY = ["", "", "", "", "美食博主", "摄影爱好者", ""]
_SIGNATURES = ["这个人很懒", "记录生活", "热爱可抵岁月漫长", ""]

# 「页游标」——真实接口就是按这种一页页的时间游标返回的，这里照着模拟
_PAGE_CURSORS = [
    1647735529,  # 2022-03
    1664718373,  # 2022-10
    1722268598,  # 2024-07
    1736742230,  # 2025-01
    1749472192,  # 2025-06
    1760967476,  # 2025-10
    1769789962,  # 2026-01
    1790607717,  # 2026-09
]


def _cursor_for(index: int, total: int) -> int:
    """把一个位置映射到某个「页」，模拟真实数据一页页分组的样子。"""
    if total <= 1:
        return _PAGE_CURSORS[0]
    return _PAGE_CURSORS[index * len(_PAGE_CURSORS) // total]


def _fake_user(uid: int, rng: random.Random, follow_status: int, cursor: int) -> Dict[str, Any]:
    nick = _NICKNAMES[uid % len(_NICKNAMES)]
    if uid >= len(_NICKNAMES):  # 重名是常态，加序号区分
        nick = f"{nick}{uid // len(_NICKNAMES) + 1}"
    created = int(datetime(2017, 1, 1).timestamp()) + rng.randint(0, 220_000_000)
    return {
        "uid": f"D{uid:06d}",
        "sec_uid": f"MS4wLjABAAAA_demo_{uid:06d}",
        "nickname": nick,
        "unique_id": f"demo_{uid:06d}",
        "short_id": str(100000000 + uid),
        "signature": rng.choice(_SIGNATURES),
        "remark_name": "",
        "follower_count": rng.randint(12, 48000),
        "following_count": rng.randint(5, 900),
        "aweme_count": rng.randint(0, 320),
        "custom_verify": rng.choice(_VERIFY),
        "follow_status": follow_status,
        "cursor_max": cursor,
        "cursor_min": cursor - 86_400 * 90,
        "extra": {"create_time": created},
    }


def _snapshot(day: str, following: List[Dict], fans: List[Dict]) -> Dict[str, Any]:
    return {
        "v": 7,
        "collected_at": f"{day}T10:00:00.000Z",
        "date": day,
        "source": "demo（全部为虚构数据）",
        "meta": {
            kind: {
                "hasMore": False,
                "total": len(items),
                "pages": len({u["cursor_max"] for u in items}),
                "rawCount": len(items),
                "ascending": True,
                "log": [
                    {"n": 20, "key": key, "more": True, "total": len(items),
                     "max_time": c, "min_time": c - 86_400 * 90}
                    for c in sorted({u["cursor_max"] for u in items})
                ],
            }
            for kind, key, items in (
                ("following", "followings", following),
                ("fans", "followers", fans),
            )
        },
        "following_count": len(following),
        "fans_count": len(fans),
        "following": following,
        "fans": fans,
    }


def demo_snapshots() -> List[Tuple[str, Dict[str, Any]]]:
    """两份快照。第二份相对第一份：

    - 我取关了 3 个人
    - 4 个人取关了我
    - 新增 5 个粉丝
    """
    rng1, rng2 = random.Random(1), random.Random(2)

    following1 = [
        _fake_user(i, rng1, 1 if i % 19 == 0 else 2, _cursor_for(i, 36)) for i in range(36)
    ]
    # 前 24 个与关注列表 uid 重合 => 互关；后 10 个是单向粉丝
    fans1 = [_fake_user(i, rng1, 2, _cursor_for(i, 34)) for i in range(24)] + [
        _fake_user(900 + i, rng1, 0, _cursor_for(i, 34)) for i in range(24, 34)
    ]

    following2 = following1[:33]
    fans2 = fans1[:30] + [
        _fake_user(2000 + k, rng2, 0, _cursor_for(k, 5)) for k in range(5)
    ]

    return [
        ("demo_2026-08-01.json", _snapshot("2026-08-01", following1, fans1)),
        ("demo_2026-08-20.json", _snapshot("2026-08-20", following2, fans2)),
    ]


def write_demo_dataset(raw_dir, upto: int = 2) -> List[Path]:
    """把前 ``upto`` 份快照写进 ``raw_dir``（会先清空该目录）。

    分两次写是为了让 ``--demo`` 能先跑一次基线、再跑一次出变化 ——
    和真人「隔几天采一次」的用法一致。
    """
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("*.json"):
        old.unlink()

    written = []
    for name, data in demo_snapshots()[:upto]:
        path = raw_dir / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)
    return written
