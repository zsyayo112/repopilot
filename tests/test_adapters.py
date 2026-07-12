"""adapter 探测的单元测试。"""

import json

from repopilot.adapters import detect


def test_python_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    profile = detect(tmp_path)
    assert profile.kind == "python"
    assert "pytest" in profile.test_cmd


def test_nestjs_repo(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"@nestjs/core": "^10.0.0"},
        "scripts": {"test": "jest"},
    }))
    profile = detect(tmp_path)
    assert profile.kind == "nestjs"
    assert profile.test_cmd == "npm test --silent"


def test_unknown_repo(tmp_path):
    profile = detect(tmp_path)
    assert profile.kind == "unknown"
    assert profile.test_cmd is None


def test_override_wins(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    profile = detect(tmp_path, test_cmd_override="make test")
    assert profile.test_cmd == "make test"
