"""坐标系工具：解析、校验、转换与诊断。

约定：三条管线的输入 CRS 一律从数据自身读取；缺失或损坏时抛出
``CRSMissingError``，由界面要求用户填写 EPSG/WKT（或 .prj 路径），不猜测。
输出 CRS 默认跟随主输入，可在任务参数中显式覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path

# ------------------------------------------------------------------ 错误类型


class CRSError(ValueError):
    """坐标系相关错误的基类：携带错误码与界面字段名。"""

    code = "CRS_ERROR"

    def __init__(self, message: str, *, field: str | None = None,
                 hint: str = "", detail: str = ""):
        super().__init__(message)
        self.field = field
        self.hint = hint
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": str(self), "error_code": self.code,
                "crs_field": self.field, "hint": self.hint}


class CRSMissingError(CRSError):
    """输入缺少或损坏坐标系，需要用户显式指定。"""

    code = "CRS_REQUIRED"


class CRSInvalidError(CRSError):
    """用户提供的坐标系无法解析或类型不受支持。"""

    code = "CRS_INVALID"


class CRSTransformError(CRSError):
    """两个 CRS 之间无法建立转换。"""

    code = "CRS_TRANSFORM_FAILED"


# ------------------------------------------------------------------ 基础工具


def describe(crs) -> str:
    """人类可读的坐标系描述，如「WuHan_WH2000（Projected CRS）」。"""
    if crs is None:
        return "未定义"
    try:
        return f"{crs.name or '未命名'}（{crs.type_name}）"
    except Exception:
        return "未知坐标系"


def crs_label(crs) -> str:
    """短标签：优先 EPSG 代码，其次名称；局部 TM 显示中央经线。"""
    if crs is None:
        return "未指定"
    try:
        epsg = crs.to_epsg()
        name = (crs.name or "").strip()
        if epsg:
            return f"EPSG:{epsg}（{name}）" if name else f"EPSG:{epsg}"
        try:
            import warnings
            with warnings.catch_warnings():
                # pyproj 从 WKT 转 PROJ 参数时会提示信息损失；此处仅取中央经线
                warnings.simplefilter("ignore")
                d = crs.to_dict()
            if d.get("proj") == "tmerc" and d.get("lon_0") is not None:
                return f"局部 TM（中央经线 {float(d['lon_0']):.6f}°）"
        except Exception:
            pass
        if name and name.lower() not in ("unknown", "unnamed"):
            return name
        return "自定义坐标系"
    except Exception:
        return "未知坐标系"


def _as_crs(value):
    from pyproj import CRS
    return value if isinstance(value, CRS) else CRS.from_user_input(value)


def usable_crs(crs) -> bool:
    """是否为可用于水平坐标转换的 2D CRS（排除工程/地心坐标系）。"""
    if crs is None:
        return False
    try:
        return not crs.is_engineering and not crs.is_geocentric
    except Exception:
        return False


def horizontal_crs(crs):
    """取 2D 水平分量；compound 取第一个水平子 CRS。"""
    c = _as_crs(crs)
    try:
        if c.is_compound:
            for sub in c.sub_crs_list:
                if sub.is_projected or sub.is_geographic:
                    return sub
            raise CRSInvalidError(
                f"坐标系「{c.name or c}」不包含可用的水平分量。",
                hint="请提供 2D 地理坐标系（如 EPSG:4326）或投影坐标系。")
        if c.is_geocentric:
            raise CRSInvalidError(
                f"不支持地心坐标系「{c.name or c}」。",
                hint="请提供 2D 地理坐标系或投影坐标系。")
        if c.is_engineering:
            raise CRSInvalidError(
                f"不支持工程坐标系「{c.name or c}」。",
                hint="请提供 2D 地理坐标系或投影坐标系。")
    except CRSError:
        raise
    except Exception:
        pass
    return c


def parse_crs(value, *, field: str = "crs", allow_none: bool = True):
    """解析 EPSG/WKT/PROJ 字符串或 .prj/.wkt 文件路径。

    空值在 ``allow_none=True`` 时返回 None；无法解析抛 ``CRSInvalidError``。
    """
    if value is None:
        if allow_none:
            return None
        raise CRSMissingError("未提供坐标系。", field=field,
                              hint="请填写 EPSG:xxxx 或 WKT。")
    text = str(value).strip()
    # 资源管理器「复制为路径」会带引号
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        if allow_none:
            return None
        raise CRSMissingError("未提供坐标系。", field=field,
                              hint="请填写 EPSG:xxxx 或 WKT。")
    # 允许直接填 .prj/.wkt 文件路径
    if os.path.isfile(text) and text.lower().endswith((".prj", ".wkt", ".txt")):
        try:
            text = Path(text).read_text(encoding="utf-8-sig", errors="replace").strip()
        except Exception as e:
            raise CRSInvalidError(
                f"无法读取坐标系文件：{value}（{e}）", field=field,
                hint="请确认文件可读，或直接填写 EPSG:xxxx / WKT。") from e
    try:
        crs = _as_crs(text)
    except Exception as e:
        raise CRSInvalidError(
            f"坐标系无法识别：{value}（{e}）", field=field,
            hint="示例：EPSG:4547、EPSG:4326，或粘贴 WKT / .prj 路径。") from e
    return horizontal_crs(crs)


def is_projected_metric(crs) -> bool:
    """是否为投影坐标系且水平单位为米。"""
    try:
        if crs is None or not crs.is_projected:
            return False
        axes = crs.axis_info
        if not axes:
            return False
        factor = float(axes[0].unit_conversion_factor)
        return abs(factor - 1.0) <= 1e-9
    except Exception:
        return False


def build_transformer(src_crs, dst_crs, *, field: str | None = None):
    """同 CRS 返回 None，否则返回 always_xy 的 pyproj Transformer。"""
    if src_crs is None or dst_crs is None:
        return None
    src = _as_crs(src_crs)
    dst = _as_crs(dst_crs)
    try:
        if src.equals(dst, ignore_axis_order=True):
            return None
    except Exception:
        pass
    from pyproj import Transformer
    try:
        return Transformer.from_crs(src, dst, always_xy=True)
    except Exception as e:
        raise CRSTransformError(
            f"无法在「{describe(src)}」与「{describe(dst)}」之间建立坐标转换"
            f"（{e}）", field=field, detail=str(e)) from e


def transform_bounds(bounds, src_crs, dst_crs, densify_pts: int = 21):
    """把边界框从 src 转到 dst；四边密度化后取包络。"""
    left, bottom, right, top = (float(v) for v in bounds)
    if src_crs is None or dst_crs is None:
        return (left, bottom, right, top)
    src = _as_crs(src_crs)
    dst = _as_crs(dst_crs)
    try:
        if src.equals(dst, ignore_axis_order=True):
            return (left, bottom, right, top)
    except Exception:
        pass
    tf = build_transformer(src, dst)
    if tf is None:
        return (left, bottom, right, top)
    xs: list[float] = []
    ys: list[float] = []
    n = max(2, int(densify_pts))
    for i in range(n):
        f = i / (n - 1)
        px = left + (right - left) * f
        py = bottom + (top - bottom) * f
        for x, y in ((px, bottom), (px, top), (left, py), (right, py)):
            try:
                xx, yy = tf.transform(x, y)
            except Exception:
                continue
            if xx == xx and yy == yy and abs(xx) != float("inf") and abs(yy) != float("inf"):
                xs.append(float(xx))
                ys.append(float(yy))
    if not xs:
        raise CRSTransformError(
            f"无法把范围 {bounds} 从「{describe(src)}」转换到「{describe(dst)}」。")
    return (min(xs), min(ys), max(xs), max(ys))


def geographic_center(crs, bounds) -> tuple[float, float]:
    """数据范围中心 → WGS84 经纬度。"""
    left, bottom, right, top = (float(v) for v in bounds)
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    c = _as_crs(crs)
    if c.is_geographic:
        return cx, cy
    from pyproj import Transformer
    tf = Transformer.from_crs(c, "EPSG:4326", always_xy=True)
    lon, lat = tf.transform(cx, cy)
    return float(lon), float(lat)


# ------------------------------------------------------- GeoTIFF 元数据恢复


def dataset_esri_wkt(ds) -> str:
    """读取 GeoTIFF 中 ArcGIS 写入的 ESRI PE String（完整投影 WKT）。"""
    for ns in (None, "xml:ESRI", "ESRI"):
        try:
            tags = ds.tags(ns=ns) if ns else ds.tags()
        except Exception:
            continue
        if not isinstance(tags, dict):
            continue
        for key, value in tags.items():
            if not isinstance(value, str):
                continue
            k = str(key).upper().replace("_", " ")
            v = value.lstrip().upper()
            if "ESRI PE STRING" in k or v.startswith(("PROJCS[", "PROJCRS[")):
                return value
    return ""


def suggest_crs_candidates(bounds) -> list[dict]:
    """根据坐标范围给出可能的 CRS，仅用于错误提示，不自动应用。"""
    try:
        left, bottom, right, top = (float(v) for v in bounds)
    except Exception:
        return []
    out: list[dict] = []
    if (all(-180.0 <= v <= 180.0 for v in (left, right))
            and all(-90.0 <= v <= 90.0 for v in (bottom, top))):
        out.append({"epsg": 4326, "label": "WGS 84",
                    "reason": "坐标范围符合经纬度（度）"})
    if (all(abs(v) <= 20037508.342789244 for v in (left, bottom, right, top))
            and abs((left + right) / 2.0) > 1.0e6):
        out.append({"epsg": 3857, "label": "WGS 84 / Pseudo-Mercator",
                    "reason": "坐标范围符合 Web Mercator（米）"})
    return out


def resolve_raster_crs(ds, input_crs=None, *, field: str = "input_crs"):
    """解析栅格 CRS；返回 (CRS, origin, note)。

    顺序：用户显式值 > 文件有效 CRS > ESRI PE String > CRSMissingError。
    """
    from pyproj import CRS
    user = parse_crs(input_crs, field=field)
    raw = getattr(ds, "crs", None)
    file_crs = None
    if raw:
        try:
            file_crs = CRS.from_user_input(raw)
        except Exception:
            file_crs = None
    if user is not None:
        note = ""
        if file_crs is not None and usable_crs(file_crs):
            note = f"用户指定的坐标系覆盖了文件自带坐标系（{describe(file_crs)}）"
        return horizontal_crs(user), "user", note
    if usable_crs(file_crs):
        return horizontal_crs(file_crs), "file", ""
    wkt = dataset_esri_wkt(ds)
    if wkt:
        try:
            recovered = horizontal_crs(CRS.from_wkt(wkt))
            return recovered, "esri_pe_string", "按文件元数据 ESRI PE String 恢复"
        except Exception:
            pass
    candidates = suggest_crs_candidates(getattr(ds, "bounds", None) or ())
    hint = ""
    if candidates:
        hint = "根据坐标范围，可能是 " + "、".join(
            f"EPSG:{c['epsg']}" for c in candidates) + "。"
    raise CRSMissingError(
        f"影像缺少有效坐标系（当前为 {describe(file_crs) if file_crs else '未定义'}）。"
        f"请在对应坐标系输入框中填写 EPSG:xxxx 或 WKT（也可选择 .prj 文件）后重试。",
        field=field, hint=hint,
        detail=str(raw) if raw else "")


# ------------------------------------------------------------- 矢量 GDB CRS


@dataclass
class GdbCRS:
    per_layer: dict = dataclass_field(default_factory=dict)
    counts: dict = dataclass_field(default_factory=dict)
    primary: object = None
    distinct: list = dataclass_field(default_factory=list)
    origin: str = "file"

    def report(self) -> dict:
        return {
            "primary": crs_label(self.primary),
            "layers": {k: crs_label(v) for k, v in self.per_layer.items()},
            "distinct": [crs_label(c) for c in self.distinct],
            "origin": self.origin,
        }


def inspect_gdb_crs(path, override=None, *, field: str = "input_crs") -> GdbCRS:
    """逐层读取矢量数据 CRS；缺失层用 override，仍缺失则报错。"""
    import pyogrio
    from pyproj import CRS
    user = parse_crs(override, field=field)
    per_layer: dict = {}
    counts: dict[str, int] = {}
    missing: list[str] = []
    origin = "user" if user is not None else "file"
    # 跳过非空间表（list_layers 的几何类型为 None）：它们没有 CRS，
    # 也不参与裁剪/走廊/比对，不应因此阻断任务。
    layers = [(n, g) for n, g in pyogrio.list_layers(path)
              if not n.startswith("~") and not n.endswith(".xlsx") and g]
    if not layers:
        raise CRSMissingError(
            f"矢量数据「{Path(path).name}」没有可读的空间图层。", field=field)
    for name, _geom in layers:
        crs = None
        nfeat = 0
        try:
            info = pyogrio.read_info(path, layer=name)
            nfeat = int(info.get("features") or 0)
            raw = info.get("crs")
            if raw:
                crs = CRS.from_user_input(raw)
                if not usable_crs(crs):
                    crs = None
        except Exception:
            crs = None
        if crs is None:
            if user is not None:
                crs = user
            elif nfeat == 0:
                # 空图层没有要素可处理，缺少 CRS 不应阻断任务
                continue
            else:
                missing.append(name)
                continue
        per_layer[name] = horizontal_crs(crs)
        counts[name] = nfeat
    if missing and user is None:
        names = "、".join(missing[:5]) + ("…" if len(missing) > 5 else "")
        raise CRSMissingError(
            f"矢量数据「{Path(path).name}」图层「{names}」缺少坐标系。"
            f"请在对应的坐标系输入框中填写 EPSG:xxxx 或 WKT 后重试。",
            field=field,
            hint="可填写 EPSG:4547、EPSG:4326，或选择 .prj 文件。",
            detail=f"layers={missing}")
    if not per_layer:
        raise CRSMissingError(
            f"矢量数据「{Path(path).name}」没有带有效坐标系的图层。", field=field)
    distinct: list = []
    for c in per_layer.values():
        if not any(c.equals(d, ignore_axis_order=True) for d in distinct):
            distinct.append(c)
    if len(distinct) > 1:
        origin = "mixed"
    primary_layer = max(counts, key=lambda n: counts.get(n, 0)) if counts else next(iter(per_layer))
    return GdbCRS(per_layer=per_layer, counts=counts,
                  primary=per_layer[primary_layer], distinct=distinct,
                  origin=origin)


# ---------------------------------------------------------------- WKT 写出


def crs_to_esri_wkt(crs) -> str:
    """写 .prj 用：优先 ESRI WKT1，失败回退 WKT1/WKT2。"""
    c = _as_crs(crs)
    for version in ("WKT1_ESRI", "WKT1_GDAL"):
        try:
            wkt = c.to_wkt(version=version, pretty=True)
            if wkt:
                return wkt
        except Exception:
            continue
    return c.to_wkt(pretty=True)


def runtime_info() -> str:
    """pyproj/PROJ 运行环境摘要，供排障日志使用。"""
    try:
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
