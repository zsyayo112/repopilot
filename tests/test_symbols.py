"""多语言符号提取：让"先看骨架再精读"这个省钱技巧对所有语言都成立。

Python 走真 AST（准确），其余走正则（近似）。测试的重点不是"一个不漏"，
而是【关键定义都在、行号对得上、不误报控制流】。
"""

from pathlib import Path

from repopilot.adapters.symbols import extract, language_of


def test_python_uses_real_ast(tmp_path):
    p = tmp_path / "svc.py"
    p.write_text(
        "class UserService(Base):\n"
        "    def login(self, email, *args, **kw):\n"
        "        pass\n"
        "\n"
        "    async def logout(self):\n"
        "        pass\n"
    )
    syms = extract(p)
    rendered = "\n".join(s.render() for s in syms)
    assert "class UserService(Base)" in rendered
    assert "def login(self, email, *args, **kw)" in rendered
    assert "async def logout(self)" in rendered
    # 行号必须准 —— 模型下一步要靠它去 read_file
    assert [s.line for s in syms] == [1, 2, 5]


def test_python_syntax_error_falls_back_instead_of_giving_up(tmp_path):
    """agent 正处在"改坏了正在修"的中间状态时，恰恰最需要看骨架。"""
    p = tmp_path / "broken.py"
    p.write_text("class A:\n    def f(self):\n        return (((\n")
    syms = extract(p)
    names = [s.name for s in syms]
    assert "A" in names and "f" in names


def test_typescript(tmp_path):
    p = tmp_path / "cart.ts"
    p.write_text(
        "export interface CartItem { id: string }\n"
        "export type Money = number;\n"
        "export class CartService {\n"
        "  async total(items: CartItem[]): Promise<Money> {\n"
        "    if (items.length === 0) {\n"
        "      return 0;\n"
        "    }\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
        "export const format = (m: Money): string => `${m}`;\n"
    )
    syms = extract(p)
    by_name = {s.name: s for s in syms}
    assert "CartItem" in by_name and by_name["CartItem"].kind == "interface"
    assert "Money" in by_name and by_name["Money"].kind == "type"
    assert "CartService" in by_name and by_name["CartService"].kind == "class"
    assert "total" in by_name
    assert "format" in by_name
    # 控制流关键字绝不能被当成方法定义
    assert "if" not in by_name and "return" not in by_name


def test_go(tmp_path):
    p = tmp_path / "user.go"
    p.write_text(
        "package user\n"
        "type User struct {\n"
        "\tName string\n"
        "}\n"
        "func New(name string) *User {\n"
        "\treturn &User{name}\n"
        "}\n"
        "func (u *User) Login(pw string) error {\n"
        "\treturn nil\n"
        "}\n"
    )
    by_name = {s.name: s for s in extract(p)}
    assert by_name["User"].kind == "type"
    assert by_name["New"].kind == "func"
    assert by_name["Login"].kind == "method"
    assert "*User" in by_name["Login"].detail      # 接收者信息带上了


def test_rust(tmp_path):
    p = tmp_path / "lib.rs"
    p.write_text(
        "pub struct Query { pub op: String }\n"
        "pub trait Matcher { fn matches(&self) -> bool; }\n"
        "impl Matcher for Query {\n"
        "    fn matches(&self) -> bool { true }\n"
        "}\n"
        "pub async fn run() {}\n"
    )
    kinds = {s.name: s.kind for s in extract(p)}
    assert kinds["Query"] == "struct"
    assert kinds["Matcher"] == "trait"
    assert kinds["run"] == "fn"


def test_java(tmp_path):
    p = tmp_path / "Cart.java"
    p.write_text(
        "public class Cart {\n"
        "    public int total(List<Item> items) {\n"
        "        return 0;\n"
        "    }\n"
        "}\n"
    )
    kinds = {s.name: s.kind for s in extract(p)}
    assert kinds["Cart"] == "class"
    assert kinds["total"] == "method"


def test_ruby(tmp_path):
    p = tmp_path / "user.rb"
    p.write_text("module Auth\n  class User\n    def login!\n    end\n  end\nend\n")
    kinds = {s.name: s.kind for s in extract(p)}
    assert kinds["Auth"] == "module"
    assert kinds["User"] == "class"
    assert kinds["login!"] == "def"


def test_unsupported_suffix_returns_none_not_empty(tmp_path):
    """None（不支持）和 []（支持但没符号）是两种不同的信息，不能混。"""
    p = tmp_path / "data.csv"
    p.write_text("a,b\n1,2\n")
    assert extract(p) is None
    assert language_of(Path("x.md")) is None
    assert language_of(Path("x.tsx")) == "typescript"
