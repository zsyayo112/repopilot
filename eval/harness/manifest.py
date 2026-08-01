"""运行清单（Manifest）：让"为什么两次结果不同"变成一个可以查的问题。

每次评测生成一份不可变的 Manifest，落在 runs/<run_id>/manifest.json。
它要能回答的问题是固定的那几个：

    当时用的是哪个 agent 版本？    → agent_commit（脏工作区会标 -dirty）
    提示词变过吗？                → prompt_hash
    工具变过吗？                  → tool_schema_hash
    模型变过吗？                  → budget.model
    成本预算一样吗？              → budget_fingerprint
    跑的是哪批实例？              → split + instance_checksum
    判分逻辑变过吗？              → harness_version

【为什么哈希提示词和工具 schema，而不是记一句"改了提示词"】
因为人不会记得。提示词是最容易随手动一句的东西，也是最影响结果的东西之一。
哈希是自动的：两次运行的 prompt_hash 不同，就是不同的实验，句号。

【不可变的含义】
manifest 在实例开跑【之前】写，写完就不再动。跑完的结果写进另一个文件。
一份跑到一半被追加修改过的 manifest，等于没有 manifest。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

HARNESS_VERSION = "0.2.0"       # 判分协议或线束语义变了就升它

ROOT = Path(__file__).resolve().parents[2]      # repopilot 项目根


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def agent_commit() -> str:
    """当前 agent 代码的版本。工作区脏就加 -dirty —— 那意味着这次结果不可复现。"""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=15).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, timeout=15).stdout.strip()
        return f"{sha}{'-dirty' if dirty else ''}" or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def prompt_hash() -> str:
    """把所有会进模型的提示词拼起来哈希。

    列在这里的每一段都必须是【真的会发给模型】的文本。漏掉一段，
    那段就成了"改了但 manifest 说没改"的暗门。
    """
    from repopilot import executor, explorer, planner, reviewer

    blob = "\n<<>>\n".join([
        executor.EXECUTOR_SYSTEM,
        executor.RUNTIME_SYSTEM,
        planner.PLANNER_SYSTEM,
        reviewer.REVIEWER_SYSTEM,
        explorer.EXPLORER_SYSTEM,
    ])
    return _sha(blob)


def tool_schema_hash(groups: tuple[str, ...] = ("core",)) -> str:
    """工具定义的哈希。工具描述里的一句话就能改变模型的行为，它也算提示词。"""
    from repopilot.tools import TOOL_GROUPS, TOOLS

    names = {n for g in groups for n in TOOL_GROUPS.get(g, ())}
    specs = [t for t in TOOLS if t["function"]["name"] in names]
    return _sha(json.dumps(specs, sort_keys=True, ensure_ascii=False))


def build(*, run_id: str, split: str, instance_ids: list[str], budget,
          variant: str, dataset: dict, notes: str = "",
          groups: tuple[str, ...] = ("core",)) -> dict:
    """组装一份 manifest。timestamp 由调用方决定何时冻结（就是现在）。"""
    from .dataset import checksum

    return {
        "run_id": run_id,
        "harness_version": HARNESS_VERSION,
        "agent_commit": agent_commit(),
        "variant": variant,                 # full / no-reviewer / baseline-a / …
        "split": split,
        "dataset": dataset,
        "instance_count": len(instance_ids),
        "instance_checksum": checksum(instance_ids),
        "instance_ids": sorted(instance_ids),
        "model": budget.model,
        "temperature": budget.temperature,
        "budget": budget.to_dict(),
        "budget_fingerprint": budget.fingerprint(),
        "prompt_hash": prompt_hash(),
        "tool_schema_hash": tool_schema_hash(groups),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notes": notes,
    }


def write(run_dir: Path, manifest: dict) -> Path:
    """写死。已经存在就【拒绝覆盖】—— 不可变不是形容词，是行为。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old.get("instance_checksum") != manifest.get("instance_checksum") \
                or old.get("budget_fingerprint") != manifest.get("budget_fingerprint"):
            raise RuntimeError(
                f"{path} 已存在且内容不同。同一个 run_id 不能跑两套不同的配置 —— "
                "换个 run_id，否则事后没人分得清哪些结果属于哪次实验。")
        return path
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load(run_dir: Path) -> dict:
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        raise RuntimeError(f"{run_dir} 没有 manifest.json —— 这批结果无法归因，视为无效。")
    return json.loads(path.read_text())


def comparable(a: dict, b: dict) -> tuple[bool, list[str]]:
    """两次运行能不能直接比 resolved 率？返回 (能不能, 哪里不同)。

    消融实验里 budget 本来就该有一项不同（那正是被消融的那一项），
    所以 budget_fingerprint 不同【不算】阻断项，只列出来提示。
    真正的阻断项是那些"不该变却变了"的：实例清单、模型、判分协议。
    """
    blockers = []
    for key, label in (("instance_checksum", "实例清单"),
                       ("model", "模型"),
                       ("harness_version", "判分线束版本")):
        if a.get(key) != b.get(key):
            blockers.append(f"{label}不同（{a.get(key)} vs {b.get(key)}）")
    notes = [f"{label}不同" for key, label in (("prompt_hash", "提示词"),
                                              ("tool_schema_hash", "工具定义"),
                                              ("agent_commit", "agent 版本"),
                                              ("budget_fingerprint", "预算"))
             if a.get(key) != b.get(key)]
    return not blockers, blockers + notes
