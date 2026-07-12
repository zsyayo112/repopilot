"""执行轨迹：agent 的黑匣子。每一步都落盘成 JSONL。

为什么它是核心模块而不是锦上添花：
  - debug：agent 跑偏时，翻 run.jsonl 就能看到它在哪一步开始走错
  - 评测：成功率 / token 成本 / 工具调用次数，全部从这些文件统计出来
    （Phase 3 的评测脚本，本质就是对 runs/ 目录做 groupby）
"""

import json
import time
from pathlib import Path


class Trace:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "run.jsonl"
        self._seq = 0

    def event(self, kind: str, **data) -> None:
        """追加一条事件。永不抛异常 —— 记录失败不该弄死主流程。"""
        self._seq += 1
        rec = {"seq": self._seq, "ts": round(time.time(), 3), "kind": kind, **data}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def save(self, name: str, text: str) -> None:
        """大块产物（plan.json / final.diff / review.json）单独存文件，好翻阅。"""
        (self.dir / name).write_text(text, encoding="utf-8")
