"""adapter 探测的单元测试。每种技术栈一条，外加 override / unknown 两个边界。"""

import json

from repopilot.adapters import detect


def test_python_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    profile = detect(tmp_path)
    assert profile.kind == "python"
    assert "pytest" in profile.test_cmd


def test_rust_repo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    profile = detect(tmp_path)
    assert profile.kind == "rust"
    assert profile.test_cmd == "cargo test"


def test_go_repo(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    profile = detect(tmp_path)
    assert profile.kind == "go"
    assert profile.test_cmd == "go test ./..."


def test_maven_repo(tmp_path):
    (tmp_path / "pom.xml").write_text("<project></project>")
    profile = detect(tmp_path)
    assert profile.kind == "java-maven"
    assert "mvn" in profile.test_cmd


def test_gradle_prefers_wrapper(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    assert detect(tmp_path).test_cmd == "gradle test"        # 无 wrapper
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    assert detect(tmp_path).test_cmd == "./gradlew test"     # 有 wrapper 就优先用


def test_ruby_repo(tmp_path):
    (tmp_path / ".rspec").write_text("")
    profile = detect(tmp_path)
    assert profile.kind == "ruby"
    assert "rspec" in profile.test_cmd


def test_nestjs_repo(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"@nestjs/core": "^10.0.0"},
        "scripts": {"test": "jest"},
    }))
    profile = detect(tmp_path)
    assert profile.kind == "nestjs"
    assert profile.test_cmd == "npm test --silent"


def test_make_fallback(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n")
    profile = detect(tmp_path)
    assert profile.kind == "make"
    assert profile.test_cmd == "make test"


def test_language_specific_wins_over_makefile(tmp_path):
    """既有 Cargo.toml 又有 Makefile 时，语言专属探测优先于 Makefile 兜底。"""
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n")
    assert detect(tmp_path).kind == "rust"


def test_unknown_repo(tmp_path):
    profile = detect(tmp_path)
    assert profile.kind == "unknown"
    assert profile.test_cmd is None


def test_override_wins(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    profile = detect(tmp_path, test_cmd_override="make test")
    assert profile.test_cmd == "make test"
    assert profile.kind == "custom"
