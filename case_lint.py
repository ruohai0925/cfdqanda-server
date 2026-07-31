"""case_lint.py — 失败后案例体检:把"死因附近的可行动线索"提炼成给用户的提示。

背景:cfdqanda.com 专业诊断对 #614/#616/#617(本平台三个失败任务)的根因分析显示,
"timeout/OOM"这类资源型表象下往往有确定性可检的配置根因(固定压差压不可压求解器、
snappy 细化超预算、FSI 需求配了纯流体求解器、setFields 缺前置文件)。本模块把这些
规律做成零成本静态检查,失败时跑一遍,提示写进 result_data.lint_hints(warning-only,
绝不阻塞、绝不抛异常)。

用法:
    from case_lint import lint_hints
    hints = lint_hints(run_dir)     # -> list[str],无发现返回 []
"""
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 不可压求解器族(p/p_rgh 是相对压;固定大压差 → 无界加速,见 #617 诊断)
_INCOMPRESSIBLE = {"icoFoam", "pisoFoam", "pimpleFoam", "simpleFoam", "interFoam",
                   "interIsoFoam", "multiphaseInterFoam", "pimpleDyMFoam"}
# 标准纯流体求解器(FSI/接触字典对它们无效,见 #614 诊断)
_PURE_FLUID = _INCOMPRESSIBLE | {"buoyantFoam", "rhoPimpleFoam", "rhoSimpleFoam", "sonicFoam"}


def _dict_get(path: Path, key: str):
    if not path.is_file():
        return None
    m = re.search(rf"^\s*{re.escape(key)}\s+([^;{{]+);", path.read_text(errors="ignore"), re.M)
    return m.group(1).strip() if m else None


def _fixed_pressure_values(field_file: Path) -> list[float]:
    """0/p 或 0/p_rgh 里各 patch 的 fixedValue uniform 数值(边界压差检查用)。"""
    if not field_file.is_file():
        return []
    txt = field_file.read_text(errors="ignore")
    vals = []
    for m in re.finditer(r"type\s+fixedValue\s*;\s*value\s+uniform\s+([0-9.eE+-]+)\s*;", txt):
        try:
            vals.append(float(m.group(1)))
        except ValueError:
            pass
    return vals


def lint_hints(run_dir) -> list[str]:
    """对失败任务的 output/ 算例做静态体检 → 用户可读提示列表。异常安全:任何错误返回已有结果。"""
    hints: list[str] = []
    try:
        base = Path(run_dir)
        case = base / "output" if (base / "output").is_dir() else base
        app = _dict_get(case / "system" / "controlDict", "application") or ""

        # ① 固定压差 + 不可压求解器(#617:80 kPa 压差 → 空气无界加速 → deltaT 坍缩 → 永不完)
        if app in _INCOMPRESSIBLE:
            for pname in ("p_rgh", "p"):
                vals = _fixed_pressure_values(case / "0" / pname)
                if len(vals) >= 2 and max(vals) - min(vals) > 5e3:
                    hints.append(
                        f"检测到 0/{pname} 中多个边界固定压力值相差 {max(vals)-min(vals):.3g}"
                        f"(如进出口恒定压差)。{app} 是不可压求解器,恒定大压差会使流体无界加速、"
                        "时间步坍缩,再长的时限也算不完——建议入口改用 totalPressure(总压)边界;"
                        "若压力为绝压且压差大,应改用可压缩求解器。")
                    break

        # ② snappy 细化超预算(#616:一轮细化 57万→275万,冲破 180万上限;上限只在轮前检查)
        sn_log = case / "log.snappyHexMesh"
        if sn_log.is_file():
            txt = sn_log.read_text(errors="ignore")
            cells = [int(x) for x in re.findall(r"cells:(\d+)", txt)]
            mgc = _dict_get(case / "system" / "snappyHexMeshDict", "maxGlobalCells")
            mgc = int(mgc) if mgc and mgc.isdigit() else None
            if cells and mgc and max(cells) > mgc:
                hints.append(
                    f"网格细化已膨胀到 {max(cells):,} 单元,超出 maxGlobalCells={mgc:,}"
                    "(该上限只在每轮细化前检查,单轮内拦不住)。单核运行下这通常直接导致内存"
                    "耗尽或超时——建议把 refinement 级别整体降 1 级,先粗网格验证再逐步加密。")

        # ③ FSI/接触需求 vs 纯流体求解器(#614:自造 fsiProperties 不被任何求解器读取)
        fsi_dicts = [n for n in ("fsiProperties", "contactProperties")
                     if (case / "constant" / n).is_file()]
        dyn = case / "constant" / "dynamicMeshDict"
        if dyn.is_file() and re.search(r"fsi|FSI", dyn.read_text(errors="ignore")):
            fsi_dicts.append("dynamicMeshDict(fsi 段)")
        if fsi_dicts and app in _PURE_FLUID:
            hints.append(
                f"算例包含流固耦合/接触相关配置({', '.join(fsi_dicts)}),但 {app} 是纯流体"
                "求解器,不会读取这些文件——即使跑完也不是真正的 FSI。双向流固耦合需要专用"
                "耦合求解器(如 solids4Foam),当前平台暂不支持,建议改为刚性边界近似或联系我们。")

        # ④ setFields 前置文件缺失(#616:0/ 只有 alpha.water.orig,Allrun 又没有拷贝步)
        allrun = case / "Allrun"
        if allrun.is_file() and "setFields" in allrun.read_text(errors="ignore"):
            zero = case / "0"
            if zero.is_dir():
                for f in zero.glob("*.orig"):
                    plain = zero / f.name[:-5]
                    if not plain.exists() and not re.search(
                            rf"(cp|mv)\s+\S*{re.escape(f.name)}", allrun.read_text(errors="ignore")):
                        hints.append(
                            f"0/ 目录只有 {f.name} 而没有 {plain.name},且 Allrun 中没有拷贝步骤"
                            "——setFields 运行时会因缺少该场文件而失败,请在 setFields 前补 "
                            f"`cp 0/{f.name} 0/{plain.name}`。")
                        break
    except Exception as e:                       # lint 绝不拖垮失败处理主流程
        logger.warning(f"case_lint 异常(忽略): {e}")
    return hints
