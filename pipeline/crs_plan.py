"""每次任务的坐标系计划：source / target / working 与转换器缓存。

- source：主输入自身声明的 CRS；
- target：成果输出 CRS，默认等于 source，可由用户覆盖；
- working：内部米制计算 CRS，所有距离/面积/缓冲/匹配都在此系完成。

working 选择规则：目标本身是米制投影且中心处尺度接近 1、子午线收敛角足够小时
直接使用目标；否则构造以数据中心经线为中央经线的局部 Transverse Mercator，
保证地面距离与方向（车载 heading）在任务范围内准确。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from . import coords

# 允许直接使用目标 CRS 作为 working 的阈值
_SCALE_TOL = 1.0e-3
_AREA_TOL = 2.0e-3
_CONVERGENCE_TOL_DEG = 0.25


def projection_factors(crs, lon: float, lat: float) -> dict | None:
    """返回投影在给定经纬度处的尺度/收敛角；不可用时返回 None。"""
    try:
        from pyproj import Proj
        factors = Proj(crs).get_factors(float(lon), float(lat))
        if factors is None:
            return None
        return {
            "meridional_scale": float(factors.meridional_scale),
            "parallel_scale": float(factors.parallel_scale),
            "areal_scale": float(factors.areal_scale),
            "meridian_convergence": float(factors.meridian_convergence),
        }
    except Exception:
        return None


def _datum_params(target_crs) -> str:
    """提取目标基准/椭球参数，供局部 TM 使用；剔除本初子午线。"""
    geod = target_crs
    try:
        if target_crs.is_projected and target_crs.geodetic_crs is not None:
            geod = target_crs.geodetic_crs
    except Exception:
        geod = target_crs
    parts: list[str] = []
    try:
        import warnings
        with warnings.catch_warnings():
            # 仅提取 datum/椭球参数，避免 pyproj 的 PROJ 字符串信息损失提示
            warnings.simplefilter("ignore")
            d = geod.to_dict()
        datum = d.get("datum")
        if datum:
            parts.append(f"+datum={datum}")
        else:
            ell = target_crs.ellipsoid
            a = float(ell.semi_major_metre)
            rf = float(ell.inverse_flattening)
            b = float(ell.semi_minor_metre)
            if a > 0 and rf and abs(rf) > 1e-9:
                parts.append(f"+a={a:.6f} +rf={rf:.12f}")
            elif a > 0 and b > 0:
                parts.append(f"+a={a:.6f} +b={b:.6f}")
        for key in ("towgs84", "nadgrids"):
            val = d.get(key)
            if val:
                if isinstance(val, (list, tuple)):
                    val = ",".join(str(x) for x in val)
                parts.append(f"+{key}={val}")
    except Exception:
        parts = []
    if not parts:
        parts = ["+ellps=WGS84"]
    return " ".join(parts)


def local_tm_crs(target_crs, lon_0: float):
    """以 lon_0 为中央经线、保留目标基准/椭球的局部 Transverse Mercator。"""
    from pyproj import CRS
    params = (
        f"+proj=tmerc +lat_0=0 +lon_0={float(lon_0):.10f} +k=1 "
        f"+x_0=500000 +y_0=0 {_datum_params(target_crs)} +units=m +no_defs +type=crs"
    )
    return CRS.from_proj4(params)


def working_crs_for(target_crs, center_lon: float, center_lat: float,
                    *, force_local_tm: bool = False):
    """返回 (working_crs, reason)。"""
    if not force_local_tm and coords.is_projected_metric(target_crs):
        f = projection_factors(target_crs, center_lon, center_lat)
        if f is not None:
            ok = (
                abs(f["meridional_scale"] - 1.0) <= _SCALE_TOL
                and abs(f["parallel_scale"] - 1.0) <= _SCALE_TOL
                and abs(f["areal_scale"] - 1.0) <= _AREA_TOL
                and abs(f["meridian_convergence"]) <= _CONVERGENCE_TOL_DEG
            )
            if ok:
                return target_crs, "target"
    return local_tm_crs(target_crs, center_lon), "local_tm"


@dataclass
class CRSPlan:
    source_crs: object
    target_crs: object
    working_crs: object
    source_origin: str = "file"
    target_origin: str = "default:source"
    center_lon: float = 0.0
    center_lat: float = 0.0
    working_reason: str = ""
    _tf_cache: dict = dataclass_field(default_factory=dict, repr=False, compare=False)

    def transformer(self, src, dst, *, field: str | None = None):
        key = (str(src), str(dst))
        if key not in self._tf_cache:
            self._tf_cache[key] = coords.build_transformer(src, dst, field=field)
        return self._tf_cache[key]

    def to_working(self):
        return self.transformer(self.source_crs, self.working_crs)

    def to_target(self):
        return self.transformer(self.working_crs, self.target_crs)

    def to_source(self):
        return self.transformer(self.working_crs, self.source_crs)

    def report(self) -> dict:
        return {
            "source_crs": coords.crs_label(self.source_crs),
            "target_crs": coords.crs_label(self.target_crs),
            "working_crs": (coords.crs_label(self.working_crs)
                            if self.working_reason == "target" else
                            f"局部 TM（中央经线 {self.center_lon:.6f}°）"),
            "source_origin": self.source_origin,
            "target_origin": self.target_origin,
            "working_reason": self.working_reason,
        }


def make_plan(source_crs, target_crs=None, *, center_lon: float, center_lat: float,
              source_origin: str = "file", target_origin: str = "",
              force_local_tm: bool = False) -> CRSPlan:
    target = target_crs if target_crs is not None else source_crs
    working, reason = working_crs_for(
        target, center_lon, center_lat, force_local_tm=force_local_tm)
    return CRSPlan(
        source_crs=source_crs, target_crs=target, working_crs=working,
        source_origin=source_origin,
        target_origin=target_origin or ("user" if target_crs is not None else "default:source"),
        center_lon=float(center_lon), center_lat=float(center_lat),
        working_reason=reason,
    )


def plan_for_bounds(source_crs, target_crs=None, bounds=None, *,
                    source_origin: str = "file", target_origin: str = "",
                    force_local_tm: bool = False) -> CRSPlan:
    lon, lat = coords.geographic_center(source_crs, bounds)
    return make_plan(source_crs, target_crs,
                     center_lon=lon, center_lat=lat,
                     source_origin=source_origin, target_origin=target_origin,
                     force_local_tm=force_local_tm)
