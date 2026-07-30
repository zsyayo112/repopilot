"""Java 技术栈（Maven / Gradle）。

这个 adapter 展示了最可靠的一种解析：**根本不解析终端文本**。
Maven Surefire 和 Gradle 都会把结果写成 JUnit XML 落盘，读 XML 比读
人类看的日志稳定得多 —— 日志格式随版本变，XML schema 二十年没动过。

顺序很重要：先试 XML，读不到才退回文本。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import (
    EnvRequirement,
    RepoAdapter,
    ServiceSpec,
    TestReport,
    ValidationStep,
    grab_int,
    tail,
)

# JUnit XML 的常见落盘位置
_REPORT_DIRS = [
    "target/surefire-reports",            # Maven
    "target/failsafe-reports",            # Maven 集成测试
    "build/test-results/test",            # Gradle
]


class _JavaBase(RepoAdapter):
    framework = "junit"

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        if report := self._parse_junit_xml(exit_code, output):
            return report
        return self._parse_text(output, exit_code)

    def _parse_junit_xml(self, exit_code: int, output: str) -> TestReport | None:
        files: list[Path] = []
        for rel in _REPORT_DIRS:
            d = self.root / rel
            if d.is_dir():
                files += sorted(d.glob("*.xml"))
        if not files:
            return None

        passed = failed = errors = skipped = 0
        names: list[str] = []
        for f in files:
            try:
                root_el = ET.parse(f).getroot()
            except (ET.ParseError, OSError):
                continue
            # 文件根可能是 <testsuite> 也可能是 <testsuites>
            suites = [root_el] if root_el.tag == "testsuite" else root_el.findall("testsuite")
            for suite in suites:
                total = int(suite.get("tests", 0))
                f_ = int(suite.get("failures", 0))
                e_ = int(suite.get("errors", 0))
                s_ = int(suite.get("skipped", 0))
                failed += f_
                errors += e_
                skipped += s_
                passed += max(total - f_ - e_ - s_, 0)
                for case in suite.findall("testcase"):
                    if case.find("failure") is not None or case.find("error") is not None:
                        names.append(f"{case.get('classname', '?')}#{case.get('name', '?')}")

        if passed == failed == errors == skipped == 0:
            return None
        return TestReport(
            exit_code=exit_code, passed=passed, failed=failed, errors=errors,
            skipped=skipped, failed_names=names, tail=tail(output),
            parsed=True, framework="junit(xml)",
        )

    def _parse_text(self, output: str, exit_code: int) -> TestReport:
        """Maven 汇总行：Tests run: 17, Failures: 2, Errors: 0, Skipped: 1
        一次构建里每个模块一行 + 最后一行总计，取【最后一行】避免重复累加。"""
        rows = re.findall(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)",
            output)
        if rows:
            total, failed, errors, skipped = (int(x) for x in rows[-1])
            return TestReport(
                exit_code=exit_code, passed=max(total - failed - errors - skipped, 0),
                failed=failed, errors=errors, skipped=skipped,
                failed_names=re.findall(r"^\[ERROR\]\s+(\S+#\S+)", output, re.M),
                tail=tail(output), parsed=True, framework="junit",
            )
        # Gradle 简报：`4 tests completed, 2 failed`
        done = grab_int(r"(\d+) tests? completed", output)
        if done is not None:
            failed = grab_int(r"(\d+) failed", output) or 0
            return TestReport(exit_code=exit_code, passed=max(done - failed, 0),
                              failed=failed, tail=tail(output), parsed=True,
                              framework="gradle")
        return super().parse_test_output(output, exit_code)

    def services(self) -> list[ServiceSpec]:
        """Spring Boot 是 Java 世界最常见的可运行应用。"""
        if not self._is_spring_boot():
            return []
        return [ServiceSpec(
            name="api", command=self.run_command(), port=8080,
            health_path="/actuator/health",
            ready_pattern=r"Started \w+Application in",
            startup_timeout=240,   # JVM 冷启动 + Spring 上下文，慢
        )]

    def _is_spring_boot(self) -> bool:
        for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            f = self.root / name
            if f.exists():
                try:
                    if "spring-boot" in f.read_text(encoding="utf-8", errors="ignore"):
                        return True
                except OSError:
                    pass
        return False

    def run_command(self) -> str:
        raise NotImplementedError

    def env_requirements(self) -> list[EnvRequirement]:
        return [EnvRequirement("java", ["java", "-version"], "安装 JDK 17+")]


class MavenAdapter(_JavaBase):
    kind = "java-maven"

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        if (root / "pom.xml").exists():
            return cls(root, ["发现 pom.xml"])
        return None

    def test_command(self) -> str | None:
        return "mvn -q -B test"

    def run_command(self) -> str:
        return "mvn -q -B spring-boot:run"

    def validation_steps(self) -> list[ValidationStep]:
        return [
            ValidationStep("test", "mvn -q -B test"),
            ValidationStep("build", "mvn -q -B -DskipTests package", required=False),
        ]

    def env_requirements(self) -> list[EnvRequirement]:
        return [*super().env_requirements(),
                EnvRequirement("mvn", ["mvn", "-v"], "安装 Maven，或用仓库自带的 ./mvnw")]


class GradleAdapter(_JavaBase):
    kind = "java-gradle"

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
            return cls(root, ["发现 build.gradle"])
        return None

    def _gradle(self) -> str:
        # 优先用项目自带的 wrapper（版本可复现），没有再退回全局 gradle
        return "./gradlew" if (self.root / "gradlew").exists() else "gradle"

    def test_command(self) -> str | None:
        return f"{self._gradle()} test"

    def run_command(self) -> str:
        return f"{self._gradle()} bootRun"

    def validation_steps(self) -> list[ValidationStep]:
        g = self._gradle()
        return [ValidationStep("test", f"{g} test"),
                ValidationStep("build", f"{g} build -x test", required=False)]

    def env_requirements(self) -> list[EnvRequirement]:
        reqs = list(super().env_requirements())
        if self._gradle() == "gradle":
            reqs.append(EnvRequirement("gradle", ["gradle", "-v"],
                                       "安装 Gradle（这个仓库没有 ./gradlew）"))
        return reqs
