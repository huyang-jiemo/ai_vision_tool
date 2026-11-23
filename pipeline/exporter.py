"""矢量导出：SHP（pyshp，纯 Python 稳定）+ File GDB（pyogrio/OpenFileGDB）。

约定 feature 结构：
- 点要素：dict(class_id, class_cn, session, image, x, y, dist, score)
- 线要素：dict(session, image, side, points=[(x,y),...], avg_dist)
- 变化要素：dict(x, y, class_id, class_cn, change_type, session_a, session_b)

所有导出函数都显式接收目标 CRS（crs 参数），不再内置任何默认坐标系。
属性 DIST/AVGDIST 始终是米，与输出 CRS 无关。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import coords

try:
    from shapely.geometry import LineString, Point
except Exception:  # shapely 缺失时降级（仅 SHP 路径依赖）
    Point = LineString = None


def _require_crs(crs):
    if crs is None:
        raise ValueError("导出必须提供目标坐标系（crs）")
    return crs


# SHP 及其 sidecar；清理旧成果时必须一并删除，否则 ArcGIS/QGIS 会读到
# 上次运行的索引（.qix/.sbn/.sbx）或元数据（.shp.xml）。
SHP_SIDECAR_SUFFIXES = (".shp", ".dbf", ".shx", ".prj", ".cpg",
                        ".qix", ".sbn", ".sbx")


def clean_vector_outputs(directory, *, prefix: str | None = None) -> list[str]:
    """删除目录下旧的 SHP 与 sidecar，返回未能删除的文件说明。

    ``prefix`` 用于只清理本工具生成的文件（如 ``result_``）；单个文件被
    ArcGIS 占用时跳过并返回原因，不阻断其余输出。
    """
    d = Path(directory)
    errors: list[str] = []
    if not d.is_dir():
        return errors
    pre = (prefix or "").lower()
    for old in d.iterdir():
        if not old.is_file():
            continue
        name = old.name.lower()
        if pre and not name.startswith(pre):
            continue
        if not (name.endswith(SHP_SIDECAR_SUFFIXES) or name.endswith(".shp.xml")):
            continue
        try:
            old.unlink()
        except OSError as e:
            errors.append(f"无法删除旧文件「{old.name}」：{e}")
    return errors


def clear_file_dataset(path) -> str:
    """删除旧的 File GDB（目录）或单文件数据集，返回错误说明。

    复用输出目录时，旧 GDB 里上次运行的图层不会自动消失；不清理会让
    本次报告指向含有陈旧图层的成果。删除失败时返回原因，由调用方决定
    是否继续写盘。
    """
    p = Path(path)
    if not (p.exists() or p.is_symlink()):
        return ""
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except OSError as e:
        return f"无法清理旧成果「{p.name}」：{e}"
    return ""


def _write_prj_cpg(shp_path, crs) -> None:
    """写投影文件与 DBF 编码声明。

    pyshp 以 UTF-8 写 DBF，但不会生成 .cpg；缺少 .cpg 时 ArcGIS 会按系统
    ANSI 代码页解释，中文属性（类别名等）可能乱码。
    """
    _require_crs(crs)
    path = Path(shp_path)
    path.with_suffix(".prj").write_text(
        coords.crs_to_esri_wkt(crs), encoding="utf-8")
    path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")


def export_points_shp(shp_path, features, *, crs):
    import shapefile
    _require_crs(crs)
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
    _write_prj_cpg(shp_path, crs)
    return shp_path


def export_lines_shp(shp_path, features, *, crs):
    import shapefile
    _require_crs(crs)
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
    _write_prj_cpg(shp_path, crs)
    return shp_path


def _gdf(geoms, fields: dict, *, crs):
    import geopandas as gpd
    import pandas as pd
    _require_crs(crs)
    df = pd.DataFrame(fields)
    return gpd.GeoDataFrame(df, geometry=geoms, crs=crs)


# 导出过程中的非致命告警，供上层（processor/api）收集并展示到运行日志
WARNINGS = []


def _export_points_gdb(gdb_path, layer, features, *, crs):
    geoms = [Point(f["x"], f["y"]) for f in features]
    fields = {
        "CLASSID": [str(f.get("class_id", "")) for f in features],
        "CLASSCN": [f.get("class_cn", "") for f in features],
        "SESSION": [f.get("session", "") for f in features],
        "IMAGE": [f.get("image", "") for f in features],
        "DIST": [float(f.get("dist", 0.0)) for f in features],
        "SCORE": [float(f.get("score", 0.0)) for f in features],
    }
    _gdf(geoms, fields, crs=crs).to_file(
        str(gdb_path), layer=layer, driver="OpenFileGDB")


def _export_lines_gdb(gdb_path, layer, features, *, crs):
    geoms = [LineString(f["points"]) for f in features]
    fields = {
        "SESSION": [f.get("session", "") for f in features],
        "IMAGE": [f.get("image", "") for f in features],
        "SIDE": [f.get("side", "") for f in features],
        "AVGDIST": [float(f.get("avg_dist", 0.0)) for f in features],
    }
    _gdf(geoms, fields, crs=crs).to_file(
        str(gdb_path), layer=layer, driver="OpenFileGDB")


def export_points_gdb(gdb_path, layer, features, *, crs):
    try:
        if Point is None:
            WARNINGS.append("GDB 导出不可用：缺少 shapely，仅输出 SHP")
            return False
        if not features:
            return True
        _export_points_gdb(gdb_path, layer, features, crs=crs)
        return True
    except Exception as e:
        WARNINGS.append(f"GDB 点层导出失败（{layer}，SHP 仍可用）: {e}")
        return False


def export_lines_gdb(gdb_path, layer, features, *, crs):
    try:
        if LineString is None:
            WARNINGS.append("GDB 导出不可用：缺少 shapely，仅输出 SHP")
            return False
        if not features:
            return True
        _export_lines_gdb(gdb_path, layer, features, crs=crs)
        return True
    except Exception as e:
        WARNINGS.append(f"GDB 线层导出失败（{layer}，SHP 仍可用）: {e}")
        return False


def export_summary_gdb(gdb_path, layer, center_x, center_y, counts: dict, *, crs):
    """图幅中心 + 计数统计点要素。"""
    try:
        _gdf([Point(center_x, center_y)],
             {"TOTAL": [int(sum(counts.values()))], "N_SESS": [len(counts)]},
             crs=crs).to_file(str(gdb_path), layer=layer, driver="OpenFileGDB")
    except Exception as e:
        WARNINGS.append(f"统计层导出失败: {e}")
