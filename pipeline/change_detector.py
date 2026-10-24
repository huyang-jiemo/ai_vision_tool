"""时序变化检测（v0.1 必做）：多期次检测成果的空间叠加比对。

方法（演示级，符合原文档 §12"像素+bbox 层变化输出"）：
- 对每个关注类别，在空间上对两期次目标点做最近邻匹配（阈值内视为同一目标）。
- 分类：stable（两期均出现）/ appeared（仅后期）/ disappeared（仅前期）。
- 输出变化点要素，供 SHP/GDB 导出。
"""
from __future__ import annotations

from .config import CHANGE_TRACKED_CLASSES, CLASS_CN


def _nearest(pt, others, thr):
    best, bd = None, thr
    for o in others:
        d = (pt["x"] - o["x"]) ** 2 + (pt["y"] - o["y"]) ** 2
        if d < bd * bd:
            bd = d ** 0.5
            best = o
    return best


def detect_changes(session_points: dict[str, list[dict]], threshold_m: float = 15.0) -> list[dict]:
    """session_points: {session_name: [point features]} -> 变化点要素列表。"""
    sessions = list(session_points.keys())
    if len(sessions) < 2:
        return []
    a, b = sessions[0], sessions[1]
    pa, pb = session_points[a], session_points[b]
    changes = []
    used_b = set()
    for cid in CHANGE_TRACKED_CLASSES:
        a_cls = [p for p in pa if p.get("class_id") == cid]
        b_cls = [p for p in pb if p.get("class_id") == cid]
        for p in a_cls:
            m = _nearest(p, b_cls, threshold_m)
            if m is not None:
                changes.append({"x": (p["x"] + m["x"]) / 2, "y": (p["y"] + m["y"]) / 2,
                                "class_id": cid, "class_cn": CLASS_CN.get(cid, str(cid)),
                                "change_type": "stable", "session_a": a, "session_b": b})
                used_b.add(id(m))
            else:
                changes.append({**p, "change_type": "disappeared", "session_a": a, "session_b": b})
        for p in b_cls:
            if id(p) in used_b:
                continue
            m = _nearest(p, a_cls, threshold_m)
            if m is None:
                changes.append({**p, "change_type": "appeared", "session_a": a, "session_b": b})
    return changes
