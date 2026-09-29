"""The built-in analyser: token-based checks over C sources. See lexer.py for the limitations."""
from __future__ import annotations

import hashlib
import re

from app.modules.codereview.lexer import KEYWORDS, Func, Lexed, lex
from app.modules.codereview.rules import DEFAULT_CONFIG, RULES

UNSAFE = {"gets": "error", "strcpy": "warning", "strcat": "warning", "sprintf": "warning", "vsprintf": "warning"}
SCANF = {"scanf", "sscanf", "fscanf"}
FORMAT_ARG = {"printf": 0, "fprintf": 1, "sprintf": 1, "snprintf": 2, "syslog": 1, "dprintf": 1}
EXEC = {"system", "popen", "execl", "execlp", "execv", "execvp"}
COPY_LEN = {"memcpy": 2, "memmove": 2, "memset": 2, "strncpy": 2, "strncat": 2}
ALLOC = {"malloc", "calloc", "realloc"}
CRITICAL_RET = {"fopen", "fread", "fwrite", "fgets", "read", "write", "recv", "send", "malloc", "calloc", "realloc",
                "snprintf", "fseek", "rename", "remove"}
RAND = {"rand", "srand", "random", "srandom", "drand48", "lrand48"}
STDIO = {"printf", "scanf", "fopen", "fclose", "fread", "fwrite", "fprintf", "fscanf", "fgets", "fputs", "puts", "getchar",
         "putchar", "sprintf", "snprintf", "sscanf", "vprintf", "vsprintf", "fseek", "ftell", "remove", "rename", "gets"}
SECRET_NAME = re.compile(r"(pass(word|wd|phrase)?|pwd|secret|token|api_?key|priv(ate)?_?key|crypto_?key|aes_?key|hmac_?key|auth_?key|"
                         r"session_?key|seed_?key|master_?key|key_?(bytes|material|value|data)|(^|_)key$)", re.I)
SECRET_EXCLUDE = re.compile(r"(len|size|max|min|count|idx|index|type|flag|enable|timeout|offset|id$)", re.I)
SEC_WORDS = re.compile(r"(seed|key|nonce|token|challenge|auth|secure|crypto|session)", re.I)
OCTAL = re.compile(r"^0[0-7]+[uUlL]*$")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())[:200]


class Ctx:
    def __init__(self, path: str, src: str, user_funcs: dict[str, bool]):
        self.path, self.lines, self.user_funcs = path, src.splitlines(), user_funcs
        self.L: Lexed = lex(src)
        self.t = self.L.toks
        self.raw: list[dict] = []

    def snippet(self, line: int) -> str:
        return norm(self.lines[line - 1]) if 0 < line <= len(self.lines) else ""

    def add(self, rule, line, message, fn=None, confidence="medium", severity=None):
        r = RULES[rule]
        self.raw.append({"rule": rule, "category": r["category"], "severity": severity or r["severity"], "confidence": confidence,
                         "title": r["title"], "message": message, "file": self.path, "line": line, "function": fn,
                         "snippet": self.snippet(line), "cwe": r["cwe"], "misra": r["misra"], "tool": "builtin", "origin": "rules"})

    def calls(self, lo: int, hi: int):
        t, p = self.t, self.L.pairs
        for i in range(lo, hi):
            if t[i].kind == "id" and i + 1 < len(t) and t[i + 1].text == "(" and (i + 1) in p and t[i].text not in KEYWORDS \
                    and (i == 0 or t[i - 1].text not in (".", "->")):
                close = p[i + 1]
                args, s, d = [], i + 2, 0
                for k in range(i + 2, close):
                    x = t[k]
                    if x.kind == "op" and x.text in "([{":
                        d += 1
                    elif x.kind == "op" and x.text in ")]}":
                        d -= 1
                    elif x.kind == "op" and x.text == "," and d == 0:
                        args.append((s, k))
                        s = k + 1
                if close > i + 2:
                    args.append((s, close))
                yield i, t[i].text, args, close

    def txt(self, a: int, b: int) -> list[str]:
        return [x.text for x in self.t[a:b]]

    def cond_ranges(self, lo: int, hi: int) -> list[tuple[int, int]]:
        out = []
        for i in range(lo, hi):
            if self.t[i].text in ("if", "while", "for", "switch") and i + 1 < len(self.t) and self.t[i + 1].text == "(" and (i + 1) in self.L.pairs:
                out.append((i + 1, self.L.pairs[i + 1]))
        return out

    def guard_positions(self, lo: int, hi: int) -> dict[str, int]:
        """First position of each identifier inside a condition or a MIN/MAX-style clamp."""
        g: dict[str, int] = {}
        for a, b in self.cond_ranges(lo, hi):
            for k in range(a, b):
                if self.t[k].kind == "id":
                    g.setdefault(self.t[k].text, k)
        for i, name, args, close in self.calls(lo, hi):
            if name.lower() in ("min", "max", "assert", "clamp") or name.upper().startswith(("MIN", "MAX", "ASSERT", "CLAMP")):
                for k in range(i, close):
                    if self.t[k].kind == "id":
                        g.setdefault(self.t[k].text, k)
        return g


# ---------------------------------------------------------------- per-function checks
def check_function(c: Ctx, f: Func, cfg: dict) -> dict:
    t, p, lo, hi, fn = c.t, c.L.pairs, f.lo, f.hi, f.name
    guard = c.guard_positions(lo, hi)
    calls = list(c.calls(lo, hi))
    idents_in_fn = {x.text for x in t[lo:hi] if x.kind == "id"}
    # ---- calls
    for i, name, args, close in calls:
        line = t[i].line
        stmt_start = i > 0 and _stmt_start(c, i)
        if name in UNSAFE:
            c.add("CR-SEC-001", line, f"{name}() cannot bound the number of bytes it writes", fn, "high", UNSAFE[name])
        if name in SCANF:
            if any(x.kind == "str" and re.search(r"%s", x.text) for a, b in args[:2] for x in t[a:b]):
                c.add("CR-SEC-001", line, f"{name}(\"%s\") reads an unbounded string; give a field width", fn, "high")
        if name in FORMAT_ARG and len(args) > FORMAT_ARG[name]:
            a, b = args[FORMAT_ARG[name]]
            toks = t[a:b]
            if toks and not all(x.kind == "str" for x in toks):
                macro = len(toks) == 1 and toks[0].text.isupper()
                c.add("CR-SEC-002", line, f"format argument of {name}() is not a string literal", fn, "low" if macro else "high")
        if name in EXEC and args:
            a, b = args[0]
            if not all(x.kind == "str" for x in t[a:b]):
                c.add("CR-SEC-003", line, f"{name}() is called with a non-constant command", fn, "high")
        if name in COPY_LEN and len(args) > COPY_LEN[name]:
            a, b = args[COPY_LEN[name]]
            ids = [x.text for x in t[a:b] if x.kind == "id" and x.text not in KEYWORDS and not x.text.isupper()]
            has_sizeof = any(x.text == "sizeof" for x in t[a:b])
            only_const = all(x.kind == "num" or x.text.isupper() or x.kind == "op" for x in t[a:b] if x.kind != "id" or x.text.isupper())
            if ids and not has_sizeof and not any(guard.get(v, 10 ** 9) < i for v in ids):
                c.add("CR-SEC-004", line, f"length '{' '.join(c.txt(a, b))}' passed to {name}() is not bounded by an earlier check", fn, "medium")
        if name in ALLOC and args:
            a, b = args[-1]
            ids = [x.text for x in t[a:b] if x.kind == "id" and x.text != "sizeof" and not x.text.isupper() and x.text not in KEYWORDS]
            if any(x.text == "*" for x in t[a:b]) and ids and not any(guard.get(v, 10 ** 9) < i for v in ids):
                c.add("CR-SEC-005", line, f"size '{' '.join(c.txt(a, b))}' passed to {name}() may overflow", fn, "medium")
            c.add("CR-MSR-2103", line, f"{name}() allocates dynamic memory", fn, "high")
        if name == "free":
            c.add("CR-MSR-2103", line, "free() releases dynamic memory", fn, "high")
        if name in RAND:
            c.add("CR-SEC-009", line, f"{name}() is predictable", fn, "medium" if SEC_WORDS.search(fn) or any(SEC_WORDS.search(x) for x in idents_in_fn) else "low")
        if name == "alloca":
            c.add("CR-SEC-014", line, "alloca() allocates a runtime-sized block on the stack", fn, "high")
        if name in ("setjmp", "longjmp"):
            c.add("CR-MSR-2104", line, f"{name}() performs a non-local jump", fn, "high")
        if name in STDIO:
            c.add("CR-MSR-2106", line, f"{name}() is a standard I/O function", fn, "high")
        if name in ("atoi", "atof", "atol", "atoll"):
            c.add("CR-MSR-2107", line, f"{name}() gives no error indication", fn, "high")
        if name in ("abort", "exit", "getenv", "system"):
            c.add("CR-MSR-2108", line, f"{name}() is a process-level function", fn, "high")
        # unused results
        if stmt_start and close + 1 < len(t) and t[close + 1].text == ";" and not _void_cast(c, i):
            if name in CRITICAL_RET:
                c.add("CR-SEC-011", line, f"result of {name}() is ignored", fn, "high")
            elif name in c.user_funcs and not c.user_funcs[name]:
                c.add("CR-MSR-1707", line, f"result of {name}() is ignored", fn, "high")
    # ---- unchecked allocation results, use after free
    for i, name, args, close in calls:
        if name in ("malloc", "calloc", "realloc", "strdup") and i >= 2 and t[i - 1].text == "=" and t[i - 2].kind == "id":
            _check_alloc(c, f, t[i - 2].text, close, name, t[i].line)
        if name == "free" and args:
            a, b = args[0]
            if b - a == 1 and t[a].kind == "id":
                _check_uaf(c, f, t[a].text, close)
    # ---- token walk: control flow rules
    depth_after_ret: list[int] = []
    for i in range(lo + 1, hi):
        x = t[i]
        if x.kind == "num" and OCTAL.match(x.text):
            c.add("CR-MSR-0701", x.line, f"octal constant {x.text}", fn, "high")
        if x.text == "goto":
            c.add("CR-MSR-1501", x.line, "goto statement", fn, "high")
        if x.text in ("if", "while", "for") and i + 1 < hi and t[i + 1].text == "(" and (i + 1) in p:
            close = p[i + 1]
            _check_condition_assign(c, i + 1, close, fn)
            nxt = t[close + 1].text if close + 1 < len(t) else ""
            if nxt != "{" and not (x.text == "while" and nxt == ";" and _is_do_while_tail(c, i)):
                c.add("CR-MSR-1506", x.line, f"body of '{x.text}' is not a compound statement", fn, "high")
        if x.text == "else" and i + 1 < hi and t[i + 1].text not in ("{", "if"):
            c.add("CR-MSR-1506", x.line, "body of 'else' is not a compound statement", fn, "high")
        if x.text == "do" and i + 1 < hi and t[i + 1].text != "{":
            c.add("CR-MSR-1506", x.line, "body of 'do' is not a compound statement", fn, "high")
        if x.text == "switch" and i + 1 < hi and t[i + 1].text == "(" and (i + 1) in p:
            _check_switch(c, p[i + 1] + 1, fn)
        if x.text in ("return", "break", "continue", "goto"):
            end = _stmt_end(c, i, hi)
            if end and end + 1 < hi:
                nx = t[end + 1]
                label = nx.kind == "id" and end + 2 < hi and t[end + 2].text == ":" and t[end + 3 if end + 3 < hi else end + 2].text != ":"
                if nx.text not in ("}", "case", "default", "else") and not label and not (x.text != "return" and nx.text == "while"):
                    c.add("CR-MSR-0201", nx.line, f"statement after '{x.text}' can never execute", fn, "medium")
    # ---- other statement-level checks
    _check_secrets(c, f)
    _check_offbyone(c, f)
    basic = [x for x in t[lo:hi] if x.text in ("int", "short", "long", "unsigned", "signed")]
    basic += [x for x in t[f.lo - 40 if f.lo > 40 else 0:f.lo] if False]
    if basic:
        c.add("CR-MSR-D406", basic[0].line, f"{len(basic)} use(s) of basic numeric types in {fn}()", fn, "medium")
    # ---- metrics
    cc = 1 + sum(1 for x in t[lo:hi] if x.text in ("if", "for", "while", "case", "&&", "||", "?") and x.kind in ("id", "op"))
    maxd = d = 0
    for x in t[lo:hi + 1]:
        if x.kind == "op" and x.text == "{":
            d += 1
            maxd = max(maxd, d)
        elif x.kind == "op" and x.text == "}":
            d -= 1
    m = {"name": fn, "file": c.path, "line": f.line, "loc": f.end_line - f.line + 1, "complexity": cc, "nesting": max(0, maxd - 1),
         "params": len(f.params)}
    if cc > cfg["max_complexity"]:
        c.add("CR-QLT-001", f.line, f"{fn}() has cyclomatic complexity {cc} (limit {cfg['max_complexity']})", fn, "high",
              "error" if cc > cfg["error_complexity"] else "warning")
    if m["loc"] > cfg["max_length"]:
        c.add("CR-QLT-002", f.line, f"{fn}() is {m['loc']} lines long (limit {cfg['max_length']})", fn, "high")
    if m["nesting"] > cfg["max_nesting"]:
        c.add("CR-QLT-003", f.line, f"{fn}() nests blocks {m['nesting']} levels deep (limit {cfg['max_nesting']})", fn, "high")
    if m["params"] > cfg["max_params"]:
        c.add("CR-QLT-004", f.line, f"{fn}() takes {m['params']} parameters (limit {cfg['max_params']})", fn, "high")
    m["callees"] = sorted({n for _, n, _, _ in calls if n in c.user_funcs})
    return m


def _stmt_start(c: Ctx, i: int) -> bool:
    t = c.t
    prev = t[i - 1]
    if prev.text in (";", "{", "}", "else", ":"):
        return True
    if prev.text == ")" and (i - 1) in c.L.pairs:
        o = c.L.pairs[i - 1]
        return o > 0 and t[o - 1].text in ("if", "while", "for", "switch")
    return False


def _void_cast(c: Ctx, i: int) -> bool:
    t = c.t
    return i >= 3 and t[i - 1].text == ")" and t[i - 2].text == "void" and t[i - 3].text == "("


def _stmt_end(c: Ctx, i: int, hi: int) -> int | None:
    t, d = c.t, 0
    for k in range(i, hi):
        if t[k].text in "([{" and t[k].kind == "op":
            d += 1
        elif t[k].text in ")]}" and t[k].kind == "op":
            d -= 1
            if d < 0:
                return None
        elif t[k].text == ";" and d == 0:
            return k
    return None


def _is_do_while_tail(c: Ctx, i: int) -> bool:
    t = c.t
    if i > 0 and t[i - 1].text == "}" and (i - 1) in c.L.pairs:
        o = c.L.pairs[i - 1]
        return o > 0 and t[o - 1].text == "do"
    return False


def _check_condition_assign(c: Ctx, open_i: int, close_i: int, fn: str):
    t, d = c.t, 0
    for k in range(open_i, close_i + 1):
        x = t[k]
        if x.kind == "op" and x.text == "(":
            d += 1
        elif x.kind == "op" and x.text == ")":
            d -= 1
        elif x.kind == "op" and x.text == "=" and t[open_i - 1].text != "for":
            c.add("CR-MSR-1304", x.line, "assignment inside a condition", fn, "high" if d == 1 else "low", "warning" if d == 1 else "info")
        elif x.kind == "op" and x.text == ";" and t[open_i - 1].text == "for":
            return


def _check_switch(c: Ctx, brace_i: int, fn: str):
    t, p = c.t, c.L.pairs
    if brace_i >= len(t) or t[brace_i].text != "{" or brace_i not in p:
        return
    end, labels, d = p[brace_i], [], 0
    for k in range(brace_i + 1, end):
        if t[k].text == "{" and t[k].kind == "op":
            d += 1
        elif t[k].text == "}" and t[k].kind == "op":
            d -= 1
        elif d == 0 and t[k].text in ("case", "default"):
            labels.append(k)
    sw_line = t[brace_i].line
    if not any(t[k].text == "default" for k in labels):
        c.add("CR-MSR-1604", sw_line, "switch has no default clause", fn, "high")
    for n_, k in enumerate(labels):
        nxt = labels[n_ + 1] if n_ + 1 < len(labels) else end
        colon = next((j for j in range(k, nxt) if t[j].text == ":" and t[j].kind == "op"), None)
        if colon is None:
            continue
        seg = [x for x in t[colon + 1:nxt]]
        body = [x for x in seg if x.text not in ("{", "}")]
        if not body or n_ + 1 >= len(labels):          # empty clause (grouped labels) or last clause
            continue
        tail = [x.text for x in body[-8:]]
        if not any(w in tail for w in ("break", "return", "goto", "continue")):
            lines = range(seg[-1].line, t[nxt].line + 1) if seg else []
            if not any("fall" in c.L.comments.get(ln, "").lower() for ln in lines):
                c.add("CR-MSR-1603", t[k].line, "case falls through into the next clause", fn, "medium")


def _check_alloc(c: Ctx, f: Func, var: str, after: int, alloc: str, line: int):
    t, p = c.t, c.L.pairs
    checked_at = None
    for a, b in c.cond_ranges(after, min(f.hi, after + 80)):
        if any(t[k].text == var for k in range(a, b)):
            checked_at = a
            break
    stop = min(f.hi, after + 80)
    for k in range(after + 1, stop):
        if t[k].text == var and t[k].kind == "id":
            if checked_at is not None and k > checked_at:
                return
            prev, nxt = t[k - 1].text, t[k + 1].text if k + 1 < len(t) else ""
            if nxt == "=":
                return
            if prev == "return":
                return
            if nxt in ("[", "->") or prev == "*" or (prev in ("(", ",") and nxt in (")", ",")):
                c.add("CR-SEC-006", line, f"result of {alloc}() is used at line {t[k].line} without a NULL check", f.name, "medium")
            return


def _check_uaf(c: Ctx, f: Func, var: str, after: int):
    t = c.t
    depth = 0
    for k in range(after + 1, f.hi):
        x = t[k]
        if x.kind == "op" and x.text == "{":
            depth += 1
        elif x.kind == "op" and x.text == "}":
            depth -= 1
            if depth < 0:
                return
        elif x.text in ("return", "goto", "break", "continue") and depth == 0:
            return
        elif x.kind == "id" and x.text == var and not (t[k - 1].text in (".", "->")):
            nxt = t[k + 1].text if k + 1 < len(t) else ""
            if nxt == "=" and t[k + 1].kind == "op":
                return
            if t[k - 1].text == "(" and k >= 2 and t[k - 2].text == "free":
                c.add("CR-SEC-007", x.line, f"'{var}' is freed twice", f.name, "high")
                return
            if nxt in ("[", "->", ".") or t[k - 1].text == "*" or t[k - 1].text in ("(", ",", "="):
                c.add("CR-SEC-007", x.line, f"'{var}' is used after free() at line {t[after].line}", f.name, "high")
                return


def _check_secrets(c: Ctx, f: Func):
    t = c.t
    for i in range(f.lo, f.hi):
        if t[i].kind != "id" or not SECRET_NAME.search(t[i].text) or SECRET_EXCLUDE.search(t[i].text):
            continue
        j = i + 1
        if j < len(t) and t[j].text == "[" and j in c.L.pairs:
            j = c.L.pairs[j] + 1
        if j < len(t) and t[j].text == "=" and j + 1 < len(t):
            r = t[j + 1]
            if r.kind == "str" and len(r.text) > 4 and r.text.strip('"') not in ("", "***", "changeme"):
                c.add("CR-SEC-008", t[i].line, f"'{t[i].text}' is initialised with a string literal", f.name, "high")
            elif r.text == "{" and (j + 1) in c.L.pairs:
                nums = [x for x in t[j + 2:c.L.pairs[j + 1]] if x.kind == "num"]
                if len(nums) >= 8:
                    c.add("CR-SEC-008", t[i].line, f"'{t[i].text}' is initialised with {len(nums)} constant bytes", f.name, "medium")


def _check_offbyone(c: Ctx, f: Func):
    t, p = c.t, c.L.pairs
    for i in range(f.lo, f.hi):
        if t[i].text == "for" and i + 1 < len(t) and t[i + 1].text == "(" and (i + 1) in p:
            close = p[i + 1]
            semi = [k for k in range(i + 2, close) if t[k].text == ";" and t[k].kind == "op"]
            if len(semi) < 2:
                continue
            cond = t[semi[0] + 1:semi[1]]
            if len(cond) >= 3 and cond[0].kind == "id" and cond[1].text == "<=":
                var, rhs = cond[0].text, " ".join(x.text for x in cond[2:])
                if re.search(r"(SIZE|LEN|LENGTH|COUNT|MAX|NUM)\b|sizeof|_N\b", rhs):
                    bs = close + 1
                    be = p[bs] if t[bs].text == "{" and bs in p else _stmt_end(c, bs, f.hi) or bs
                    if any(t[k].text == "[" and k + 2 <= be and t[k + 1].text == var and t[k + 2].text == "]" for k in range(bs, be)):
                        c.add("CR-SEC-010", t[i].line, f"loop runs while {var} <= {rhs} and indexes with {var}", f.name, "medium")


# ---------------------------------------------------------------- file/scan level
def analyze(files: dict[str, str], cfg: dict | None = None) -> tuple[list[dict], list[dict]]:
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    ctxs = {}
    user: dict[str, bool] = {}
    for path, src in files.items():
        c = Ctx(path, src, user)
        ctxs[path] = c
        for f in c.L.funcs:
            user[f.name] = f.returns_void
    metrics = []
    for c in ctxs.values():
        for f in c.L.funcs:
            metrics.append(check_function(c, f, cfg))
        for line, text in c.L.pp:                                    # #define SECRET "literal"
            m = re.match(r"#\s*define\s+(\w+)\s+\"([^\"]{4,})\"", text)
            if m and SECRET_NAME.search(m.group(1)) and not SECRET_EXCLUDE.search(m.group(1)):
                c.add("CR-SEC-008", line, f"macro '{m.group(1)}' holds a string literal secret", None, "high")
            for m in re.finditer(r"\b(0[0-7]+)\b", text if text.startswith("#define") or text.startswith("# define") else ""):
                c.add("CR-MSR-0701", line, f"octal constant {m.group(1)} in a macro", None, "medium")
    _recursion(ctxs, metrics)
    findings = [f for c in ctxs.values() for f in c.raw]
    return _dedupe_and_fingerprint(findings), metrics


def _recursion(ctxs: dict[str, Ctx], metrics: list[dict]):
    graph = {m["name"]: [x for x in m["callees"]] for m in metrics}
    where = {m["name"]: (m["file"], m["line"]) for m in metrics}
    index, low, stack, on, sccs, counter = {}, {}, [], set(), [], [0]

    def strong(v):
        index[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); on.add(v)
        for w in graph.get(v, []):
            if w not in index:
                strong(w); low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); on.discard(w); comp.append(w)
                if w == v:
                    break
            sccs.append(comp)
    for v in graph:
        if v not in index:
            strong(v)
    for comp in sccs:
        if len(comp) > 1 or comp[0] in graph.get(comp[0], []):
            for v in comp:
                path, line = where[v]
                kind = "directly recursive" if comp == [v] else "part of the recursion cycle " + " -> ".join(sorted(comp))
                ctxs[path].add("CR-MSR-1702", line, f"{v}() is {kind}", v, "high")


def _dedupe_and_fingerprint(findings: list[dict]) -> list[dict]:
    seen, out, occ = set(), [], {}
    for f in sorted(findings, key=lambda x: (x["file"], x["line"], x["rule"])):
        k = (f["rule"], f["file"], f["line"], f["message"])
        if k in seen:
            continue
        seen.add(k)
        base = (f["tool"], f["rule"], f["file"], f["function"], f["snippet"])
        occ[base] = occ.get(base, 0) + 1
        f["fingerprint"] = fingerprint(*base, occ[base])
        out.append(f)
    return out


def fingerprint(tool, rule, file, function, snippet, n) -> str:
    return hashlib.sha1(f"{tool}|{rule}|{file}|{function}|{snippet}|{n}".encode()).hexdigest()[:16]
