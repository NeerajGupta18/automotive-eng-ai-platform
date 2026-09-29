"""A small, dependency-free C tokenizer with function and bracket matching.

It is NOT a compiler front end: macros are not expanded, types are not resolved and #if branches are all
scanned. The rules built on it are therefore heuristics; findings carry a confidence level and go through human
triage. Real static analysers (cppcheck, clang-tidy, ...) are ingested next to it (see ingest.py)."""
from __future__ import annotations

import re
from dataclasses import dataclass

KEYWORDS = {"if", "else", "for", "while", "do", "switch", "case", "default", "return", "break", "continue", "goto",
            "sizeof", "typedef", "struct", "union", "enum", "static", "extern", "const", "volatile", "inline",
            "register", "void", "int", "char", "short", "long", "float", "double", "signed", "unsigned"}
_NUM = re.compile(r"(?:0[xX][0-9A-Fa-f]+|0[bB][01]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)[uUlLfF]*")
_ID = re.compile(r"[A-Za-z_]\w*")
_OPS3 = ("<<=", ">>=", "...")
_OPS2 = ("->", "++", "--", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "##")


@dataclass
class Tok:
    kind: str          # id | num | str | chr | op
    text: str
    line: int


@dataclass
class Func:
    name: str
    line: int
    end_line: int
    lo: int            # index of '{'
    hi: int            # index of matching '}'
    returns_void: bool
    params: list[str]
    param_names: list[str]


@dataclass
class Lexed:
    toks: list[Tok]
    comments: dict[int, str]
    pp: list[tuple[int, str]]
    pairs: dict[int, int]          # opening index -> closing index and back
    funcs: list[Func]


def tokenize(src: str) -> tuple[list[Tok], dict[int, str], list[tuple[int, str]]]:
    toks: list[Tok] = []
    comments: dict[int, str] = {}
    pp: list[tuple[int, str]] = []
    i, n, line, bol = 0, len(src), 1, True
    while i < n:
        c = src[i]
        if c == "\n":
            line, i, bol = line + 1, i + 1, True
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            comments[line] = comments.get(line, "") + src[i + 2:j]
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k, part in enumerate(src[i:j].split("\n")):
                comments[line + k] = comments.get(line + k, "") + part
            line += src.count("\n", i, j)
            i, bol = j, False
            continue
        if c == "#" and bol:
            start, start_line = i, line
            while i < n:
                j = src.find("\n", i)
                j = n if j < 0 else j
                if src[i:j].rstrip().endswith("\\") and j < n:
                    i, line = j + 1, line + 1
                    continue
                i = j
                break
            pp.append((start_line, " ".join(src[start:i].replace("\\\n", " ").split())))
            continue
        bol = False
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            toks.append(Tok("str" if c == '"' else "chr", src[i:j + 1], line))
            i = j + 1
            continue
        m = _ID.match(src, i)
        if m:
            toks.append(Tok("id", m.group(), line))
            i = m.end()
            continue
        m = _NUM.match(src, i)
        if m and (c.isdigit() or c == "."):
            toks.append(Tok("num", m.group(), line))
            i = m.end()
            continue
        for op in _OPS3 + _OPS2:
            if src.startswith(op, i):
                toks.append(Tok("op", op, line))
                i += len(op)
                break
        else:
            toks.append(Tok("op", c, line))
            i += 1
    return toks, comments, pp


def match_pairs(toks: list[Tok]) -> dict[int, int]:
    pairs: dict[int, int] = {}
    stacks = {"(": [], "{": [], "[": []}
    close = {")": "(", "}": "{", "]": "["}
    for i, t in enumerate(toks):
        if t.kind != "op":
            continue
        if t.text in stacks:
            stacks[t.text].append(i)
        elif t.text in close and stacks[close[t.text]]:
            o = stacks[close[t.text]].pop()
            pairs[o], pairs[i] = i, o
    return pairs


def find_functions(toks: list[Tok], pairs: dict[int, int]) -> list[Func]:
    funcs, i, n, boundary, depth = [], 0, len(toks), 0, 0
    while i < n:
        t = toks[i]
        if t.text == "{" and t.kind == "op":
            if depth == 0 and i > 0 and toks[i - 1].text == ")" and (i - 1) in pairs and i in pairs:
                op = pairs[i - 1]
                if op > 0 and toks[op - 1].kind == "id" and toks[op - 1].text not in KEYWORDS:
                    ret = [x.text for x in toks[boundary:op - 1] if x.text not in ("static", "inline", "extern", "const", "volatile")]
                    params, cur, d = [], [], 0
                    for x in toks[op + 1:i - 1]:
                        if x.text in "([" and x.kind == "op":
                            d += 1
                        elif x.text in ")]" and x.kind == "op":
                            d -= 1
                        if x.text == "," and d == 0:
                            params.append(cur)
                            cur = []
                        else:
                            cur.append(x)
                    if cur:
                        params.append(cur)
                    if len(params) == 1 and [x.text for x in params[0]] == ["void"]:
                        params = []
                    names = [next((x.text for x in reversed(p) if x.kind == "id" and x.text not in KEYWORDS), "") for p in params]
                    end = pairs[i]
                    funcs.append(Func(toks[op - 1].text, toks[op - 1].line, toks[end].line, i, end, ret == ["void"],
                                      [" ".join(x.text for x in p) for p in params], names))
                    i, boundary = end + 1, end + 1
                    continue
            depth += 1
        elif t.text == "}" and t.kind == "op":
            depth = max(0, depth - 1)
            if depth == 0:
                boundary = i + 1
        elif t.text == ";" and t.kind == "op" and depth == 0:
            boundary = i + 1
        i += 1
    return funcs


def lex(src: str) -> Lexed:
    toks, comments, pp = tokenize(src)
    pairs = match_pairs(toks)
    return Lexed(toks, comments, pp, pairs, find_functions(toks, pairs))
