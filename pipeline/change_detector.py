"""时序变化检测（v0.1 必做）：多期次检测成果的空间叠加比对。

方法（演示级，符合原文档 §12"像素+bbox 层变化输出"）：
- 按时序对**相邻两期**做一对一最近邻匹配（阈值内视为同一目标）；
- 分类：stable（两期均出现）/ appeared（仅后期）/ disappeared（仅前期）；
- 输出变化点要素，供 SHP/GDB 导出。每期转换单独成组，特征里的
  session_a/session_b 标明所属转换。

匹配使用网格索引 + 全局按距离贪心，保证一对一：同一目标在两期中只产生
一条 stable，不会因为就近匹配而被重复计数。调用方需按时间先后传入期次
（DataProject 按期次名排序后构造 dict）。
"""
from __future__ import annotations

import math

from .config import CHANGE_TRACKED_CLASSES, CLASS_CN


def _candidate_pairs(a_cls, b_cls, thr):
    """返回阈值内的所有候选 (距离, a 下标, b 下标)，网格索引避免 O(n²)。"""
    cell = max(float(thr), 1e-6)
    grid: dict[tuple[int, int], list[int]] = {}
    for ib, p in enumerate(b_cls):
        key = (math.floor(p["x"] / cell), math.floor(p["y"] / cell))
        grid.setdefault(key, []).append(ib)
    pairs = []
    for ia, p in enumerate(a_cls):
        kx, ky = math.floor(p["x"] / cell), math.floor(p["y"] / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ib in grid.get((kx + dx, ky + dy), ()):
                    q = b_cls[ib]
                    d = math.hypot(p["x"] - q["x"], p["y"] - q["y"])
                    if d <= thr:
                        pairs.append((d, ia, ib))
    return pairs


def _match_one_to_one(a_cls, b_cls, thr):
    """按距离从近到远贪心配对，返回 (pairs, used_a, used_b)。"""
    pairs = sorted(_candidate_pairs(a_cls, b_cls, thr))
    used_a, used_b, matched = set(), set(), []
    for d, ia, ib in pairs:
        if ia in used_a or ib in used_b:
            continue
        used_a.add(ia)
        used_b.add(ib)
        matched.append((ia, ib, d))
    return matched, used_a, used_b


def _detect_pair(a, pa, b, pb, threshold_m):
    changes = []
    for cid in CHANGE_TRACKED_CLASSES:
        a_cls = [p for p in pa if p.get("class_id") == cid]
        b_cls = [p for p in pb if p.get("class_id") == cid]
        if not a_cls and not b_cls:
            continue
        matched, used_a, used_b = _match_one_to_one(a_cls, b_cls, threshold_m)
        for ia, ib, d in matched:
            p, m = a_cls[ia], b_cls[ib]
            changes.append({
                "x": (p["x"] + m["x"]) / 2,
                "y": (p["y"] + m["y"]) / 2,
                "class_id": cid,
                "class_cn": CLASS_CN.get(cid, str(cid)),
                "change_type": "stable",
                "session_a": a, "session_b": b,
                "match_dist": round(d, 2),
            })
        for ia, p in enumerate(a_cls):
            if ia not in used_a:
                changes.append({**p, "change_type": "disappeared",
                                "session_a": a, "session_b": b})
        for ib, p in enumerate(b_cls):
            if ib not in used_b:
                changes.append({**p, "change_type": "appeared",
                                "session_a": a, "session_b": b})
    return changes


def detect_changes(session_points: dict[str, list[dict]],
                   threshold_m: float = 15.0) -> list[dict]:
    """session_points: {session_name: [point features]}（按时间先后）→ 变化点要素。"""
    sessions = list(session_points.keys())
    if len(sessions) < 2:
        return []
    changes = []
    for a, b in zip(sessions, sessions[1:]):
        changes.extend(_detect_pair(a, session_points[a], b,
                                    session_points[b], threshold_m))
    return changes
