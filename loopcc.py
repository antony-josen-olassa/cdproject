#!/usr/bin/env python3
"""
loopcc - Loop Statement Compiler (front end)
============================================

A mini compiler front end for C-style loop statements:

    for (init; condition; update) body
    while (condition) body
    do body while (condition);

Phases
------
1. Lexical analysis   (lex)        source text      -> tokens
2. Syntax analysis    (Parser)     tokens           -> AST  (recursive descent,
                                                           with error recovery)
3. Semantic analysis  (Semantic)   AST              -> diagnostics
4. Intermediate code  (TACGenerator) AST            -> three-address code

Usage:  python loopcc.py program.c [--tokens] [--ast] [--tac]
"""
import argparse
import bisect
import math
import re
import sys
from collections import Counter
from types import SimpleNamespace

# --------------------------------------------------------------------------
# Language definition
# --------------------------------------------------------------------------
RANK = {"char": 0, "int": 1, "float": 2, "double": 3}  # type "width" for promotion
TYPES = set(RANK)
KEYWORDS = TYPES | {"for", "while", "do", "if", "else", "break", "continue"}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%="}
BINPREC = {
    "||": 1, "&&": 2,
    "==": 3, "!=": 3,
    "<": 4, ">": 4, "<=": 4, ">=": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
}
STMT_START = TYPES | {"for", "while", "do", "if", "break", "continue"}
LOOPS = {"For", "While", "DoWhile"}
FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}

TOKEN_RE = re.compile(
    "|".join(
        f"(?P<{name}>{pattern})"
        for name, pattern in [
            ("COMMENT", r"//[^\n]*|/\*.*?\*/"),
            ("BADCOMMENT", r"/\*.*"),                 # unterminated /* ...
            ("WS", r"\s+"),
            ("BADID", r"\d+[A-Za-z_]\w*"),            # 9abc
            ("FLOAT", r"\d+\.\d+"),
            ("INT", r"\d+"),
            ("ID", r"[A-Za-z_]\w*"),
            ("CHAR", r"'(?:\\.|[^\\'\n])'"),
            ("OP", r"\+\+|--|\+=|-=|\*=|/=|%=|==|!=|<=|>=|&&|\|\||[+\-*/%<>=!]"),
            ("PUNCT", r"[(){};,]"),
            ("BAD", r"."),
        ]
    ),
    re.S,
)


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------
class Diagnostics:
    def __init__(self):
        self.items = []  # (line, col, level, message)

    def error(self, line, col, msg):
        self.items.append((line, col, "error", msg))

    def warning(self, line, col, msg):
        self.items.append((line, col, "warning", msg))

    def note(self, line, col, msg):
        self.items.append((line, col, "note", msg))

    @property
    def errors(self):
        return sum(1 for i in self.items if i[2] == "error")

    @property
    def warnings(self):
        return sum(1 for i in self.items if i[2] == "warning")

    def messages(self, level=None):
        return [m for (_, _, lv, m) in self.items if level in (None, lv)]

    def render(self, src_lines, filename):
        out = []
        for line, col, level, msg in sorted(set(self.items), key=lambda x: (x[0], x[1])):
            out.append(f"{filename}:{line}:{col}: {level}: {msg}")
            if 1 <= line <= len(src_lines):
                text = src_lines[line - 1].rstrip("\r\n").replace("\t", " ")
                out.append(f"{line:>5} | {text}")
                out.append(f"{'':>5} | {' ' * (col - 1)}^")
        return "\n".join(out)


# --------------------------------------------------------------------------
# Phase 1: Lexical analysis
# --------------------------------------------------------------------------
class Token:
    __slots__ = ("kind", "value", "line", "col")

    def __init__(self, kind, value, line, col):
        self.kind, self.value, self.line, self.col = kind, value, line, col

    def __repr__(self):
        return f"Token({self.kind!r}, {self.value!r}, {self.line}:{self.col})"


def lex(text, diag):
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
    toks = []
    for m in TOKEN_RE.finditer(text):
        kind, val = m.lastgroup, m.group()
        line = bisect.bisect_right(line_starts, m.start())
        col = m.start() - line_starts[line - 1] + 1
        if kind in ("WS", "COMMENT"):
            continue
        if kind == "BADCOMMENT":
            diag.error(line, col, "unterminated comment (missing '*/')")
            continue
        if kind == "BADID":
            diag.error(line, col, f"invalid identifier '{val}' (identifiers cannot start with a digit)")
            toks.append(Token("ID", val, line, col))
            continue
        if kind == "BAD":
            hint = f" (did you mean '{val * 2}'?)" if val in "&|" else ""
            diag.error(line, col, f"unexpected character {val!r}{hint}")
            continue
        if kind == "ID":
            kind = val if val in KEYWORDS else "ID"
        elif kind in ("OP", "PUNCT"):
            kind = val
        toks.append(Token(kind, val, line, col))
    if toks:
        last = toks[-1]
        toks.append(Token("EOF", "", last.line, last.col + len(last.value)))
    else:
        toks.append(Token("EOF", "", 1, 1))
    return toks


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------
class Node:
    def __init__(self, kind, line, col, **fields):
        self.kind, self.line, self.col = kind, line, col
        self.__dict__.update(fields)

    def __repr__(self):
        return f"Node({self.kind})"


def children(n):
    for k, v in n.__dict__.items():
        if k in ("kind", "line", "col"):
            continue
        for x in v if isinstance(v, list) else [v]:
            if isinstance(x, Node):
                yield x
            elif isinstance(x, tuple) and x[1] is not None:  # declaration item
                yield x[1]


def walk(n):
    yield n
    for c in children(n):
        yield from walk(c)


def has_break(n):
    """True if a `break` that targets *this* loop occurs inside n."""
    if n is None:
        return False
    if n.kind == "Break":
        return True
    if n.kind in LOOPS:
        return False
    return any(has_break(c) for c in children(n))


def has_effect(n):
    return any(
        w.kind == "Assign" or (w.kind in ("Unary", "Postfix") and w.op in ("++", "--"))
        for w in walk(n)
    )


def unparse(n):
    k = n.kind
    if k == "Num":
        return n.value
    if k == "Var":
        return n.name
    if k == "Unary":
        return n.op + unparse(n.operand)
    if k == "Postfix":
        return unparse(n.operand) + n.op
    if k == "Assign":
        return f"{unparse(n.target)} {n.op} {unparse(n.value)}"
    if k == "Binary":
        def side(c):
            return f"({unparse(c)})" if c.kind == "Binary" else unparse(c)
        return f"{side(n.left)} {n.op} {side(n.right)}"
    return k


def const_eval(n):
    """Evaluate a constant expression, or return None if it is not constant."""
    k = n.kind
    if k == "Num":
        return n.num
    if k == "Unary" and n.op in ("-", "+", "!"):
        v = const_eval(n.operand)
        if v is None:
            return None
        return -v if n.op == "-" else (v if n.op == "+" else int(not v))
    if k == "Binary":
        a, b = const_eval(n.left), const_eval(n.right)
        if a is None or b is None:
            return None
        op = n.op
        if op in ("/", "%") and b == 0:
            return None
        ops = {
            "+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
            "/": lambda: a / b if isinstance(a, float) or isinstance(b, float) else int(a / b),
            "%": lambda: int(math.fmod(a, b)),
            "<": lambda: int(a < b), ">": lambda: int(a > b),
            "<=": lambda: int(a <= b), ">=": lambda: int(a >= b),
            "==": lambda: int(a == b), "!=": lambda: int(a != b),
            "&&": lambda: int(bool(a) and bool(b)), "||": lambda: int(bool(a) or bool(b)),
        }
        return ops[op]()
    return None


def dump_ast(n, depth=0, tag=""):
    skip = ("kind", "line", "col", "num")
    pad = "  " * depth
    scalars = {k: v for k, v in n.__dict__.items()
               if k not in skip and not isinstance(v, (Node, list, type(None)))}
    head = f"{pad}{tag}{n.kind}"
    if scalars:
        head += " (" + ", ".join(f"{k}={v!r}" for k, v in scalars.items()) + ")"
    lines = [head]
    for k, v in n.__dict__.items():
        if k in skip:
            continue
        if isinstance(v, Node):
            lines += dump_ast(v, depth + 1, f"{k}: ")
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, Node):
                    lines += dump_ast(x, depth + 1, f"{k}: ")
                elif isinstance(x, tuple):
                    lines.append(f"{pad}  {k}: {x[0]}")
                    if x[1] is not None:
                        lines += dump_ast(x[1], depth + 2, "init: ")
        elif v is None and k in ("init", "cond", "update"):
            lines.append(f"{pad}  {k}: <empty>")
    return lines


# --------------------------------------------------------------------------
# Phase 2: Syntax analysis  (recursive descent + panic-mode / insertion recovery)
#
#   program   -> stmt*
#   stmt      -> decl | for | while | do_while | if | block
#              | 'break' ';' | 'continue' ';' | expr ';' | ';'
#   decl      -> type ID ['=' expr] (',' ID ['=' expr])* ';'
#   for       -> 'for' '(' [decl_noSemi | exprlist] ';' [expr] ';' [exprlist] ')' stmt
#   while     -> 'while' '(' expr ')' stmt
#   do_while  -> 'do' stmt 'while' '(' expr ')' ';'
#   expr      -> assignment ;  assignment -> binary [assign_op assignment]
#   binary    -> precedence climbing over || && == != < > <= >= + - * / %
#   unary     -> ('!' | '-' | '+' | '++' | '--') unary | postfix
#   postfix   -> primary ('++' | '--')*
#   primary   -> INT | FLOAT | CHAR | ID | '(' expr ')'
# --------------------------------------------------------------------------
class ParseError(Exception):
    pass


class Parser:
    def __init__(self, toks, diag):
        self.t, self.i, self.d = toks, 0, diag

    # ---- helpers ----
    @property
    def tok(self):
        return self.t[self.i]

    def at(self, *kinds):
        return self.tok.kind in kinds

    def advance(self):
        tok = self.tok
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def describe(self):
        return "end of file" if self.at("EOF") else f"'{self.tok.value}'"

    def fail(self, msg, tok=None):
        tok = tok or self.tok
        self.d.error(tok.line, tok.col, msg)
        raise ParseError()

    def expect(self, kind, ctx="", recover=False):
        if self.tok.kind == kind:
            return self.advance()
        what = "identifier" if kind == "ID" else f"'{kind}'"
        msg = f"expected {what}" + (f" {ctx}" if ctx else "") + f", found {self.describe()}"
        if recover and self.i > 0:  # "insert" the missing token, keep parsing
            p = self.t[self.i - 1]
            self.d.error(p.line, p.col + len(p.value), msg)
            return None
        self.fail(msg)

    def sync(self):
        """Panic mode: skip to a safe point after a syntax error."""
        while not self.at("EOF"):
            if self.at(";"):
                self.advance()
                return
            if self.at("}") or self.tok.kind in STMT_START:
                return
            self.advance()

    def header_has_comma(self):
        depth, j = 0, self.i
        while self.t[j].kind not in ("EOF", "{"):
            k = self.t[j].kind
            depth += (k == "(") - (k == ")")
            if depth < 0:
                break
            if k == "," and depth == 0:
                return True
            j += 1
        return False

    def skip_to_close_paren(self):
        depth = 1
        while not self.at("EOF", "{"):
            if self.at("("):
                depth += 1
            elif self.at(")"):
                depth -= 1
                if depth == 0:
                    self.advance()
                    return
            self.advance()

    # ---- statements ----
    def parse_program(self):
        stmts = []
        while not self.at("EOF"):
            if self.at("}"):
                self.d.error(self.tok.line, self.tok.col, "unmatched '}' (no open '{')")
                self.advance()
                continue
            s = self.safe_stmt()
            if s:
                stmts.append(s)
        return Node("Program", 1, 1, stmts=stmts)

    def safe_stmt(self):
        start = self.i
        try:
            return self.parse_stmt()
        except ParseError:
            self.sync()
            if self.i == start and not self.at("EOF", "}"):
                self.advance()  # guarantee progress
            return None

    def parse_block(self):
        lb = self.advance()
        stmts = []
        while not self.at("}", "EOF"):
            s = self.safe_stmt()
            if s:
                stmts.append(s)
        if self.at("EOF"):
            self.d.error(lb.line, lb.col, "unclosed '{' (reached end of file, missing '}')")
        else:
            self.advance()
        return Node("Block", lb.line, lb.col, stmts=stmts)

    def parse_stmt(self):
        t = self.tok
        k = t.kind
        if k == ";":
            self.advance()
            return Node("Empty", t.line, t.col)
        if k == "{":
            return self.parse_block()
        if k in TYPES:
            return self.parse_decl()
        if k == "for":
            return self.parse_for()
        if k == "while":
            return self.parse_while()
        if k == "do":
            return self.parse_do()
        if k == "if":
            return self.parse_if()
        if k in ("break", "continue"):
            self.advance()
            self.expect(";", f"after '{k}'", recover=True)
            return Node("Break" if k == "break" else "Continue", t.line, t.col)
        if k == "else":
            self.fail("'else' without a matching 'if'")
        e = self.parse_expr()
        self.expect(";", "after expression", recover=True)
        return Node("ExprStmt", t.line, t.col, expr=e)

    def parse_body(self, what):
        if self.at("}", "EOF"):
            self.fail(f"missing body for {what} loop, found {self.describe()}")
        return self.parse_stmt()

    def parse_decl(self, semi=True):
        t = self.advance()
        items = []
        while True:
            idt = self.tok
            if idt.kind != "ID":
                self.fail(f"expected identifier in declaration, found {self.describe()}")
            self.advance()
            init = None
            if self.at("="):
                self.advance()
                init = self.parse_assignment()
            items.append((idt.value, init, idt.line, idt.col))
            if self.at(","):
                self.advance()
                continue
            break
        if semi:
            self.expect(";", "after declaration", recover=True)
        return Node("Decl", t.line, t.col, type=t.kind, items=items)

    def parse_for(self):
        kw = self.advance()
        self.expect("(", "after 'for'")
        init = cond = upd = None
        try:
            if not self.at(";"):
                init = self.parse_decl(semi=False) if self.tok.kind in TYPES else self.parse_expr_list()
            if self.at(")"):
                self.fail("malformed for-header: expected 'init; condition; update' "
                          "(the three parts must be separated by ';', not ',')")
            if not self.at(";"):
                hint = " (the parts of a for header are separated by ';', not ',')" \
                    if self.header_has_comma() else ""
                self.fail(f"expected ';' after for-loop initialization, found {self.describe()}{hint}")
            self.advance()
            if not self.at(";"):
                cond = self.parse_expr()
            if self.at(")"):
                self.fail("malformed for-header: missing second ';' "
                          "(expected 'init; condition; update')")
            self.expect(";", "after for-loop condition", recover=True)
            if not self.at(")"):
                upd = self.parse_expr_list()
            self.expect(")", "to close the for-loop header", recover=True)
        except ParseError:
            self.skip_to_close_paren()
        body = self.parse_body("for")
        return Node("For", kw.line, kw.col, init=init, cond=cond, update=upd, body=body)

    def parse_while(self):
        kw = self.advance()
        self.expect("(", "after 'while'")
        if self.at(")"):
            self.d.error(self.tok.line, self.tok.col, "empty condition in while loop")
            cond = Node("Num", self.tok.line, self.tok.col, value="0", num=0, type="int")
        else:
            cond = self.parse_expr()
        self.expect(")", "after while condition", recover=True)
        body = self.parse_body("while")
        return Node("While", kw.line, kw.col, cond=cond, body=body)

    def parse_do(self):
        kw = self.advance()
        body = self.parse_body("do-while")
        if not self.at("while"):
            self.fail(f"expected 'while' after do-loop body, found {self.describe()}")
        self.advance()
        self.expect("(", "after 'while'")
        if self.at(")"):
            self.d.error(self.tok.line, self.tok.col, "empty condition in do-while loop")
            cond = Node("Num", self.tok.line, self.tok.col, value="0", num=0, type="int")
        else:
            cond = self.parse_expr()
        self.expect(")", "after do-while condition", recover=True)
        self.expect(";", "after do-while condition (do-while must end with ';')", recover=True)
        return Node("DoWhile", kw.line, kw.col, body=body, cond=cond)

    def parse_if(self):
        kw = self.advance()
        self.expect("(", "after 'if'")
        cond = self.parse_expr()
        self.expect(")", "after if condition", recover=True)
        then = self.parse_stmt()
        els = None
        if self.at("else"):
            self.advance()
            els = self.parse_stmt()
        return Node("If", kw.line, kw.col, cond=cond, then=then, els=els)

    # ---- expressions ----
    def parse_expr(self):
        return self.parse_assignment()

    def parse_expr_list(self):
        items = [self.parse_assignment()]
        while self.at(","):
            self.advance()
            items.append(self.parse_assignment())
        return items

    def parse_assignment(self):
        left = self.parse_binary(1)
        if self.tok.kind in ASSIGN_OPS:
            op = self.advance()
            if left.kind != "Var":
                self.d.error(op.line, op.col,
                             f"left side of '{op.value}' must be a variable, not '{unparse(left)}'")
            right = self.parse_assignment()
            return Node("Assign", op.line, op.col, op=op.value, target=left, value=right)
        return left

    def parse_binary(self, min_prec):
        left = self.parse_unary()
        while self.tok.kind in BINPREC and BINPREC[self.tok.kind] >= min_prec:
            op = self.advance()
            right = self.parse_binary(BINPREC[op.kind] + 1)
            left = Node("Binary", op.line, op.col, op=op.kind, left=left, right=right)
        return left

    def parse_unary(self):
        t = self.tok
        if t.kind in ("!", "-", "+", "++", "--"):
            self.advance()
            operand = self.parse_unary()
            if t.kind in ("++", "--") and operand.kind != "Var":
                self.d.error(t.line, t.col, f"operand of '{t.kind}' must be a variable")
            return Node("Unary", t.line, t.col, op=t.kind, operand=operand)
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while self.tok.kind in ("++", "--"):
            t = self.advance()
            if e.kind != "Var":
                self.d.error(t.line, t.col, f"operand of '{t.kind}' must be a variable")
            e = Node("Postfix", t.line, t.col, op=t.kind, operand=e)
        return e

    def parse_primary(self):
        t = self.tok
        if t.kind == "INT":
            self.advance()
            return Node("Num", t.line, t.col, value=t.value, num=int(t.value), type="int")
        if t.kind == "FLOAT":
            self.advance()
            return Node("Num", t.line, t.col, value=t.value, num=float(t.value), type="float")
        if t.kind == "CHAR":
            self.advance()
            body = t.value[1:-1]
            code = ord(bytes(body, "utf-8").decode("unicode_escape")) if body else 0
            return Node("Num", t.line, t.col, value=t.value, num=code, type="char")
        if t.kind == "ID":
            self.advance()
            return Node("Var", t.line, t.col, name=t.value)
        if t.kind == "(":
            self.advance()
            e = self.parse_expr()
            self.expect(")", "to close '('", recover=True)
            return e
        self.fail(f"expected expression, found {self.describe()}")


# --------------------------------------------------------------------------
# Phase 3: Semantic analysis
# --------------------------------------------------------------------------
class Semantic:
    def __init__(self, diag):
        self.d = diag
        self.scopes = [{}]
        self.loop_depth = 0
        self.max_depth = 0
        self.stats = Counter()

    def run(self, prog):
        for s in prog.stmts:
            self.stmt(s)

    # -- infrastructure --
    def err(self, n, msg):
        self.d.error(n.line, n.col, msg)

    def warn(self, n, msg):
        self.d.warning(n.line, n.col, msg)

    def note(self, n, msg):
        self.d.note(n.line, n.col, msg)

    def lookup(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def stmt(self, n):
        getattr(self, "s_" + n.kind)(n)

    def expr(self, n):
        return getattr(self, "e_" + n.kind)(n)

    # -- statements --
    def s_Empty(self, n):
        pass

    def s_Break(self, n):
        if self.loop_depth == 0:
            self.err(n, "'break' used outside of a loop")

    def s_Continue(self, n):
        if self.loop_depth == 0:
            self.err(n, "'continue' used outside of a loop")

    def s_Block(self, n):
        self.scopes.append({})
        for s in n.stmts:
            self.stmt(s)
        self.scopes.pop()

    def s_ExprStmt(self, n):
        self.expr(n.expr)
        if not has_effect(n.expr):
            self.warn(n.expr, "expression statement has no effect")

    def s_Decl(self, n):
        for name, init, line, col in n.items:
            it = self.expr(init) if init is not None else None
            if name in self.scopes[-1]:
                self.d.error(line, col, f"redeclaration of '{name}' in the same scope")
                continue
            if it is not None and RANK[it] > RANK[n.type]:
                self.d.warning(line, col, f"implicit conversion from '{it}' to '{n.type}' may lose precision")
            self.scopes[-1][name] = {"type": n.type, "assigned": init is not None}

    def s_If(self, n):
        self.expr(n.cond)
        if n.cond.kind == "Assign":
            self.warn(n.cond, "assignment used as if condition; did you mean '=='?")
        self.stmt(n.then)
        if n.els:
            self.stmt(n.els)

    def loop_body(self, node, body, kw):
        if body.kind == "Empty":
            self.warn(body, f"{kw} loop has an empty body ';' (stray semicolon?)")
        if body.kind == "Decl":
            self.err(body, "a declaration cannot be the direct body of a loop; use braces '{ }'")
        self.stats[kw] += 0
        self.loop_depth += 1
        self.max_depth = max(self.max_depth, self.loop_depth)
        self.stmt(body)
        self.loop_depth -= 1

    def check_cond(self, node, cond, kw, body):
        self.expr(cond)
        if cond.kind == "Assign":
            self.warn(cond, f"assignment used as {kw} condition; did you mean '=='?")
        c = const_eval(cond)
        if c is not None:
            if c == 0:
                if kw == "do-while":
                    self.note(node, "do-while condition is always false: body executes exactly once")
                else:
                    self.warn(node, f"{kw} condition is always false: loop body never executes")
            elif not has_break(body):
                self.warn(node, f"{kw} condition is always true and the body has no 'break': infinite loop")

    def check_termination(self, node, cond, update, body):
        """Heuristic checks on increment/decrement logic."""
        if cond is None or const_eval(cond) is not None:
            return
        cond_vars = {w.name for w in walk(cond) if w.kind == "Var"}
        steps = self.collect_steps([*update, body])
        if not (cond_vars & set(steps)):
            if cond_vars and not has_break(body):
                names = ", ".join(f"'{v}'" for v in sorted(cond_vars))
                self.warn(node, f"possible infinite loop: condition variable(s) {names} "
                                f"never modified in the loop update or body")
            return
        # direction check: is the variable moving away from the exit condition?
        if cond.kind != "Binary" or cond.op not in FLIP:
            return
        if cond.left.kind == "Var":
            v, op, other = cond.left.name, cond.op, cond.right
        elif cond.right.kind == "Var":
            v, op, other = cond.right.name, FLIP[cond.op], cond.left
        else:
            return
        if {w.name for w in walk(other) if w.kind == "Var"} & set(steps):
            return
        s = steps.get(v)
        if not s or 0 in s:
            return
        if op in ("<", "<=") and all(x < 0 for x in s):
            self.warn(node, f"loop may never terminate: '{v}' is only decremented but the loop "
                            f"continues while ({unparse(cond)})")
        elif op in (">", ">=") and all(x > 0 for x in s):
            self.warn(node, f"loop may never terminate: '{v}' is only incremented but the loop "
                            f"continues while ({unparse(cond)})")

    @staticmethod
    def collect_steps(roots):
        steps = {}
        for root in roots:
            for n in walk(root):
                if n.kind in ("Unary", "Postfix") and n.op in ("++", "--") and n.operand.kind == "Var":
                    steps.setdefault(n.operand.name, []).append(1 if n.op == "++" else -1)
                elif n.kind == "Assign" and n.target.kind == "Var":
                    c = const_eval(n.value)
                    sign = 0
                    if c is not None and c != 0:
                        sign = (1 if c > 0 else -1) * (1 if n.op == "+=" else -1 if n.op == "-=" else 0)
                    steps.setdefault(n.target.name, []).append(sign)
        return steps

    def s_While(self, n):
        self.stats["while"] += 1
        self.check_cond(n, n.cond, "while", n.body)
        self.loop_body(n, n.body, "while")
        self.check_termination(n, n.cond, [], n.body)

    def s_DoWhile(self, n):
        self.stats["do-while"] += 1
        self.loop_body(n, n.body, "do-while")
        self.check_cond(n, n.cond, "do-while", n.body)
        self.check_termination(n, n.cond, [], n.body)

    def s_For(self, n):
        self.stats["for"] += 1
        self.scopes.append({})  # the loop variable's scope is the for statement
        if n.init is not None:
            if isinstance(n.init, list):
                for e in n.init:
                    self.expr(e)
                    if not has_effect(e):
                        hint = " (did you mean '='?)" if e.kind == "Binary" and e.op == "==" else ""
                        self.warn(e, f"for-loop initialization has no effect{hint}")
            else:
                self.stmt(n.init)
        if n.cond is None:
            if not has_break(n.body):
                self.note(n, "for loop has no condition: it runs forever unless exited with 'break'")
        else:
            self.check_cond(n, n.cond, "for", n.body)
        for e in n.update or []:
            self.expr(e)
            if not has_effect(e):
                self.warn(e, "for-loop update has no effect; the loop variable is never changed here")
        self.loop_body(n, n.body, "for")
        self.check_termination(n, n.cond, n.update or [], n.body)
        self.scopes.pop()

    # -- expressions (return the static type) --
    def e_Num(self, n):
        return n.type

    def e_Var(self, n):
        sym = self.lookup(n.name)
        if sym is None:
            self.err(n, f"use of undeclared identifier '{n.name}'")
            return "int"
        if not sym["assigned"]:
            self.warn(n, f"variable '{n.name}' is used before being initialized")
            sym["assigned"] = True
        return sym["type"]

    def e_Unary(self, n):
        t = self.expr(n.operand)
        return "int" if n.op == "!" else max(t, "int", key=RANK.get)

    e_Postfix = e_Unary

    def e_Binary(self, n):
        lt, rt = self.expr(n.left), self.expr(n.right)
        if n.op in ("&&", "||", "==", "!=", "<", ">", "<=", ">="):
            return "int"
        if n.op in ("/", "%"):
            c = const_eval(n.right)
            if c is not None and c == 0:
                self.err(n, "division by zero")
        if n.op == "%" and (RANK[lt] >= 2 or RANK[rt] >= 2):
            self.err(n, "operands of '%' must be integers")
        return max(lt, rt, "int", key=RANK.get)

    def e_Assign(self, n):
        target = n.target
        if target.kind != "Var":
            self.expr(n.value)
            return "int"
        sym = self.lookup(target.name)
        if sym is None:
            self.err(target, f"use of undeclared identifier '{target.name}'")
            tt = "int"
        else:
            tt = sym["type"]
            if n.op != "=" and not sym["assigned"]:
                self.warn(target, f"variable '{target.name}' is used before being initialized")
        rt = self.expr(n.value)
        if sym is not None:
            sym["assigned"] = True
        if n.op in ("/=", "%="):
            c = const_eval(n.value)
            if c is not None and c == 0:
                self.err(n, "division by zero")
        if n.op == "%=" and (RANK[tt] >= 2 or RANK[rt] >= 2):
            self.err(n, "operands of '%=' must be integers")
        if RANK[rt] > RANK[tt]:
            self.warn(n, f"implicit conversion from '{rt}' to '{tt}' may lose precision")
        return tt


# --------------------------------------------------------------------------
# Phase 4: Intermediate code generation (three-address code)
# --------------------------------------------------------------------------
class TACGenerator:
    def __init__(self):
        self.code, self.nt, self.nl, self.loops = [], 0, 0, []

    def generate(self, prog):
        for s in prog.stmts:
            self.stmt(s)
        return self.code

    def temp(self):
        self.nt += 1
        return f"t{self.nt}"

    def label(self):
        self.nl += 1
        return f"L{self.nl}"

    def emit(self, s):
        self.code.append("    " + s)

    def place(self, label):
        self.code.append(f"{label}:")

    def stmt(self, n):
        getattr(self, "g_" + n.kind)(n)

    def g_Empty(self, n):
        pass

    def g_Block(self, n):
        for s in n.stmts:
            self.stmt(s)

    def g_Decl(self, n):
        for name, init, _, _ in n.items:
            if init is not None:
                v = self.expr(init)
                self.emit(f"{name} = {v}")

    def g_ExprStmt(self, n):
        self.effect(n.expr)

    def g_Break(self, n):
        self.emit(f"goto {self.loops[-1][1]}")

    def g_Continue(self, n):
        self.emit(f"goto {self.loops[-1][0]}")

    def g_If(self, n):
        c = self.expr(n.cond)
        else_l = self.label()
        self.emit(f"ifFalse {c} goto {else_l}")
        self.stmt(n.then)
        if n.els:
            end = self.label()
            self.emit(f"goto {end}")
            self.place(else_l)
            self.stmt(n.els)
            self.place(end)
        else:
            self.place(else_l)

    def g_While(self, n):
        start, end = self.label(), self.label()
        self.place(start)
        c = self.expr(n.cond)
        self.emit(f"ifFalse {c} goto {end}")
        self.loops.append((start, end))
        self.stmt(n.body)
        self.loops.pop()
        self.emit(f"goto {start}")
        self.place(end)

    def g_DoWhile(self, n):
        start, cont, end = self.label(), self.label(), self.label()
        self.place(start)
        self.loops.append((cont, end))
        self.stmt(n.body)
        self.loops.pop()
        self.place(cont)
        c = self.expr(n.cond)
        self.emit(f"if {c} goto {start}")
        self.place(end)

    def g_For(self, n):
        if n.init is not None:
            if isinstance(n.init, list):
                for e in n.init:
                    self.effect(e)
            else:
                self.stmt(n.init)
        start, cont, end = self.label(), self.label(), self.label()
        self.place(start)
        if n.cond is not None:
            c = self.expr(n.cond)
            self.emit(f"ifFalse {c} goto {end}")
        self.loops.append((cont, end))
        self.stmt(n.body)
        self.loops.pop()
        self.place(cont)
        for e in n.update or []:
            self.effect(e)
        self.emit(f"goto {start}")
        self.place(end)

    # expression whose value is not needed (statement / for-update)
    def effect(self, e):
        if e.kind in ("Unary", "Postfix") and e.op in ("++", "--"):
            v = e.operand.name
            self.emit(f"{v} = {v} {'+' if e.op == '++' else '-'} 1")
        else:
            self.expr(e)

    def expr(self, n):
        k = n.kind
        if k == "Num":
            return n.value
        if k == "Var":
            return n.name
        if k == "Unary":
            if n.op in ("++", "--"):
                v = n.operand.name
                self.emit(f"{v} = {v} {'+' if n.op == '++' else '-'} 1")
                return v
            a = self.expr(n.operand)
            if n.op == "+":
                return a
            t = self.temp()
            self.emit(f"{t} = {n.op}{a}")
            return t
        if k == "Postfix":
            v = n.operand.name
            t = self.temp()
            self.emit(f"{t} = {v}")
            self.emit(f"{v} = {v} {'+' if n.op == '++' else '-'} 1")
            return t
        if k == "Assign":
            name = n.target.name
            if n.op == "=":
                v = self.expr(n.value)
                self.emit(f"{name} = {v}")
            else:
                v = self.expr(n.value)
                self.emit(f"{name} = {name} {n.op[0]} {v}")
            return name
        if k == "Binary":
            if n.op in ("&&", "||"):  # short-circuit evaluation
                r, end = self.temp(), self.label()
                a = self.expr(n.left)
                self.emit(f"{r} = {a}")
                self.emit(f"{'ifFalse' if n.op == '&&' else 'if'} {r} goto {end}")
                b = self.expr(n.right)
                self.emit(f"{r} = {b}")
                self.place(end)
                return r
            a, b = self.expr(n.left), self.expr(n.right)
            t = self.temp()
            self.emit(f"{t} = {a} {n.op} {b}")
            return t
        raise ValueError(k)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def compile_source(text):
    diag = Diagnostics()
    toks = lex(text, diag)
    ast = Parser(toks, diag).parse_program()
    sem = tac = None
    if diag.errors == 0:
        sem = Semantic(diag)
        sem.run(ast)
        if diag.errors == 0:
            tac = TACGenerator().generate(ast)
    return SimpleNamespace(tokens=toks, ast=ast, diag=diag, sem=sem, tac=tac)


def main():
    ap = argparse.ArgumentParser(description="Loop Statement Compiler front end (for / while / do-while)")
    ap.add_argument("source", nargs="?", help="source file (default: read stdin)")
    ap.add_argument("--tokens", action="store_true", help="print the token stream")
    ap.add_argument("--ast", action="store_true", help="print the abstract syntax tree")
    ap.add_argument("--tac", action="store_true", help="print three-address code")
    args = ap.parse_args()

    name = args.source or "<stdin>"
    text = open(args.source, encoding="utf-8").read() if args.source else sys.stdin.read()
    res = compile_source(text)

    if args.tokens:
        print("== TOKENS ==")
        for t in res.tokens:
            print(f"{t.line:>3}:{t.col:<3} {t.kind:<10} {t.value}")
        print()

    report = res.diag.render(text.splitlines(), name)
    if report:
        print(report)
        print()

    d = res.diag
    if d.errors == 0:
        s = res.sem.stats
        print(f"OK: {name} is a valid program "
              f"(for={s['for']}, while={s['while']}, do-while={s['do-while']}, "
              f"max loop nesting={res.sem.max_depth}; {d.warnings} warning(s))")
        if args.ast:
            print("\n== AST ==")
            print("\n".join(dump_ast(res.ast)))
        if args.tac:
            print("\n== THREE-ADDRESS CODE ==")
            print("\n".join(res.tac))
    else:
        print(f"FAILED: {d.errors} error(s), {d.warnings} warning(s)")
    sys.exit(1 if d.errors else 0)


if __name__ == "__main__":
    main()
