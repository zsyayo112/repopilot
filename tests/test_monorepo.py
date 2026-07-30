"""monorepo 扫描：一个仓库不再假设只有一种技术栈。"""

import json

from repopilot.adapters import detect, scan


def _shop(tmp_path):
    """造一个典型的真实布局：前端 Next、后端 Nest、worker Python、搜索 Go。"""
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    (tmp_path / "package.json").write_text(json.dumps(
        {"name": "shop", "workspaces": ["apps/*"], "scripts": {"test": "turbo test"}}))

    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(json.dumps(
        {"dependencies": {"next": "^14"}, "devDependencies": {"vitest": "^1"},
         "scripts": {"test": "vitest run", "dev": "next dev", "build": "next build"}}))
    (web / "pnpm-lock.yaml").write_text("")

    api = tmp_path / "apps" / "api"
    api.mkdir(parents=True)
    (api / "package.json").write_text(json.dumps(
        {"dependencies": {"@nestjs/core": "^10"}, "devDependencies": {"jest": "^29"},
         "scripts": {"test": "jest", "start:dev": "nest start --watch"}}))

    worker = tmp_path / "apps" / "worker"
    worker.mkdir(parents=True)
    (worker / "pyproject.toml").write_text("[project]\nname='worker'\n")

    search = tmp_path / "apps" / "search"
    search.mkdir(parents=True)
    (search / "go.mod").write_text("module search\n")
    return tmp_path


def test_scan_finds_every_unit(tmp_path):
    profile = scan(_shop(tmp_path))
    kinds = {u.path: u.kind for u in profile.units}
    assert profile.is_monorepo
    assert kinds["apps/web"] == "nextjs"
    assert kinds["apps/api"] == "nestjs"
    assert kinds["apps/worker"] == "python"
    assert kinds["apps/search"] == "go"


def test_workspace_root_does_not_stop_the_scan(tmp_path):
    """根目录的 package.json 认领之后【不能】停止下钻 —— 真项目在 apps/ 里。"""
    profile = scan(_shop(tmp_path))
    assert any(u.path == "." for u in profile.units)
    assert len(profile.units) == 5


def test_unit_for_uses_longest_prefix(tmp_path):
    """一个文件同时落在 "." 和 "apps/web" 名下时，必须选更精确的那个。"""
    profile = scan(_shop(tmp_path))
    assert profile.unit_for("apps/web/components/Button.tsx").path == "apps/web"
    assert profile.unit_for("apps/search/index.go").path == "apps/search"
    assert profile.unit_for("README.md").path == "."


def test_units_for_dedupes_a_batch_of_changes(tmp_path):
    profile = scan(_shop(tmp_path))
    units = profile.units_for(["apps/web/a.tsx", "apps/web/b.tsx", "apps/api/c.ts"])
    assert [u.path for u in units] == ["apps/api", "apps/web"]


def test_node_modules_is_never_scanned(tmp_path):
    """node_modules 里几千个 package.json，扫进去等于自杀。"""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    nested = tmp_path / "node_modules" / "left-pad"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text(json.dumps({"name": "left-pad"}))
    assert [u.path for u in scan(tmp_path).units] == ["."]


def test_single_stack_repo_is_not_a_monorepo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    profile = detect(tmp_path)
    assert profile.repository.is_monorepo is False
    assert profile.kind == "python"


def test_detect_reports_when_primary_is_not_at_root(tmp_path):
    """主项目在子目录时要说出来 —— 否则用户不知道测试是在哪跑的。"""
    sub = tmp_path / "backend"
    sub.mkdir()
    (sub / "go.mod").write_text("module x\n")
    profile = detect(tmp_path)
    assert profile.kind == "go"
    assert any("backend" in n for n in profile.notes)


def test_services_are_collected_across_units(tmp_path):
    """【回归测试】monorepo 里能跑起来的那个 app 往往不是主单元。

    `shop/` 根目录只是 workspace 管理者，真正的前端在 `apps/web/`。
    只问主单元会得出"这个项目没法启动"—— 而它明明有前端。
    """
    profile = detect(_shop(tmp_path))
    by_name = {s.name: s for s in profile.services()}
    # 根目录（workspace 管理者）起不了任何东西，但两个子应用都能起
    assert set(by_name) == {"api", "web"}
    assert by_name["web"].cwd == "apps/web"       # 在子目录里跑，不是仓库根
    assert by_name["api"].cwd == "apps/api"
    # Next 和 Nest 的默认端口都是 3000 —— 顺带验证了端口错开真的生效
    assert {by_name["api"].port, by_name["web"].port} == {3000, 3001}


def test_colliding_ports_are_offset(tmp_path):
    """Next 和 Nest 默认端口都是 3000。不错开的话第二个服务会撞上第一个 ——
    一个看起来像环境问题的自伤。"""
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    for name in ("a", "b"):
        d = tmp_path / "apps" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps(
            {"dependencies": {"next": "^14"},
             "scripts": {"dev": "next dev", "test": "jest"}}))
    services = detect(tmp_path).services()
    ports = sorted(s.port for s in services)
    assert ports == [3000, 3001]
    assert len({s.name for s in services}) == 2          # 名字也不能重
    assert all(s.env["PORT"] == str(s.port) for s in services)   # 告诉框架新端口


def test_unit_test_output_is_parsed_by_the_units_own_adapter(tmp_path):
    """【回归测试】拿根目录的解析器去读子项目的 vitest 输出会得出"未解析成功"。

    根是 turbo/workspace 管理者（framework=generic），apps/web 用的是 vitest。
    明明手边就有一份读得懂它的 adapter。
    """
    profile = detect(_shop(tmp_path))
    root_unit = next(u for u in profile.repository.units if u.path == ".")
    web_unit = next(u for u in profile.repository.units if u.path == "apps/web")
    vitest_output = " Test Files  1 passed (1)\n      Tests  2 failed | 15 passed (17)\n"

    assert root_unit.adapter.parse_test_output(vitest_output, 1).parsed is False
    parsed = web_unit.adapter.parse_test_output(vitest_output, 1)
    assert parsed.parsed is True
    assert (parsed.passed, parsed.failed) == (15, 2)


def test_broken_package_json_does_not_kill_detection(tmp_path):
    """一个 adapter 的探测炸了不该拖垮整条链。"""
    (tmp_path / "package.json").write_text("{ this is not json")
    profile = detect(tmp_path)
    assert profile.kind in ("node", "unknown")   # 不抛异常就算过
