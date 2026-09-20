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


# 网格/前后处理工具,其 log.* 不代表求解结果(与 worker._OF_UTILITY_LOGS 同义)
_UTILITY_APPS = {
    "blockMesh", "snappyHexMesh", "checkMesh", "setFields", "decomposePar",
    "reconstructPar", "reconstructParMesh", "surfaceFeatures", "surfaceFeatureExtract",
    "topoSet", "createPatch", "extrudeMesh", "transformPoints", "renumberMesh",
    "mapFields", "foamToVTK", "postProcess", "foamDictionary", "foamLog",
    "paraFoam", "moveDynamicMesh", "splitMeshRegions", "refineMesh",
}


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
    """对失败任务的算例做静态体检 → 用户可读提示列表。异常安全:任何错误返回已有结果。

    算例既可能直接躺在 `output/`,也可能是 Foam-Agent 的多算例布局
    `output/cases/<name>/`(如 #713 的参数扫描,9 个算例)。后者过去完全扫不到:
    #713 的 lint_hints 返回空列表,而 `output/cases/*/log.SRFSimpleFoam` 里
    明明写着缺 `0/Urel` 的 FOAM FATAL ERROR。两种布局都要查。
    """
    hints: list[str] = []
    try:
        base = Path(run_dir)
        root = base / "output" if (base / "output").is_dir() else base
        cases = [root]
        if (root / "cases").is_dir():
            cases += sorted(d for d in (root / "cases").iterdir() if d.is_dir())
        for case in cases:
            for h in _lint_case(case):
                if h not in hints:            # 多算例布局下同一问题会重复命中
                    hints.append(h)
    except Exception as e:                       # lint 绝不拖垮失败处理主流程
        logger.warning(f"case_lint 异常(忽略): {e}")
    return hints


def _lint_case(case: Path) -> list[str]:
    """对单个 OpenFOAM 算例目录做体检。"""
    hints: list[str] = []
    try:
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

        # ⑤ 求解器自己报的 FOAM FATAL ERROR(#713:SRFSimpleFoam 缺 0/Urel)
        # Foam-Agent 的 "Allrun executed successfully without errors" 不可信,
        # 求解器日志才是唯一真相;把 OpenFOAM 原话直接摆给用户,比任何猜测都有用。
        for solver_log in sorted(case.glob("log.*")):
            if solver_log.name[4:] in _UTILITY_APPS:
                continue
            txt = solver_log.read_text(errors="ignore")
            m = re.search(r"-->\s*FOAM FATAL (?:IO )?ERROR:?\s*(.{0,200})", txt, re.S)
            if not m:
                continue
            # 去掉 "From function ... in file ... at line N." 的调用栈噪音
            detail = " ".join(m.group(1).split()).split("From function")[0].strip()
            hint = (f"求解器 {solver_log.name[4:]} 直接报错退出(不是算不完,是压根没跑起来):"
                    f"{detail}")
            miss = re.search(r'cannot find file "?[^"]*/0/(\w+)"?', detail)
            if miss and solver_log.name[4:].startswith("SRF"):
                hint += (f"。SRF(单旋转坐标系)系列求解器读的是相对速度场 {miss.group(1)},"
                         "不是 U——请在 0/ 下提供该场,或改用非 SRF 求解器 + MRF 方式。")
            elif miss:
                hint += f"。0/ 目录缺少该求解器必需的场文件 {miss.group(1)},请补齐后重试。"
            hints.append(hint)
            break

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
    except Exception as e:                       # 单个算例出错不影响其余算例
        logger.warning(f"case_lint 单算例异常(忽略) {case}: {e}")
    return hints
