"""环境层：把仓库准备好，并且【只用公开信息】决定测试怎么跑。

【这个文件是本次改造的第一优先级】
上一版的测试范围是这么来的：

    paths = re.findall(r"^diff --git a/(\\S+)", inst["test_patch"], re.M)   # ← 泄露

从 test_patch 里抠出测试文件路径，再把 agent 的测试命令 scope 到那些文件。
省时间，但它把"官方隐藏测试藏在哪个文件"直接告诉了 agent。这条捷径一旦存在，
所有 resolved 数字都要打折 —— 因为没人能证明 agent 不是靠它定位的。

现在测试范围只来自两个公开来源：
  1) RepoPilot 自己的 adapter 探测（它看的是 pyproject.toml 这类公开文件）
  2) 仓库根目录下实际存在的测试目录（tests/ testing/ test/ —— ls 一下就知道）
这两样在 agent clone 完仓库之后自己也能看到，所以它们不构成任何额外信息。

【缓存策略：什么能跨版本复用，什么绝对不能】
可以缓存（属于环境，与 agent 无关）：
    仓库克隆、base commit、依赖安装层、adapter 探测结果、基线测试结果
绝对不能跨 agent 版本复用（属于 agent 的产出）：
    agent 的代码分析结论、Explorer 答案、失败后的模型上下文、上一次的补丁
所以每次推理【开始前】都会把工作区强制复位到 pristine（reset），
venv 和克隆则原样复用。两个 agent 版本共享的只有环境，不共享任何思考。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .instances import PublicInstance

EVAL_DIR = Path(__file__).resolve().parents[1]
REPO_CACHE = EVAL_DIR / "cache" / "repos"      # 每个仓库一份完整克隆，所有实例共享
ENVS_DIR = EVAL_DIR / "envs"                   # 每个实例一份 venv + 工作树

# ---------------------------------------------------------------------------
# 依赖版本：第二个"环境伪影"来源，解法和解释器版本同源
#
# 【问题】`pip install -e .` 装的是【今天】的依赖。2023 年的 flask 2.3 配上
# 2025 年的 werkzeug 3.x，第一行就 AttributeError：werkzeug 删掉了 __version__。
# 这跟 agent 会不会修 bug 毫无关系，纯粹是把代码丢进了未来。
#
# 【曾经的解法，以及它为什么不好】上一版维护了一张手工钉版本表：
#     ENV_PINS = {"pallets__flask-4992": ["werkzeug<2.3"], …}
# 它能work，但每一条都是我可以随时调到"这条终于过了"为止的魔法数字。
# 一张能被调的表，就是一个能被调的分子。
#
# 【现在的解法】uv 的 --exclude-newer：只允许安装 base_commit 那天【之前】
# 发布的版本。一条规则覆盖所有实例，依据是提交日期这个公开事实，
# 谁跑都得到同一套依赖。没有可以调的旋钮。
# ---------------------------------------------------------------------------
# 装完包还要单独装 pytest（它通常不是被测包的运行依赖）。
# 版本同样由 --exclude-newer 决定，不再手工写 `pytest<8`。
TEST_EXTRAS = ("tests", "test", "dev", "testing")

TEST_ROOT_CANDIDATES = ("tests", "testing", "test", "spec")

# ---------------------------------------------------------------------------
# 解释器版本：这一条决定了"环境伪影"有多少
#
# 【问题】这批实例是 2013-2023 年的，本机只有 Python 3.12。新版 Python 会对
# 老代码抛出当年不存在的 DeprecationWarning，而很多仓库配了
# `filterwarnings = error` —— 于是一个警告把 58 个测试全变成 ERROR。
# 实测：pallets__flask-5014 连【官方 gold 补丁】都过不了，纯粹是 3.12 的锅。
# 这类实例会被 gold 校准正确地标成"不可判定"，但如果一半实例都这样，
# 这份评测就没什么可判的了。
#
# 【解法】按仓库【自己声明的】python_requires 选解释器。
# 注意这是一条机械规则，不是一张手工维护的版本表：
#   · 它读的是仓库在 base_commit 上的公开元数据（pyproject/setup.cfg/setup.py）
#   · 与实例难不难无关，与我希望哪条通过无关
#   · 谁都能复现同一个选择
# 手工表（"flask 2.3 用 3.11、sphinx 用 3.9…"）也能work，但它是一串我可以
# 随时调到好看为止的魔法数字。机械规则调不动。
#
# 偏好从 3.9 起：再往下装依赖的 wheel 就不全了，得现场编译，反而更脆。
# ---------------------------------------------------------------------------
PREFERRED_PYTHONS = ("3.9", "3.10", "3.11", "3.12")
_REQUIRES = re.compile(r"""(?:requires-python|python_requires)\s*[=:]\s*["']([^"']+)["']""")


def sh(cmd, cwd=None, timeout=900, text=True):
    """跑一条命令，返回 (exit_code, 合并输出)。永不抛异常 —— 错误即信息。"""
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True,
                           text=text, shell=isinstance(cmd, str))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"超时（>{timeout}s）"
    except OSError as e:
        return -1, f"命令无法启动：{e}"


@dataclass
class Env:
    """一个准备好的实例环境。ok=False 时 error 里写着为什么。"""

    instance_id: str
    repo_dir: Path | None = None
    venv_py: str = ""
    python_version: str = ""       # 实际用的解释器版本，进 manifest 和报告
    requires_python: str = ""      # 仓库自己声明的约束，选择依据
    commit_date: str = ""          # base_commit 的日期，依赖时间线的截止点
    install_note: str = ""         # 依赖是怎么装上的（哪个 extras、有没有降级）
    test_cmd: str = ""
    test_flags: str = ""           # 这套 pytest 实际认的参数（探出来的，不是假设的）
    test_roots: list[str] = field(default_factory=list)
    collect_exit: int | None = None   # 闸门：测试收集得起来吗
    baseline_exit: int | None = None  # 度量：全量跑完是什么结果
    # 跑一遍全量测试要多久。这是"去掉 test_patch 泄露"的直接代价：范围从
    # 几个文件变成整个测试目录，seaborn 这类项目一轮就要 8 分钟 —— 而 agent
    # 每轮都要跑一次。这个数直接决定单实例耗时和 --test-timeout 该给多大。
    baseline_secs: float = 0.0
    baseline_timed_out: bool = False
    prepared_at: float = 0.0
    ok: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["repo_dir"] = str(self.repo_dir) if self.repo_dir else None
        return d


def env_dir(instance_id: str) -> Path:
    return ENVS_DIR / instance_id


def _stamp(instance_id: str) -> Path:
    return env_dir(instance_id) / "env.json"


def prepare(inst: PublicInstance, *, force: bool = False, quiet: bool = False,
            baseline_timeout: int = 900) -> Env:
    """克隆 → checkout base_commit → 建 venv 装依赖 → 探测测试命令 → 跑一次基线。

    做过一次就把结果盖章存进 env.json，后续运行直接读 —— 这是最值钱的一层
    缓存（装依赖动辄一两分钟，30 个实例就是一小时）。
    """
    log = (lambda *a: None) if quiet else print
    stamp = _stamp(inst.instance_id)
    if stamp.exists() and not force:
        # 只取当前认识的字段：env.json 会跨代码版本存活，加/删字段不该让
        # 几十分钟的装机成果变成一个 TypeError。
        data = json.loads(stamp.read_text())
        known = set(Env.__dataclass_fields__)
        cached = Env(**{k: v for k, v in data.items() if k in known and k != "repo_dir"})
        cached.repo_dir = env_dir(inst.instance_id) / "repo"
        if cached.ok and cached.repo_dir.exists():
            return cached

    wd = env_dir(inst.instance_id)
    wd.mkdir(parents=True, exist_ok=True)
    env = Env(instance_id=inst.instance_id, prepared_at=time.time())

    # -- 1. 仓库（跨实例共享的那份克隆）--
    cache = REPO_CACHE / inst.repo.replace("/", "__")
    if not cache.exists():
        REPO_CACHE.mkdir(parents=True, exist_ok=True)
        log(f"  克隆 {inst.repo} …")
        code, out = sh(["git", "clone", f"https://github.com/{inst.repo}.git", str(cache)])
        if code != 0:
            return _fail(env, stamp, f"clone 失败：{out[-300:]}")

    repo_dir = wd / "repo"
    if not repo_dir.exists():
        sh(["git", "clone", "-q", str(cache), str(repo_dir)])
        code, out = sh(["git", "checkout", "-q", inst.base_commit], cwd=repo_dir)
        if code != 0:
            # 缓存的克隆可能比 base_commit 旧，拉一次再来
            sh(["git", "fetch", "-q", "origin"], cwd=cache)
            sh(["git", "fetch", "-q", str(cache)], cwd=repo_dir)
            code, out = sh(["git", "checkout", "-q", inst.base_commit], cwd=repo_dir)
            if code != 0:
                return _fail(env, stamp, f"checkout {inst.base_commit} 失败：{out[-300:]}")
    env.repo_dir = repo_dir
    env.commit_date = commit_date(repo_dir, inst.base_commit)

    # -- 2. venv + 依赖 --
    env.requires_python = read_requires_python(repo_dir)
    env.python_version = pick_python(env.requires_python)
    venv_py = wd / "venv" / "bin" / "python"
    if not venv_py.exists():
        log(f"  建 venv（Python {env.python_version}，依据仓库声明的 "
            f"{env.requires_python or '（未声明）'}）+ 装 {env.commit_date[:10]} 之前的依赖…")
        if not make_venv(wd / "venv", env.python_version):
            return _fail(env, stamp, f"无法用 Python {env.python_version} 建 venv")
        sh([str(venv_py), "-m", "pip", "install", "-q", "-U", "pip",
            SETUPTOOLS_PIN, "wheel"], timeout=600)
        ok, note = install_deps(venv_py, repo_dir, env.commit_date)
        if not ok:
            return _fail(env, stamp, f"装依赖失败：{note[-500:]}")
        env.install_note = note
    env.venv_py = str(venv_py)

    # -- 3. 测试命令 + 环境闸门：只用公开信息，只证明"跑得动"不证明"跑得快" --
    #
    # 闸门用 --collect-only，因为它证明的正好是"环境就绪"该证明的那件事：
    # 依赖装上了、import 得进去、测试能被发现。
    # 拿跑完整套件当闸门会把【慢】和【坏】混为一谈 —— seaborn 一轮要 8 分钟，
    # 它不是坏，它只是慢，而慢是一个该被记录的数字，不是一个该被淘汰的理由。
    log("  探测 pytest 参数 + 收集测试（环境闸门）…")
    env.test_roots = discover_test_roots(repo_dir)
    flags, err = probe_flags(str(venv_py), repo_dir, env.test_roots)
    if not flags:
        return _fail(env, stamp, f"测试收集失败（各组参数都不行）：{err[-400:]}")
    env.test_flags = flags
    env.test_cmd = derive_test_command(str(venv_py), env.test_roots, flags)
    env.collect_exit = 0

    # -- 5. 量一下全量套件要多久。这是【度量】不是【闸门】：超时不判死。 --
    # 为什么值得单独量：agent 每一轮都要跑一次测试，这个数直接决定单实例耗时，
    # 也决定 --test-timeout 该给多大。测不出来只说明它很慢，不说明它坏了。
    log("  量基线耗时…")
    t0 = time.time()
    code, out = sh(env.test_cmd, cwd=repo_dir, timeout=baseline_timeout)
    env.baseline_secs = round(time.time() - t0, 1)
    env.baseline_exit = code
    env.baseline_timed_out = code == -1

    env.ok = True
    _save(env, stamp)
    return env


def read_requires_python(repo_dir: Path) -> str:
    """读仓库自己声明的 python_requires / requires-python。读不到就返回空串。

    只读【base_commit 上就有的】公开元数据文件 —— 和 agent 自己能看到的
    是同一批文件，不构成任何额外信息。
    """
    for name in ("pyproject.toml", "setup.cfg", "setup.py"):
        f = repo_dir / name
        if not f.exists():
            continue
        m = _REQUIRES.search(f.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1).strip()
    return ""


def pick_python(requires: str) -> str:
    """在偏好序列里选第一个满足约束的版本。约束读不懂就选最老的那个。

    "读不懂就选最老" 是有意的保守取舍：这批代码是老的，用老解释器出问题的
    概率远小于用新解释器 —— 新解释器带来的是【当年不存在的】警告和移除。
    """
    lo, hi = _parse_bounds(requires)
    for v in PREFERRED_PYTHONS:
        t = _tuple(v)
        if (lo is None or t >= lo) and (hi is None or t <= hi):
            return v
    return PREFERRED_PYTHONS[0]


def _tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".")[:2])


def _parse_bounds(requires: str) -> tuple[tuple | None, tuple | None]:
    """把 `>=3.8,<4` 这种约束解析成 (下界, 上界)。只认 >= / > / <= / < 四种。"""
    lo = hi = None
    for op, ver in re.findall(r"(>=|<=|>|<|==|~=)\s*([0-9.]+)", requires or ""):
        t = _tuple(ver)
        if op in (">=", "==", "~="):
            lo = max(lo or t, t)
        elif op == ">":
            lo = max(lo or (t[0], t[1] + 1), (t[0], t[1] + 1))
        elif op == "<=":
            hi = min(hi or t, t)
        elif op == "<":
            # `<4` 对小版本没有约束（4.0 之前的 3.x 全可以），别把它算成 <3.x
            hi = min(hi or (t[0] - 1, 99), (t[0] - 1, 99)) if len(t) == 1 or t[1] == 0 \
                else min(hi or (t[0], t[1] - 1), (t[0], t[1] - 1))
    return lo, hi


def make_venv(path: Path, version: str) -> bool:
    """建 venv。优先用 uv（它能按需下载任意版本的 CPython，几秒钟一个）。

    没装 uv 就退回当前解释器 —— 那样多半会撞上新版 Python 的弃用警告，
    但**退化要说出来**：env.json 里的 python_version 会如实记下真正用的版本，
    报告据此解释环境伪影率为什么偏高。
    """
    uv = shutil.which("uv") or str(Path(sys.executable).parent / "uv")
    if Path(uv).exists():
        sh([uv, "python", "install", version], timeout=600)
        code, _ = sh([uv, "venv", "--python", version, "--seed", str(path)], timeout=600)
        if code == 0:
            return True
    code, _ = sh([sys.executable, "-m", "venv", str(path)], timeout=300)
    return code == 0


# setuptools 的版本卡在一个窄区间里，两头都是被实测逼出来的：
#   >=64  才有 PEP 660 的 build_editable，低于它可编辑安装直接失败
#   <81   才还带着 pkg_resources；81 把它删了，而 2020 年的 sphinx 在
#         registry.py 里 `from pkg_resources import iter_entry_points`
# 中间这段既构建得动老包，又跑得起老代码。
SETUPTOOLS_PIN = "setuptools>=64,<81"


def commit_date(repo_dir: Path, sha: str) -> str:
    """base_commit 的提交日期（ISO8601）。依赖时间线就截止在这一天。"""
    code, out = sh(["git", "show", "-s", "--format=%cI", sha], cwd=repo_dir, timeout=60)
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else ""


def _uv() -> str | None:
    path = shutil.which("uv") or str(Path(sys.executable).parent / "uv")
    return path if Path(path).exists() else None


def install_deps(venv_py: Path, repo_dir: Path, iso_date: str) -> tuple[bool, str]:
    """装被测包 + pytest，全部限制在 base_commit 那天之前发布的版本。

    先试仓库自己声明的测试 extras（tests/test/dev），都没有就装裸的 `-e .`。
    extras 名字来自仓库的 pyproject/setup.cfg —— 又一个公开事实，不是我编的。

    没装 uv 就退回普通 pip：那样装的是今天的依赖，**大概率制造环境伪影**。
    这条退化路径会写进 install_note，最终出现在报告里，而不是默默发生。
    """
    uv = _uv()
    if uv is None:
        code, out = sh([str(venv_py), "-m", "pip", "install", "-q", "-e", str(repo_dir),
                        "pytest"], timeout=1800)
        return code == 0, "退化：未安装 uv，装的是【今天】的依赖，环境伪影率会偏高\n" + out[-300:]

    # --no-build-isolation：**构建工具链不参与时间旅行，只有依赖参与。**
    #
    # 不加这个参数时，uv 会为构建单独开一个隔离环境，而那个环境里的 setuptools
    # 也受 --exclude-newer 约束 —— 于是 2020 年的实例拿到 2020 年的 setuptools，
    # 它早于 PEP 660，没有 build_editable，可编辑安装直接失败：
    #     AttributeError: module 'setuptools.build_meta' has no attribute 'build_editable'
    # 实测代价：dev 划分 15 条里 11 条卡在这里。
    #
    # 语义上这也是对的：我们要复现的是【这个库当年的依赖环境】，不是
    # 【当年的打包工具】。setuptools 是脚手架，不是被测对象。
    # 关掉隔离后构建用 venv 里的现代 setuptools（建 venv 时就装好了），
    # 而运行期依赖仍然被 --exclude-newer 钉在提交日期之前。
    base = [uv, "pip", "install", "--python", str(venv_py), "-q",
            "--no-build-isolation"]
    dated = base + (["--exclude-newer", iso_date] if iso_date else [])
    notes = []

    # 关掉构建隔离之后，构建依赖【必须由我们自己装好】—— 没人再替我们准备了。
    # 它们来自 pyproject.toml 的 [build-system] requires，是公开元数据。
    # 不装的代价是隐蔽的：pytest 用 setuptools_scm 在构建时生成 _version.py，
    # 缺了它构建仍然"成功"，但一 import 就 `No module named '_pytest._version'`。
    # 这些是脚手架，**不受日期约束** —— 我们要复现的是当年的依赖，不是当年的构建工具。
    build_reqs = read_build_requires(repo_dir)
    if build_reqs:
        code, out = sh([*base, *build_reqs], timeout=900)
        notes.append(f"构建依赖 {' '.join(build_reqs)}"
                     + ("" if code == 0 else f"（装不上：{out[-120:]}）"))

    extras = [e for e in TEST_EXTRAS if _declares_extra(repo_dir, e)]
    targets = [f"{repo_dir}[{e}]" for e in extras] + [str(repo_dir)]
    for target in targets:
        ok, note = _install_dated_or_fall_back(base, dated, ["-e", target], iso_date)
        notes.append(note)
        if ok:
            break
    else:
        return False, "\n".join(notes)

    # 仓库自己的测试依赖清单。pylint 的 tests/primer 需要 GitPython，
    # 它既不在 extras 里也不在运行依赖里，只写在 requirements_test*.txt ——
    # 又一份公开元数据，不读它就会把"少装一个包"记成"测试收集失败"。
    for req in sorted(repo_dir.glob("requirements*test*.txt")) + \
            sorted(repo_dir.glob("test-requirements*.txt")):
        code, out = sh([*dated, "-r", str(req)], timeout=900)
        notes.append(f"{req.name}" + ("" if code == 0 else "（部分装不上，已忽略）"))

    # pytest 单独装：它通常不是被测包的运行依赖，但判分协议依赖它。
    # **pytest 自己的实例除外** —— 在那里 pytest 就是被测对象，再装一个会把
    # 可编辑安装覆盖掉，然后测试跑的是 PyPI 上的 pytest，不是仓库里那份。
    if not _is_self_pytest(repo_dir):
        code, out = sh([*dated, "pytest"], timeout=900)
        if code != 0:
            notes.append(f"pytest 装不上：{out[-200:]}")
    else:
        notes.append("跳过安装 pytest：被测对象就是 pytest 本身")

    # 【最后一步：把 setuptools 也送回当年】
    # 构建阶段需要现代 setuptools（PEP 660），但**运行阶段不需要**，而现代
    # setuptools 会带来当年不存在的行为：80.x 一 import pkg_resources 就抛
    #     UserWarning: pkg_resources is deprecated as an API
    # 而 xarray / sphinx 这类仓库配了 `filterwarnings = error` —— 一个警告
    # 变成 error，整个收集阶段崩掉。这和被测代码毫无关系。
    # 所以顺序是：现代 setuptools 构建 → 装完之后降回提交日期那天的版本。
    # 脚手架用完就撤走，留下的才是当年的环境。
    if iso_date:
        code, _ = sh([*dated, "--reinstall-package", "setuptools", "setuptools"],
                     timeout=600)
        notes.append("setuptools 已降回提交日期版本" if code == 0
                     else "setuptools 未能降回当年版本（可能触发弃用警告）")
    return True, "\n".join(notes)


def _install_dated_or_fall_back(base: list[str], dated: list[str], args: list[str],
                                iso_date: str) -> tuple[bool, str]:
    """先按提交日期装；日期约束让解析无解时，退回不限日期【并如实记下来】。

    为什么要退路：PyPI 上的上传时间并不总和发布时间一致（撤回后重传、
    补传旧版本都会把时间戳推后）。实测 pytest-5631 就卡在这里：
        `atomicwrites` was filtered by `exclude-newer`
    时间旅行是为了降低环境伪影，不是目的本身 —— 为了它把实例判死，
    是拿手段去伤目的。

    但退回【必须留痕】：install_note 会写进 env.json，再进报告。
    一个悄悄用了今天依赖的实例，和一个用了当年依赖的实例，不是同一个测量。
    """
    code, out = sh([*dated, *args], timeout=1800)
    if code == 0:
        return True, f"已安装 {args[-1]}"
    if not iso_date:
        return False, f"{args[-1]} 装不上：{_last_line(out)}"

    code2, out2 = sh([*base, *args], timeout=1800)
    if code2 == 0:
        return True, (f"已安装 {args[-1]}【但退回了不限日期的依赖】—— "
                      f"日期约束下无解（{_last_line(out)}），本实例的依赖不是当年的版本")
    return False, f"{args[-1]} 装不上：{_last_line(out2)}"


def _last_line(out: str) -> str:
    return out.strip().splitlines()[-1][:140] if out.strip() else ""


def _is_self_pytest(repo_dir: Path) -> bool:
    return (repo_dir / "src" / "_pytest").is_dir() or (repo_dir / "_pytest").is_dir()


def read_build_requires(repo_dir: Path) -> list[str]:
    """读 pyproject.toml 的 [build-system] requires。读不到就返回空。"""
    f = repo_dir / "pyproject.toml"
    if not f.exists():
        return []
    text = f.read_text(encoding="utf-8", errors="replace")
    try:
        import tomllib
        reqs = tomllib.loads(text).get("build-system", {}).get("requires", [])
    except Exception:
        # 老仓库的 pyproject 可能不是合法 TOML（或本机 Python < 3.11），退回正则
        m = re.search(r"\[build-system\](.*?)(?:\n\[|\Z)", text, re.S)
        reqs = re.findall(r'["\']([^"\']+)["\']', m.group(1)) if m else []
    # setuptools / wheel 已经按 SETUPTOOLS_PIN 装好了，别让仓库的声明把它顶掉。
    # 【必须按完整包名比对，不能用前缀匹配】—— 实测踩过：
    # `re.match(r"^(setuptools|wheel)\b", "setuptools-scm[toml]>=3.4")` 是**匹配的**
    # （`-` 是非单词字符，`\b` 在那里成立），于是 setuptools-scm 被我自己的过滤器
    # 剔掉了。而 pytest 正是靠它在构建时生成 _version.py —— 缺了这个文件，
    # 构建照样"成功"，一 import 就 `No module named '_pytest._version'`，
    # 三条 pytest 实例全挂在这里。
    return [r for r in reqs if _dist_name(r) not in ("setuptools", "wheel")]


def _dist_name(requirement: str) -> str:
    """从 `setuptools-scm[toml]>=3.4` 里取出 `setuptools-scm`。"""
    return re.split(r"[\[<>=!~;\s]", requirement.strip(), 1)[0].lower()


def _declares_extra(repo_dir: Path, name: str) -> bool:
    """仓库有没有声明这个 extras。

    三种写法都要认，因为它们在真实仓库里都出现：
        setup.cfg   `testing =`           （[options.extras_require] 下）
        pyproject   `testing = [`
        setup.py    `"testing": [`        ← 字典字面量，用冒号不是等号

    漏掉第三种的代价实测是：pytest-7236 装成了裸的 `-e .`，测试依赖
    hypothesis 没进来，收集阶段 ModuleNotFoundError —— 看起来像环境坏了，
    其实是我少读了一种语法。
    """
    n = re.escape(name)
    pattern = rf"^\s*\[?{n}\]?\s*=|\b{n}\s*=\s*\[|[\"']{n}[\"']\s*:\s*\["
    for f in ("pyproject.toml", "setup.cfg", "setup.py"):
        p = repo_dir / f
        if p.exists() and re.search(pattern,
                                    p.read_text(encoding="utf-8", errors="replace"), re.M):
            return True
    return False


def discover_test_roots(repo_dir: Path) -> list[str]:
    """仓库自己的测试目录 —— `ls` 一下就知道的公开事实，不含任何实例信息。"""
    roots = [d for d in TEST_ROOT_CANDIDATES if (repo_dir / d).is_dir()]
    if roots:
        return roots
    # 没有顶层测试目录的项目（测试和源码放一起），退回全量
    return []


# pytest 的参数不是万古不变的。`--color=no` 是后来才有的，2013 年的
# pytest 2.4.2 见了它直接报 "unrecognized arguments" —— 于是整条实例挂在
# 一个和被测代码毫无关系的地方。按【由全到简】的顺序探一遍，用第一个能跑的。
# 探测本身也是机械的、公开的：只跑 --collect-only，不看任何答案。
FLAG_SETS = (
    "-q -ra --color=no -p no:randomly",   # 现代 pytest：省 token、无颜色码、禁随机顺序
    "-q -ra --color=no",                  # 没装 pytest-randomly
    "-q -ra",                             # 老 pytest 不认 --color
    "-q",                                 # 更老：连 -ra 都没有
)


def derive_test_command(venv_py: str, test_roots: list[str],
                        flags: str = FLAG_SETS[0]) -> str:
    """拼出测试命令。**这个函数的参数里没有 test_patch，也永远不该有。**

    scope 到仓库自己的测试目录是性能取舍（笔记本跑不动某些仓库的全量套件），
    不是信息取舍：agent clone 完仓库自己 ls 一下也能看到同样的目录。
    """
    base = f"{venv_py} -m pytest {flags}"
    return f"{base} {' '.join(test_roots)}" if test_roots else base


def probe_flags(venv_py: str, repo_dir: Path, test_roots: list[str]) -> tuple[str, str]:
    """探出这套 pytest 认哪组参数。返回 (可用参数, 最后一次错误输出)。

    区分两种失败很重要：**参数不被认识**是我们自己造成的，**测试根本跑不起来**
    才是真的环境问题。不探测的话两者都表现为"收集失败"，会把前者记成后者。
    """
    last = ""
    for flags in FLAG_SETS:
        cmd = derive_test_command(venv_py, test_roots, flags) + " --collect-only"
        code, out = sh(cmd, cwd=repo_dir, timeout=600)
        if code in (0, 5) or _collected_a_lot(out):
            return flags, ""
        last = out
    return "", last


# `1989 tests collected, 2 errors` —— 收集到了绝大多数，只有几个文件炸了
_COLLECTED = re.compile(r"(\d+)\s+tests?\s+collected", re.I)


def _collected_a_lot(output: str) -> bool:
    """部分收集失败【不该】判死整条实例。

    实测：pylint-6903 的 `tests/primer/` 需要 GitPython，收集时报 2 个 ERROR，
    但同时 **1989 个测试收集成功**。判分只会跑 F2P/P2P 指定的那几个文件，
    大概率就在这 1989 个里 —— 因为两个无关文件 import 失败就把整条实例扔掉，
    等于用一个比判分更严的标准去筛实例，白白缩小可用样本。

    这不会放松判分：judge 阶段仍然要求 F2P 全绿、P2P 不退步。这里放松的只是
    "这个环境值不值得跑一次 agent"。
    """
    m = _COLLECTED.search(output)
    return bool(m) and int(m.group(1)) >= 50


def reset(env: Env) -> None:
    """把工作树强制复位到 pristine。

    每次推理【之前】都要跑一次。它保证的是：上一个 agent 版本留下的补丁、
    判分阶段打上的官方 test_patch、上一轮失败的半成品，一样都不会带进下一次。
    共享环境的前提就是这一步 —— 少了它，缓存就变成了污染。
    """
    if env.repo_dir is None:
        return
    sh(["git", "checkout", "-q", "--", "."], cwd=env.repo_dir)
    sh(["git", "clean", "-fdq"], cwd=env.repo_dir)


def capture_patch(env: Env) -> str:
    """取 agent 的补丁。含未跟踪的新文件 —— 新增源文件也是修复的一部分。"""
    if env.repo_dir is None:
        return ""
    # 先把未跟踪文件加进索引（不提交），这样 diff 才看得见它们
    sh(["git", "add", "-A", "-N"], cwd=env.repo_dir)
    _, diff = sh(["git", "diff"], cwd=env.repo_dir)
    return diff


def _fail(env: Env, stamp: Path, msg: str) -> Env:
    env.ok, env.error = False, msg
    _save(env, stamp)
    return env


def _save(env: Env, stamp: Path) -> None:
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps(env.to_dict(), indent=2, ensure_ascii=False),
                     encoding="utf-8")


# ---------------------------------------------------------------------------
# 自查：证明测试命令里没有夹带答案
# ---------------------------------------------------------------------------
_SUSPICIOUS = re.compile(r"::|FAIL_TO_PASS|PASS_TO_PASS|test_patch", re.I)


def assert_command_public(cmd: str) -> None:
    """测试命令里出现 `::`（pytest 的用例级选择）就说明它是从用例清单来的。

    仓库级/目录级路径是公开信息；用例级 id 不是 —— 它只可能来自 F2P/P2P。
    这是一条很便宜、但能挡住整类回退的断言。
    """
    if _SUSPICIOUS.search(cmd):
        from .instances import LeakError
        raise LeakError(f"测试命令里出现了用例级选择或判分字段：{cmd!r}。"
                        "测试范围只能来自公开信息（adapter 探测 + 仓库目录结构）。")
