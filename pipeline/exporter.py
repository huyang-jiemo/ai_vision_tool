"""矢量导出：SHP（pyshp，纯 Python 稳定）+ File GDB（pyogrio/OpenFileGDB）。

约定 feature 结构：
- 点要素：dict(class_id, class_cn, session, image, x, y, dist, score)
- 线要素：dict(session, image, side, points=[(x,y),...], avg_dist)
- 变化要素：dict(x, y, class_id, class_cn, change_type, session_a, session_b)

GDB 写入必须包 try/except，失败不能拖垮整流程；SHP 无论如何都要出。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import WUHAN_CGCS2000_GK_WKT

try:
    from shapely.geometry import LineString, Point
except Exception:  # shapely 缺失时降级（仅 SHP 路径依赖）
    Point = LineString = None


def export_points_shp(shp_path, features):
    import shapefile
    w = shapefile.Writer(str(shp_path), shapeType=shapefile.POINT)
    w.field("CLASSID", "C", size=8)
    w.field("CLASSCN", "C", size=16)
    w.field("SESSION", "C", size=80)
    w.field("IMAGE", "C", size=40)
    w.field("DIST", "F", decimal=2)
    w.field("SCORE", "F", decimal=3)
    for f in features:
        w.point(f["x"], f["y"])
        w.record(str(f.get("class_id", "")), f.get("class_cn", ""), f.get("session", ""),
                 f.get("image", ""), float(f.get("dist", 0.0)), float(f.get("score", 0.0)))
    w.close()
    # 写 .prj
    Path(str(shp_path).replace(".shp", ".prj")).write_text(WUHAN_CGCS2000_GK_WKT, encoding="utf-8")
    return shp_path


def export_lines_shp(shp_path, features):
    import shapefile
    w = shapefile.Writer(str(shp_path), shapeType=shapefile.POLYLINE)
    w.field("SESSION", "C", size=80)
    w.field("IMAGE", "C", size=40)
    w.field("SIDE", "C", size=8)
    w.field("AVGDIST", "F", decimal=2)
    for f in features:
        w.line([f["points"]])
        w.record(f.get("session", ""), f.get("image", ""), f.get("side", ""),
                 float(f.get("avg_dist", 0.0)))
    w.close()
    Path(str(shp_path).replace(".shp", ".prj")).write_text(WUHAN_CGCS2000_GK_WKT, encoding="utf-8")
    return shp_path


def _gdf(geoms, fields: dict):
    import geopandas as gpd
    import pandas as pd
    df = pd.DataFrame(fields)
    return gpd.GeoDataFrame(df, geometry=geoms, crs=WUHAN_CGCS2000_GK_WKT)


# 导出过程中的非致命告警，供上层（processor/api）收集并展示到运行日志
WARNINGS = []


def _export_points_gdb(gdb_path, layer, features):
    geoms = [Point(f["x"], f["y"]) for f in features]
    fields = {
        "CLASSID": [str(f.get("class_id", "")) for f in features],
        "CLASSCN": [f.get("class_cn", "") for f in features],
        "SESSION": [f.get("session", "") for f in features],
        "IMAGE": [f.get("image", "") for f in features],
        "DIST": [float(f.get("dist", 0.0)) for f in features],
        "SCORE": [float(f.get("score", 0.0)) for f in features],
    }
    _gdf(geoms, fields).to_file(str(gdb_path), layer=layer, driver="OpenFileGDB")


def _export_lines_gdb(gdb_path, layer, features):
    geoms = [LineString(f["points"]) for f in features]
    fields = {
        "SESSION": [f.get("session", "") for f in features],
        "IMAGE": [f.get("image", "") for f in features],
        "SIDE": [f.get("side", "") for f in features],
        "AVGDIST": [float(f.get("avg_dist", 0.0)) for f in features],
    }
    _gdf(geoms, fields).to_file(str(gdb_path), layer=layer, driver="OpenFileGDB")


def export_points_gdb(gdb_path, layer, features):
    try:
        if Point is None:
            WARNINGS.append("GDB 导出不可用：缺少 shapely，仅输出 SHP")
            return False
        if not features:
            return True
        _export_points_gdb(gdb_path, layer, features)
        return True
    except Exception as e:
        WARNINGS.append(f"GDB 点层导出失败（{layer}，SHP 仍可用）: {e}")
        return False


def export_lines_gdb(gdb_path, layer, features):
    try:
        if LineString is None:
            WARNINGS.append("GDB 导出不可用：缺少 shapely，仅输出 SHP")
            return False
        if not features:
            return True
        _export_lines_gdb(gdb_path, layer, features)
        return True
    except Exception as e:
        WARNINGS.append(f"GDB 线层导出失败（{layer}，SHP 仍可用）: {e}")
        return False


def export_summary_gdb(gdb_path, layer, center_x, center_y, counts: dict):
    """图幅中心 + 计数统计点要素。"""
    try:
        _gdf([Point(center_x, center_y)],
             {"TOTAL": [int(sum(counts.values()))], "N_SESS": [len(counts)]}
             ).to_file(str(gdb_path), layer=layer, driver="OpenFileGDB")
    except Exception as e:
        WARNINGS.append(f"统计层导出失败: {e}")
