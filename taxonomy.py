"""Prompt taxonomy — derived, non-identifying metrics for a simulation request.

Shared by worker.py (writes them into simulation_archive before a row is purged)
and stats.py (reports them). Deliberately derives only *shape* from a prompt —
domain tags, length, language — so the archive can outlive the TTL without
retaining user text. See docs/active/usage-metrics.md.

The regexes were validated against the 76 live rows on 2026-09-22; note
'(?<!in)compressible', which stops "incompressible" from being read as a
compressible-flow case (that bug alone mislabelled 70% of the sample).
"""

import re

# (tag, pattern). A prompt may carry several tags; order is display order.
DOMAIN_PATTERNS = [
    ('multiphase',   r'interfoam|\bvof\b|两相|多相|multiphase|two-?phase|free surface|液膜|liquid film|droplet|液滴|气泡|bubble|alpha\.water|自由液面|溃坝|dam ?break'),
    ('heat',         r'heat transfer|thermal|温度|传热|换热|buoyant|natural convection|conjugate|导热|温差|heat flux'),
    ('turbulence',   r'turbulen|湍流|\brans\b|\bles\b|k-?epsilon|k-?omega|\bsst\b|spalart'),
    ('rotating',     r'rotat|旋转|\bmrf\b|\bsrf\b|impeller|叶轮|pump\b|泵|fan\b|风机|turbine|涡轮|离心|centrifug'),
    ('aerodynamics', r'airfoil|翼型|aerodynam|气动|\bdrag\b|\blift\b|升力|阻力|cylinder|圆柱绕流|bluff|wing|机翼|汽车|vehicle'),
    ('internal',     r'\bpipe\b|管道|\bchannel\b|管路|\bduct\b|管内|poiseuille|elbow|弯管|喷嘴|nozzle|固定床|packed bed'),
    ('porous',       r'porous|多孔|darcy|填料|catalyst|催化'),
    ('combustion',   r'combust|燃烧|reacting|flame|火焰|反应流'),
    ('particles',    r'\bparticle|颗粒|lagrangian|\bdpm\b|喷雾|spray'),
    ('benchmark',    r'cavity|lid.?driven|方腔|顶盖驱动|benchmark'),
    ('compressible', r'(?<!in)compressible|可压缩|supersonic|超音速|shock wave|激波|\bmach\b|跨音速'),
    ('fsi',          r'\bfsi\b|流固耦合|fluid.?structure'),
    ('marine',       r'\bwave\b|波浪|船舶|\bship\b|marine|海洋|sloshing|晃荡'),
    ('microfluidic', r'microfluidic|微流|micro.?channel|毛细|capillary'),
    ('nonnewtonian', r'non-?newtonian|非牛顿|viscoelastic|粘弹|cross model|carreau|rheolog|剪切变稀'),
]

# Human-readable labels for reports (English tag -> bilingual label).
DOMAIN_LABELS = {
    'multiphase':   '多相流 / 自由界面 (VOF)',
    'heat':         '传热 / 对流换热',
    'turbulence':   '湍流建模',
    'rotating':     '旋转机械 / 旋转坐标系',
    'aerodynamics': '外流 / 气动',
    'internal':     '内流 / 管道',
    'porous':       '多孔介质',
    'combustion':   '燃烧 / 反应流',
    'particles':    '颗粒 / 离散相',
    'benchmark':    '基准算例 (cavity 等)',
    'compressible': '可压缩 / 高速流',
    'fsi':          '流固耦合 FSI',
    'marine':       '海洋 / 波浪 / 晃荡',
    'microfluidic': '微流控 / 毛细',
    'nonnewtonian': '非牛顿 / 流变',
}

_COMPILED = [(tag, re.compile(pat, re.I)) for tag, pat in DOMAIN_PATTERNS]
_CJK = re.compile(r'[一-鿿]')
# Platform's own test submissions: docker_submit_test_tasks.sh writes "[Test i/n]",
# validation batches use "[V1-cavity r2]" / "[V4-regression]".
_TEST_PROMPT = re.compile(r'^\s*\[(test|v\d|regression)', re.I)


def domain_tags(prompt):
    """Return the CFD application areas a prompt touches (possibly several)."""
    if not prompt:
        return []
    return [tag for tag, rx in _COMPILED if rx.search(prompt)]


def prompt_language(prompt):
    """'zh' if the request contains Chinese characters, else 'en'."""
    if not prompt:
        return None
    return 'zh' if _CJK.search(prompt) else 'en'


def is_platform_test(prompt):
    """True for the platform's own smoke/validation submissions."""
    return bool(prompt and _TEST_PROMPT.match(prompt))


def summarize(prompt):
    """Everything derived from a prompt that the archive keeps."""
    return {
        'prompt_len': len(prompt or ''),
        'prompt_lang': prompt_language(prompt),
        'domain_tags': domain_tags(prompt),
        'is_platform_test': is_platform_test(prompt),
    }
