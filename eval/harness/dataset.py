"""数据集与冻结划分：SWE-bench Verified + dev / test / holdout。

【为什么换 Verified，不再自己从 Lite 里挑】
上一版是"从 Lite 里挑看起来能跑的仓库"。这句话里有两个问题：
  1) "看起来能跑" = 按我的环境挑，挑出来的样本对我的环境有利；
  2) 挑选发生在【看过实例之后】，这在统计上就是选择偏差。
SWE-bench Verified 是官方请工程师逐条人工确认"这题是可解的、描述是充分的"
之后留下的 500 条。从它里面固定抽样，挑选这一步就不再由我做。

【为什么必须划 dev / test / holdout】
在同一批 8 个实例上反复改提示词和 agent 逻辑，最终一定会对这 8 条过拟合 ——
不是模型过拟合，是**我过拟合**：我读了它们的失败日志，然后按日志改代码。

    看过某个实例的 issue、失败日志或 agent 轨迹之后，
    它就不再是严格意义上的测试样本，只能算开发样本。

所以划分必须【在看任何东西之前】冻结，并且提交进 git：
  dev      15 条  随便调，随便看轨迹
  test     30 条  只在阶段性版本上跑，跑完看聚合数字，不逐条读轨迹
  holdout  10 条  从未看过，留给最终对外的那个数字

【冻结的含义是分母不许动】
跑完发现某几条特别难，把它们从分母里删掉 —— 这是评测里最常见也最致命的
自欺。清单一旦写进 splits/*.json 就不许改；环境跑不起来的实例照样留在分母里，
只是在报告里单列成"环境失败"那一行（见 report.py）。
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

from .instances import PublicInstance, SecretInstance

HARNESS_DIR = Path(__file__).resolve().parent
EVAL_DIR = HARNESS_DIR.parent
SPLITS_DIR = EVAL_DIR / "splits"
CACHE_DIR = EVAL_DIR / "cache"

DATASET = "princeton-nlp/SWE-bench_Verified"
PARQUET_URL = (f"https://huggingface.co/datasets/{DATASET}"
               "/resolve/main/data/test-00000-of-00001.parquet")
PARQUET_FILE = CACHE_DIR / "swebench_verified_test.parquet"

# 抽样种子。写死在代码里而不是当命令行参数 —— 参数可以反复试到一个好看的
# 划分为止，那等于换了个方式挑实例。
SPLIT_SEED = "repopilot-verified-v1"
SPLIT_SIZES = {"dev": 15, "test": 30, "holdout": 10}

# ---------------------------------------------------------------------------
# 抽样总体（sampling frame）。**这是一条事前声明的限制，不是事后筛选。**
#
# 两者的区别是整个评测可信度的分界线：
#   事前声明：先按一条与实例无关的机械规则划定总体，再从中随机抽 —— 合法。
#   事后筛选：抽完、跑完，发现哪几条跑不动就删掉 —— 这是评测里最常见的自欺。
#
# 规则（三条全都与"这道题难不难"无关，只与"我这台笔记本装不装得上"有关）：
#   1) 纯 Python，pip 安装不需要编译 C / C++ / Cython 扩展
#   2) 测试用 pytest（判分协议依赖 pytest 的 -rA 摘要格式）
#   3) 仓库根目录有独立的测试目录（否则测试范围只能是全量，笔记本跑不完）
#
# 按这三条被排除的仓库，以及被排除的具体原因：
#   scikit-learn / matplotlib / astropy  违反 1：要从源码编译扩展
#   sympy                                违反 3：测试散在 sympy/*/tests/，无顶层测试目录
#   django                               违反 2：自带 runtests.py，不是 pytest 协议
# 排除是【按仓库】做的，仓库内的实例一条都不挑 —— 挑到实例粒度就又变成筛选了。
#
# 想扩大总体，正确的做法是把环境层换成官方 Docker Harness（见 README 路线图），
# 而不是放宽这里的规则。
# ---------------------------------------------------------------------------
POOL_REPOS = ("sphinx-doc/sphinx", "pydata/xarray", "pytest-dev/pytest",
              "pylint-dev/pylint", "psf/requests", "mwaskom/seaborn",
              "pallets/flask")
POOL_RULE = ("纯 Python + pip 免编译 + pytest + 有顶层测试目录；"
             "按仓库整体纳入或整体排除，事前声明，与实例难度无关")
EXCLUDED_REPOS = {
    "scikit-learn/scikit-learn": "需从源码编译 Cython 扩展",
    "matplotlib/matplotlib": "需编译 C++ 扩展",
    "astropy/astropy": "需编译 C 扩展",
    "sympy/sympy": "测试散在 sympy/*/tests/，无顶层测试目录",
    "django/django": "自带 runtests.py，不走 pytest 协议",
}


def _download() -> Path:
    if not PARQUET_FILE.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"下载 {DATASET} …")
        req = urllib.request.Request(PARQUET_URL,
                                     headers={"User-Agent": "repopilot-eval/0.2"})
        with urllib.request.urlopen(req, timeout=300) as r:
            PARQUET_FILE.write_bytes(r.read())
    return PARQUET_FILE


_rows_cache: dict[str, dict] | None = None


def _rows() -> dict[str, dict]:
    """读全量数据集，返回 instance_id → 原始行。**只有本模块可以碰原始行。**"""
    global _rows_cache
    if _rows_cache is None:
        import pyarrow.parquet as pq
        _rows_cache = {r["instance_id"]: r
                       for r in pq.read_table(_download()).to_pylist()}
    return _rows_cache


# ---------------------------------------------------------------------------
# 冻结划分
# ---------------------------------------------------------------------------
def _rank(instance_id: str) -> str:
    """确定性打乱：拿 (种子, id) 的哈希当排序键。

    比 random.shuffle(seed=…) 好在哪：它和数据集的行顺序无关。数据集哪天
    重新排了序，random 那套抽出来的就是另一批实例，而这套抽出来的一模一样。
    """
    return hashlib.sha256(f"{SPLIT_SEED}:{instance_id}".encode()).hexdigest()


def freeze_splits(force: bool = False) -> dict[str, list[str]]:
    """生成三份冻结清单并写进 eval/splits/（提交进 git）。

    按仓库分层轮转再取，避免某个划分被单一仓库垄断 —— 30 条里 25 条是 django
    的话，得到的其实是"在 django 上的成功率"。
    """
    SPLITS_DIR.mkdir(parents=True, exist_ok=True)
    existing = {name: p for name in SPLIT_SIZES
                if (p := SPLITS_DIR / f"verified_{name}.json").exists()}
    if existing and not force:
        raise RuntimeError(
            f"划分已经冻结过了：{sorted(existing)}。重新抽样会让之前所有数字失去意义。"
            "确实要重来请显式加 --force，并且把旧结果一起作废。")

    rows = _rows()
    by_repo: dict[str, list[str]] = {}
    for iid, row in rows.items():
        if row["repo"] not in POOL_REPOS:      # 事前声明的总体，见文件上方
            continue
        by_repo.setdefault(row["repo"], []).append(iid)
    for iid_list in by_repo.values():
        iid_list.sort(key=_rank)

    pool_size = sum(len(v) for v in by_repo.values())
    need = sum(SPLIT_SIZES.values())
    if pool_size < need:
        raise RuntimeError(f"总体只有 {pool_size} 条，抽不满 {need} 条。"
                           "要么缩小 SPLIT_SIZES，要么换成官方 Docker Harness 扩大总体。")

    # 分层轮转：每个仓库轮流出一条，直到取够
    order: list[str] = []
    repos = sorted(by_repo, key=lambda r: _rank(r))
    depth = 0
    while len(order) < sum(SPLIT_SIZES.values()):
        added = False
        for repo in repos:
            if depth < len(by_repo[repo]):
                order.append(by_repo[repo][depth])
                added = True
        if not added:
            break
        depth += 1

    out, cursor = {}, 0
    for name in ("dev", "test", "holdout"):
        n = SPLIT_SIZES[name]
        ids = sorted(order[cursor:cursor + n])
        cursor += n
        out[name] = ids
        payload = {
            "split": name,
            "dataset": DATASET,
            "seed": SPLIT_SEED,
            "size": len(ids),
            "instance_ids": ids,
            "checksum": checksum(ids),
            # 总体规则跟着清单一起冻结。抽样限制必须和抽样结果存在同一个文件里，
            # 否则读到这份清单的人无从判断"这 30 条代表什么"。
            "sampling_frame": {
                "rule": POOL_RULE,
                "included_repos": list(POOL_REPOS),
                "excluded_repos": EXCLUDED_REPOS,
                "pool_size": pool_size,
                "declared": "在抽样之前声明，按仓库整体纳入/排除，与实例难度无关",
            },
            "rule": ("冻结清单。分母只由这份清单决定：跑不起来的实例留在分母里，"
                     "在报告中单列为环境失败，【不许】从清单里删除。"),
        }
        (SPLITS_DIR / f"verified_{name}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
    return out


def load_split(name: str) -> list[str]:
    """读一份冻结清单，并当场校验它没被人偷偷改过。"""
    path = SPLITS_DIR / f"verified_{name}.json"
    if not path.exists():
        raise RuntimeError(f"没有 {name} 划分。先跑：python eval/run.py freeze")
    data = json.loads(path.read_text())
    ids = data["instance_ids"]
    if data.get("checksum") != checksum(ids):
        raise RuntimeError(
            f"{path} 的校验和对不上 —— 清单被改过。这会让此前所有基于它的数字失效。"
            "要么恢复原文件，要么显式作废旧结果重新冻结。")
    return ids


def checksum(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 两个取数入口：公开的 / 保密的。谁 import 哪一个，一目了然。
# ---------------------------------------------------------------------------
def load_public(instance_ids: list[str]) -> list[PublicInstance]:
    """推理侧的入口。返回值里没有任何答案字段。"""
    rows = _rows()
    missing = [i for i in instance_ids if i not in rows]
    if missing:
        raise RuntimeError(f"数据集里找不到这些实例：{missing[:5]}")
    return [PublicInstance.from_row(rows[i]) for i in instance_ids]


def load_secret(instance_ids: list[str]) -> dict[str, SecretInstance]:
    """判分侧的入口。**只应被 grader.py / audit 调用。**"""
    rows = _rows()
    return {i: SecretInstance.from_row(rows[i]) for i in instance_ids if i in rows}


def dataset_info() -> dict:
    """进 manifest 用：跑的到底是哪一版数据集。"""
    blob = PARQUET_FILE.read_bytes() if PARQUET_FILE.exists() else b""
    return {"name": DATASET,
            "parquet_sha256": hashlib.sha256(blob).hexdigest()[:16] if blob else None,
            "rows": len(_rows()) if blob else 0}
