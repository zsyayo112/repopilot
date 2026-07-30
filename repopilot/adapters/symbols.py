"""多语言符号提取：把"文件骨架"这件事从 Python 独占变成人人有份。

【为什么需要它】
真实仓库里一个文件动辄上千行，整读就是烧上下文。正确姿势是先看骨架
（有哪些类/函数、在第几行），再按行号范围精读那一小段。旧版只支持 .py
（Python 自带 ast），于是"先看骨架"这个省钱技巧对 TS/Go/Java 全部失效 ——
模型只能整读，或者瞎猜行号。

【为什么不直接上 tree-sitter】
tree-sitter 是正解（真语法树、几十种语言），但它是一个需要编译的原生依赖。
本项目的立身之本是"依赖只有 openai + python-dotenv，三年后还能跑"，
为一个"看骨架"的功能引入原生依赖，性价比不对。

所以这里的取舍是明确的、也说得出代价的：

    Python  → 真 AST（准确）
    其他语言 → 逐行正则（近似）

正则版会漏（宏、装饰器里的定义、单行压缩过的代码），也可能误报（字符串里
出现 "function " 之类）。但它拿到的是这个功能 80% 的价值：**知道大概有什么、
在第几行**。定位不准，模型下一步 read_file 一读就发现了，代价是一次工具调用；
而完全没有骨架，代价是整读一个两千行的文件。

升级路线写在这里，将来只需要换掉 extract() 的实现，调用方一行不用改：
    正则 → tree-sitter → LSP（复用各语言现成的语言服务器）
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .base import Symbol

# 后缀 → 语言名。同一个语言可能有多个后缀（.ts/.tsx、.js/.jsx/.mjs）。
LANG_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
}

# 每种语言一组"行首正则"。约定：
#   命名组 name 必须存在；kind 从 pattern 附带的标签来。
# 只匹配【行首缩进后】的定义，避免把字符串、注释里的关键字当成定义。
_RULES: dict[str, list[tuple[str, str]]] = {
    "typescript": [
        ("class",     r"(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>\w+)"),
        ("interface", r"(?:export\s+)?interface\s+(?P<name>\w+)"),
        ("type",      r"(?:export\s+)?type\s+(?P<name>\w+)"),
        ("enum",      r"(?:export\s+)?(?:const\s+)?enum\s+(?P<name>\w+)"),
        ("def",       r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>\w+)"),
        # 箭头函数常量：const foo = (a, b) => / const foo = async () =>
        ("def",       r"(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]*)?=>"),
        # 类方法：public async foo(...)  /  foo(...) {   —— 需要缩进，避免匹配到调用
        ("method",    r"(?:public|private|protected|static|readonly|async|\s)*(?P<name>\w+)\s*\([^;]*\)\s*(?::[^{;]+)?\{"),
        ("decorator", r"@(?P<name>Controller|Injectable|Module|Entity|Component)\b"),
    ],
    "go": [
        ("func",   r"func\s+(?P<name>\w+)\s*\("),
        ("method", r"func\s+\((?P<recv>[^)]*)\)\s*(?P<name>\w+)\s*\("),
        ("type",   r"type\s+(?P<name>\w+)\s+(?:struct|interface|func|map|\[)"),
    ],
    "rust": [
        ("fn",     r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)"),
        ("struct", r"(?:pub(?:\([^)]*\))?\s+)?struct\s+(?P<name>\w+)"),
        ("enum",   r"(?:pub(?:\([^)]*\))?\s+)?enum\s+(?P<name>\w+)"),
        ("trait",  r"(?:pub(?:\([^)]*\))?\s+)?trait\s+(?P<name>\w+)"),
        # impl 块的完整头部（"Matcher for Query"）比只取第一个标识符有用得多：
        # 一个类型可能有五个 impl 块，只写 "Query" 五行长得一模一样
        ("impl",   r"impl(?:<[^>]*>)?\s+(?P<name>[^{]+?)\s*\{"),
    ],
    "java": [
        ("class",     r"(?:public|private|protected|abstract|final|static|\s)*class\s+(?P<name>\w+)"),
        ("interface", r"(?:public|private|protected|\s)*interface\s+(?P<name>\w+)"),
        ("enum",      r"(?:public|private|protected|\s)*enum\s+(?P<name>\w+)"),
        ("method",    r"(?:public|private|protected|static|final|synchronized|abstract|\s)+[\w<>\[\],\s?]+\s+(?P<name>\w+)\s*\([^)]*\)\s*(?:throws [\w., ]+)?\{"),
    ],
    "ruby": [
        ("class",  r"class\s+(?P<name>[\w:]+)"),
        ("module", r"module\s+(?P<name>[\w:]+)"),
        ("def",    r"def\s+(?P<name>[\w.?!=\[\]]+)"),
    ],
}
_RULES["javascript"] = _RULES["typescript"]
_RULES["kotlin"] = [
    ("class", r"(?:open\s+|data\s+|sealed\s+|abstract\s+)*class\s+(?P<name>\w+)"),
    ("fun",   r"(?:override\s+|suspend\s+|private\s+|public\s+)*fun\s+(?P<name>\w+)"),
]

_COMPILED = {
    lang: [(kind, re.compile(r"^(?P<indent>\s*)" + pat)) for kind, pat in rules]
    for lang, rules in _RULES.items()
}

# 这些行首关键字是控制流，不是定义 —— TS 的 method 规则会误伤它们
_TS_KEYWORD_TRAP = {"if", "for", "while", "switch", "catch", "return", "do",
                    "else", "try", "function", "class", "constructor", "new"}


def language_of(path: Path) -> str | None:
    return LANG_BY_SUFFIX.get(path.suffix.lower())


def extract(path: Path) -> list[Symbol] | None:
    """提取一个文件的符号。返回 None = 这种文件类型不支持（调用方好给出明确提示）。"""
    lang = language_of(path)
    if lang is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if lang == "python":
        return _python_symbols(text)
    return _regex_symbols(text, lang)


def _python_symbols(text: str) -> list[Symbol]:
    """真 AST 版。ast.walk 顺序不保证，所以最后按行号排。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # 语法错误时不要放弃 —— agent 可能正处在"改坏了正在修"的中间状态，
        # 这时候恰恰最需要看骨架。降级到正则版（Python 的 def/class 正则很稳）。
        return _regex_symbols(text, "python_fallback")

    out: list[Symbol] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(_expr_name(b) for b in node.bases)
            out.append(Symbol(node.lineno, "class", node.name,
                              f"({bases})" if bases else "", node.col_offset))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            if node.args.vararg:
                args.append("*" + node.args.vararg.arg)
            if node.args.kwarg:
                args.append("**" + node.args.kwarg.arg)
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            out.append(Symbol(node.lineno, kind, node.name,
                              f"({', '.join(args)})", node.col_offset))
    out.sort(key=lambda s: s.line)
    return out


def _expr_name(node: ast.expr) -> str:
    """把基类表达式还原成大致的名字，够读就行，不追求完整。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_expr_name(node.value)}.{node.attr}"
    return "…"


_PY_FALLBACK = [
    ("class", re.compile(r"^(?P<indent>\s*)class\s+(?P<name>\w+)")),
    ("def", re.compile(r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>\w+)")),
]


def _regex_symbols(text: str, lang: str) -> list[Symbol]:
    rules = _PY_FALLBACK if lang == "python_fallback" else _COMPILED.get(lang, [])
    out: list[Symbol] = []
    seen: set[tuple[int, str]] = set()

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("//", "#", "*", "/*")):
            continue
        for kind, pattern in rules:
            m = pattern.match(line)
            if not m:
                continue
            name = m.group("name").strip()
            if not name or (lang in ("typescript", "javascript")
                            and kind == "method" and name in _TS_KEYWORD_TRAP):
                continue
            key = (lineno, name)
            if key in seen:      # 一行可能命中多条规则，先命中的更具体，留它
                continue
            seen.add(key)
            recv = m.groupdict().get("recv")
            detail = f"  [{recv.strip()}]" if recv else ""
            out.append(Symbol(lineno, kind, name, detail, len(m.group("indent"))))
            break
    return out


def render(symbols: list[Symbol], path: Path, limit: int = 400) -> str:
    lang = language_of(path) or "?"
    head = f"# {path.name}（{lang}，{len(symbols)} 个符号）"
    if lang != "python":
        head += "\n# 注意：非 Python 语言用正则近似提取，可能有漏报/误报，行号以 read_file 为准"
    body = "\n".join(s.render() for s in symbols[:limit])
    if len(symbols) > limit:
        body += f"\n… 另有 {len(symbols) - limit} 个符号未显示"
    return f"{head}\n{body}" if symbols else f"{head}\n(没有识别到类或函数定义)"
