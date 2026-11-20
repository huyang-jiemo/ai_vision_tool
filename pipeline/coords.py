"""坐标系工具：同投影判断、Transformer 构建与可读诊断。

背景：`pyproj.Transformer.from_crs` 在 `proj_create_crs_to_crs` 返回空时只抛
通用文案 `Error creating Transformer from CRS.`，看不出真实原因。正射提取在
真机上报该错时，实测对应两类影像：

1. 影像使用本地/未定义坐标系（GeoTIFF 只有 LOCAL_CS，PROJ 中为 Engineering
   CRS），PROJ 无法在它与武汉本地 TM 之间建立任何转换；
2. 影像本来就是武汉本地 TM，但 datum 名称不同导致 `CRS.equals()` 为 False，
   本不需要转换却走进了转换链路。

本模块统一两种判断，保证：同投影直接跳过转换；本地坐标系且坐标范围与武汉
本地 TM 吻合时按源坐标处理并告警；其余情况给出可执行的中文错误。
"""
from __future__ import annotations

# 武汉本地 TM（x_0=800000, y_0=-3000000）的合理坐标区间，覆盖武汉全域并留余量。
# 实测：114.0°E/30.3°N → (767935, 353417)，114.6°E/30.9°N → (825494, 419918)。
LOCAL_TM_X_RANGE = (6.0e5, 1.0e6)
LOCAL_TM_Y_RANGE = (2.0e5, 6.0e5)

# 横轴墨卡托参数（缺省值按 PROJ 约定：lat_0=0, k=1, x_0=y_0=0）
_TM_PARAMS = {"lat_0": 0.0, "lon_0": 0.0, "k": 1.0, "x_0": 0.0, "y_0": 0.0}


def describe(crs) -> str:
    """人类可读的坐标系描述，如「WuHan_WH2000（Projected CRS）」。"""
    try:
        return f"{crs.name or '未命名'}（{crs.type_name}）"
    except Exception:
        return "未知坐标系"


def same_projection(a, b) -> bool:
    """两个投影坐标系的方法与参数是否完全一致（忽略 datum 名称差异）。

    与 `CRS.equals()` 的区别：WKT 与 Proj4 描述的同一投影，datum 名称不同会
    让 equals() 判 False，但坐标数值完全一致，无需转换。
    """
    try:
        if not (a.is_projected and b.is_projected):
            return False
        # to_dict() 内部会经 PROJ 字符串转换，WKT 会触发一条无意义的 UserWarning
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            da, db = a.to_dict(), b.to_dict()
        if da.get("proj") != db.get("proj"):
            return False
        if da.get("units", "m") != db.get("units", "m"):
            return False
        for key, default in _TM_PARAMS.items():
            va = float(da.get(key, default))
            vb = float(db.get(key, default))
            if abs(va - vb) > 1e-9 * max(1.0, abs(va), abs(vb)):
                return False
        ea, eb = a.ellipsoid, b.ellipsoid
        if abs(ea.semi_major_metre - eb.semi_major_metre) > 1e-3:
            return False
        if abs(ea.inverse_flattening - eb.inverse_flattening) > 1e-6:
            return False
        # 任一侧带非零基准面偏移时不能跳过转换
        for d in (da, db):
            tw = d.get("towgs84") or []
            if any(abs(float(x)) > 1e-9 for x in tw):
                return False
        # 网格改正 / 中央子午线不同时同样不能跳过（宁可多建一次 Transformer）
        for key in ("nadgrids", "geoidgrids", "pm"):
            if (da.get(key) or db.get(key)) and da.get(key) != db.get(key):
                return False
        return True
    except Exception:
        return False


def bounds_look_like_local_tm(bounds) -> bool:
    """坐标范围中心是否落在武汉本地 TM 的合理区间。"""
    try:
        left, bottom, right, top = bounds
        cx = (float(left) + float(right)) / 2.0
        cy = (float(bottom) + float(top)) / 2.0
        return (LOCAL_TM_X_RANGE[0] <= cx <= LOCAL_TM_X_RANGE[1]
                and LOCAL_TM_Y_RANGE[0] <= cy <= LOCAL_TM_Y_RANGE[1])
    except Exception:
        return False


def build_transformer(src_crs, dst_crs):
    """同投影返回 None，否则返回 pyproj Transformer；失败抛 ValueError（含诊断）。"""
    if same_projection(src_crs, dst_crs):
        return None
    try:
        if src_crs.equals(dst_crs):
            return None
    except Exception:
        pass
    from pyproj import Transformer
    try:
        return Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    except Exception as e:
        raise ValueError(
            f"无法在「{describe(src_crs)}」与「{describe(dst_crs)}」之间建立坐标转换"
            f"（{e}）") from e


def runtime_info() -> str:
    """pyproj/PROJ 运行环境摘要，供排障日志使用。"""
    try:
        import os
        import pyproj
        from pyproj import datadir
        try:
            data_dir = datadir.get_data_dir()
            has_db = os.path.exists(os.path.join(data_dir, "proj.db"))
            data_info = f"{data_dir}（proj.db={'有' if has_db else '无'}）"
        except Exception as e:
            data_info = f"不可用（{e}）"
        return (f"pyproj={pyproj.__version__} PROJ={pyproj.proj_version_str} "
                f"数据目录={data_info}")
    except Exception as e:
        return f"pyproj 环境读取失败：{e}"
