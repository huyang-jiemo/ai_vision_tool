"""坐标系工具：解析、校验、转换与诊断。

约定：三条管线的输入 CRS 一律从数据自身读取；缺失或损坏时抛出
``CRSMissingError``，由界面要求用户填写 EPSG/WKT（或 .prj 路径），不猜测。
输出 CRS 默认跟随主输入，可在任务参数中显式覆盖。
"""
from __future__ import annotations

import math
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


# 粘贴自网页/文档的 CRS 常带不可见字符；全角冒号也会让 pyproj 无法识别。
_CRS_INVISIBLE = dict.fromkeys(
    map(ord, "\ufeff\u200b\u200c\u200d\u2060"), None)
_PROJ_FALLBACK_DONE = False


def _normalize_crs_text(text) -> str:
    """去掉 BOM/零宽字符并归一化全角冒号，避免「看起来是 EPSG:4547」却解析失败。"""
    return str(text).translate(_CRS_INVISIBLE).replace("：", ":").strip()


def _retry_with_bundled_proj() -> bool:
    """PROJ_DATA/PROJ_LIB 指向缺库目录时，切回 pyproj 随包数据库。

    返回 True 表示已切换，调用方可重试；每个进程只尝试一次。
    """
    global _PROJ_FALLBACK_DONE
    if _PROJ_FALLBACK_DONE:
        return False
    _PROJ_FALLBACK_DONE = True
    try:
        import pyproj
        from pyproj import datadir
        bundled = (Path(pyproj.__file__).resolve().parent
                   / "proj_dir" / "share" / "proj")
        if not (bundled / "proj.db").is_file():
            return False
        datadir.set_data_dir(str(bundled))
        return True
    except Exception:
        return False


def _as_crs_with_fallback(value):
    try:
        return _as_crs(value)
    except Exception:
        if not _retry_with_bundled_proj():
            raise
        return _as_crs(value)


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
    text = _normalize_crs_text(value)
    # 资源管理器「复制为路径」会带引号
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = _normalize_crs_text(text[1:-1])
    if not text:
        if allow_none:
            return None
        raise CRSMissingError("未提供坐标系。", field=field,
                              hint="请填写 EPSG:xxxx 或 WKT。")
    # 允许直接填 .prj/.wkt 文件路径
    if os.path.isfile(text) and text.lower().endswith((".prj", ".wkt", ".txt")):
        try:
            text = Path(text).read_text(
                encoding="utf-8-sig", errors="replace").strip()
        except Exception as e:
            raise CRSInvalidError(
                f"无法读取坐标系文件：{value}（{e}）", field=field,
                hint="请确认文件可读，或直接填写 EPSG:xxxx / WKT。") from e
    try:
        crs = _as_crs_with_fallback(text)
    except Exception as e:
        raise CRSInvalidError(
            f"坐标系无法识别：{value}（{e}）", field=field,
            hint="示例：EPSG:4326 或粘贴 WKT / .prj 路径。") from e
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


class _ComposedTransformer:
    """把多个 Transformer 依次串起来（中间 CRS 可取 datum 的默认转换）。"""

    def __init__(self, steps):
        self.steps = list(steps)

    def transform(self, x, y):
        for tf in self.steps:
            x, y = tf.transform(x, y)
        return x, y


def compose_transformers(steps):
    """组合转换器：过滤 None；空返回 None，单个直接返回。"""
    live = [s for s in steps if s is not None]
    if not live:
        return None
    if len(live) == 1:
        return live[0]
    return _ComposedTransformer(live)


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


def transform_bounds_with(bounds, tf, densify_pts: int = 21):
    """用给定转换器（可为 None）把边界框四边密度化后取包络。"""
    left, bottom, right, top = (float(v) for v in bounds)
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
        raise CRSTransformError(f"无法转换范围 {bounds}。")
    return (min(xs), min(ys), max(xs), max(ys))


def transform_bounds(bounds, src_crs, dst_crs, densify_pts: int = 21):
    """把边界框从 src 转到 dst；同 CRS 直接返回，否则密度化取包络。"""
    box = tuple(float(v) for v in bounds)
    if src_crs is None or dst_crs is None:
        return box
    try:
        if _as_crs(src_crs).equals(_as_crs(dst_crs), ignore_axis_order=True):
            return box
    except Exception:
        pass
    return transform_bounds_with(
        box, build_transformer(src_crs, dst_crs), densify_pts)


def geographic_center(crs, bounds) -> tuple[float, float]:
    """数据范围中心 → WGS84 经纬度。"""
    left, bottom, right, top = (float(v) for v in bounds)
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    c = _as_crs(crs)
    # 地理 CRS 也要转换：非 Greenwich 本初子午线（如 NTF Paris）或
    # 非度单位（grad）时，原始 cx/cy 不能直接当作 WGS84 经纬度。
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
            file_crs = _as_crs_with_fallback(raw)
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
            recovered = horizontal_crs(_as_crs_with_fallback(wkt))
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


# ------------------------------------------------------------- 矢量 CRS 推断

@dataclass
class InferredCRS:
    crs: object
    source: str
    score: float
    reason: str


def inference_roots(*paths) -> list[str]:
    """给缺 CRS 的矢量数据收集可搜索的邻近目录。

    优先返回各路径的公共祖先（如 测试/），这样同项目下的轨迹 .shp/.prj
    可以作为推断依据；公共祖先退化为磁盘根/用户目录时只保留各自父目录。
    """
    dirs: list[str] = []
    for p in paths:
        if not p:
            continue
        ap = os.path.abspath(str(p))
        if os.path.isdir(ap) and ap.lower().endswith(".gdb"):
            # FileGDB 本身是目录，但推断要搜索的是它所在的项目目录
            d = os.path.dirname(ap)
        elif os.path.isdir(ap):
            d = ap
        else:
            d = os.path.dirname(ap)
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    if not dirs:
        return []
    try:
        common = os.path.commonpath(dirs)
    except ValueError:
        return dirs
    # 根目录（POSIX "/" 或 Windows "D:\\"）与用户主目录范围过大，不能整盘扫描
    at_root = os.path.dirname(common) == common
    if common == os.path.expanduser("~"):
        return dirs
    if at_root:
        # 仍然搜各自父目录，但不要把整个磁盘根当搜索起点
        return [d for d in dirs if os.path.dirname(d) != d] or dirs
    return [common] + [d for d in dirs if d != common]


def _bbox_area(b) -> float:
    return max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))


def _bbox_inter_area(a, b) -> float:
    iw = max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0])))
    ih = max(0.0, min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1])))
    return iw * ih


def _bbox_cover_ratio(target, cand) -> float:
    """候选范围覆盖目标范围的比例（目标完全落在候选内时为 1）。"""
    denom = min(_bbox_area(target), _bbox_area(cand))
    return _bbox_inter_area(target, cand) / denom if denom > 0 else 0.0


def _bbox_overlap_ratio(a, b) -> float:
    denom = min(_bbox_area(a), _bbox_area(b))
    return _bbox_inter_area(a, b) / denom if denom > 0 else 0.0


def _vector_bounds(path, layers=None):
    """矢量数据集（可指定图层）的总范围；没有可用范围返回 None。"""
    import pyogrio
    try:
        names = list(layers) if layers is not None else [
            n for n, g in pyogrio.list_layers(path)
            if g and not str(n).startswith("~") and not str(n).endswith(".xlsx")]
    except Exception:
        return None
    boxes = []
    for name in names:
        b = None
        try:
            b = pyogrio.read_info(path, layer=name).get("total_bounds")
        except Exception:
            pass
        if b is None or len(b) != 4:
            try:
                b = pyogrio.read_bounds(path, layer=name)
            except Exception:
                b = None
        if b is not None and len(b) == 4:
            vals = [float(v) for v in b]
            if all(math.isfinite(v) for v in vals) and vals[2] >= vals[0] \
                    and vals[3] >= vals[1]:
                boxes.append(tuple(vals))
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _iter_shapefiles(roots, *, max_candidates=80, max_depth=5):
    seen: set[str] = set()
    count = 0
    for root in roots:
        base = os.path.abspath(str(root))
        if os.path.isfile(base):
            base = os.path.dirname(base)
        if not os.path.isdir(base) or base.lower().endswith(".gdb"):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel = os.path.relpath(dirpath, base)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d.lower() != "__macosx"
                and not d.lower().endswith(".gdb")]
            for fn in filenames:
                if not fn.lower().endswith(".shp"):
                    continue
                fp = os.path.abspath(os.path.join(dirpath, fn))
                if fp in seen:
                    continue
                seen.add(fp)
                yield fp
                count += 1
                if count >= max_candidates:
                    return


def infer_vector_crs(path, *, roots=(), anchor_crs=None, anchor_bounds=None,
                     layers=None, max_candidates=80) -> InferredCRS | None:
    """按邻近矢量的坐标范围推断缺失 CRS；证据不足时返回 None，不猜测。

    两重证据：
    1. 邻近 Shapefile 与目标数据的原始坐标范围高度重合（同一套坐标）；
    2. 候选 CRS 转换到主输入 CRS 后，范围与主输入影像/成果重叠。

    只有最佳候选明显领先（或与主输入重叠很强）才采用，避免静默用错坐标系。
    """
    import pyogrio
    target = _vector_bounds(path, layers)
    if target is None or _bbox_area(target) <= 0:
        return None
    best: dict[str, dict] = {}
    anchor_used = anchor_crs is not None and anchor_bounds is not None
    for shp in _iter_shapefiles(roots, max_candidates=max_candidates):
        try:
            info = pyogrio.read_info(shp)
            raw = info.get("crs")
            cb = info.get("total_bounds")
            if not raw or cb is None or len(cb) != 4:
                continue
            cand = _as_crs_with_fallback(raw)
            if not usable_crs(cand):
                continue
        except Exception:
            continue
        cb = tuple(float(v) for v in cb)
        if not all(math.isfinite(v) for v in cb):
            continue
        raw_ratio = _bbox_cover_ratio(target, cb)
        if raw_ratio < 0.3:
            continue
        anchor_ratio = 1.0
        if anchor_crs is not None and anchor_bounds is not None:
            try:
                tb = transform_bounds(target, cand, anchor_crs)
                anchor_ratio = _bbox_overlap_ratio(tb, anchor_bounds)
            except Exception:
                continue
            if anchor_ratio < 0.05:
                continue
        score = 2.0 * anchor_ratio + raw_ratio
        key = cand.to_epsg() or cand.name or str(cand)
        old = best.get(key)
        if old is None or score > old["score"]:
            best[key] = {"crs": cand, "source": shp, "score": score,
                         "raw": raw_ratio, "anchor": anchor_ratio}
    if not best:
        return None
    ranked = sorted(best.values(), key=lambda d: d["score"], reverse=True)
    top = ranked[0]
    if len(ranked) > 1:
        second = ranked[1]
        # 多个不同 CRS 得分接近、又没有主输入范围可裁决 → 证据不足，不猜
        if top["score"] - second["score"] < 0.5 and not anchor_used:
            return None
        if top["score"] - second["score"] < 0.5 and top["anchor"] < 0.8:
            return None
    reason = (f"按邻近矢量「{Path(top['source']).name}」的坐标范围推断"
              f"（范围重合 {top['raw']:.0%}"
              + (f"，与主输入重叠 {top['anchor']:.0%}" if anchor_crs is not None
                 else "") + "）")
    return InferredCRS(crs=horizontal_crs(top["crs"]),
                       source=top["source"], score=top["score"], reason=reason)


# ------------------------------------------------------------- 矢量 GDB CRS


@dataclass
class GdbCRS:
    per_layer: dict = dataclass_field(default_factory=dict)
    counts: dict = dataclass_field(default_factory=dict)
    primary: object = None
    distinct: list = dataclass_field(default_factory=list)
    origin: str = "file"
    note: str = ""

    def report(self) -> dict:
        return {
            "primary": crs_label(self.primary),
            "layers": {k: crs_label(v) for k, v in self.per_layer.items()},
            "distinct": [crs_label(c) for c in self.distinct],
            "origin": self.origin,
            "note": self.note,
        }


def inspect_gdb_crs(path, override=None, *, field: str = "input_crs",
                    infer_roots=(), anchor_crs=None,
                    anchor_bounds=None) -> GdbCRS:
    """逐层读取矢量数据 CRS；缺失层用 override，仍缺失则尝试自动推断。

    自动推断需要 ``infer_roots``（邻近目录）和/或主输入范围作为锚点；
    证据不足时仍抛 ``CRSMissingError``，绝不静默猜测。
    """
    import pyogrio
    user = parse_crs(override, field=field)
    per_layer: dict = {}
    counts: dict[str, int] = {}
    missing: list[str] = []
    origin = "user" if user is not None else "file"
    note = ""
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
                crs = _as_crs_with_fallback(raw)
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
        inferred = infer_vector_crs(
            path, roots=infer_roots, anchor_crs=anchor_crs,
            anchor_bounds=anchor_bounds, layers=missing)
        if inferred is not None:
            for name in missing:
                per_layer[name] = inferred.crs
                counts.setdefault(name, 0)
            origin = "inferred"
            note = inferred.reason
        else:
            names = "、".join(missing[:5]) + ("…" if len(missing) > 5 else "")
            hint = "可填写数据实际使用的 EPSG:xxxx、粘贴 WKT，或选择 .prj 文件。"
            if infer_roots:
                hint = ("已尝试按邻近矢量的坐标范围自动判断，但证据不足。"
                        + hint)
            raise CRSMissingError(
                f"矢量数据「{Path(path).name}」图层「{names}」缺少坐标系。"
                f"请在对应的坐标系输入框中填写 EPSG:xxxx 或 WKT 后重试。",
                field=field, hint=hint, detail=f"layers={missing}")
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
                  origin=origin, note=note)


# ---------------------------------------------------------------- WKT 写出


def crs_to_esri_wkt(crs) -> str:
    """写 .prj 用：优先 ESRI WKT1；带基准转换时改用 WKT1_GDAL。

    ESRI WKT1 会把 BoundCRS 的 TOWGS84 只写进基准名称（如
    ``D_Unknown_..._using_towgs84_...``），重新读取后转换参数丢失，坐标会
    回退到 PROJ 默认基准转换。此时优先 WKT1_GDAL（保留 ``TOWGS84[...]``），
    ArcGIS/QGIS 均可读取。
    """
    c = _as_crs(crs)
    try:
        is_bound = bool(c.is_bound)
    except Exception:
        is_bound = False
    versions = (("WKT1_GDAL", "WKT1_ESRI") if is_bound
                else ("WKT1_ESRI", "WKT1_GDAL"))
    for version in versions:
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
