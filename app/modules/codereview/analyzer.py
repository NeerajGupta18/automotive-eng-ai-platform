"""Lightweight, dependency-free C analyzer ("MISRA-style" and security checks).

Honest scope: this is a pattern/heuristic analyzer over a comment- and string-masked token stream with brace
matching, NOT a compiler front end. It has no type information and no data-flow engine. Rules are original
descriptions mapped to public identifiers (CWE, CERT C) plus informational MISRA C:2012 numbers that must be
checked against your licensed copy. It complements, and does not replace, a certified static analyzer.

Every finding carries a stable fingerprint (rule + file + function + normalised line text) so that triage
decisions survive line shifts and revisions."""
from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field

SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def R(title, sev, cat, cwe, why, fix, conf="high", cert=None, misra=None):
    return {"title": title, "severity": sev, "category": cat, "cwe": cwe, "cert": cert, "misra": misra,
            "why": why, "fix": fix, "confidence": conf}


RULES = {
    "CR001": R("Use of gets()", "critical", "security", "CWE-242", "gets() cannot limit input length and always allows a buffer overflow.", "Use fgets(buf, sizeof buf, stdin) and handle the trailing newline.", cert="MSC24-C"),
    "CR002": R("Unbounded string function", "high", "security", "CWE-120", "strcpy/strcat/sprintf do not check the destination size.", "Use snprintf or a bounded copy with an explicit size check; verify truncation.", cert="STR31-C"),
    "CR003": R("scanf %s without field width", "high", "security", "CWE-120", "An unbounded %s conversion lets input overflow the destination.", "Add a width (e.g. %31s for a 32-byte buffer) or use fgets.", cert="STR31-C"),
    "CR004": R("Non-literal format string", "high", "security", "CWE-134", "A format string that is not a literal can be attacker-controlled (%n, %s leaks).", "Pass a literal format: printf(\"%s\", text).", cert="FIO30-C"),
    "CR005": R("Shell command execution", "high", "security", "CWE-78", "system()/popen() start a shell; unsanitised input leads to command injection.", "Avoid the shell; use a fixed argument vector with strict validation, or remove the call.", cert="ENV33-C"),
    "CR006": R("Restricted library function", "medium", "safety", "CWE-20", "atoi/atof/exit/abort/getenv/setjmp/longjmp hide errors or break control flow.", "Use strtol with error checks; avoid termination and non-local jumps in embedded code.", cert="ERR34-C", misra="21.7 / 21.8 / 21.4"),
    "CR007": R("Unchecked allocation or open result", "medium", "robustness", "CWE-476", "The returned pointer may be NULL and is used without a check.", "Check the result before use and handle the failure path.", "medium", cert="MEM32-C"),
    "CR008": R("Dynamic memory allocation", "low", "safety", "CWE-401", "Heap use in embedded/safety code risks fragmentation, leaks and non-determinism.", "Prefer static allocation or a qualified memory pool.", cert="MEM00-C", misra="21.3"),
    "CR009": R("Use after free / double free", "high", "security", "CWE-416", "Memory is used or freed again after free().", "Set the pointer to NULL after free and restructure ownership; never reuse freed memory.", "medium", cert="MEM30-C"),
    "CR010": R("Hard-coded credential or key", "high", "security", "CWE-798", "Secrets in source are exposed to everyone with the binary or repository.", "Load secrets from protected storage (HSM/secure element); rotate the exposed value.", "medium", cert="MSC41-C"),
    "CR011": R("Possible integer overflow in allocation size", "medium", "security", "CWE-190", "A multiplied size can wrap around and produce a too-small buffer.", "Check operands against SIZE_MAX / count limits before multiplying, or use calloc.", "medium", cert="MEM35-C"),
    "CR012": R("Off-by-one loop bound", "high", "security", "CWE-193", "The loop runs one element past the array (<= instead of <).", "Use i < N (or i < sizeof(a)/sizeof(a[0])).", "medium", cert="ARR30-C"),
    "CR013": R("Wrong size argument", "medium", "security", "CWE-467", "sizeof of a pointer, or a size derived from the source, does not bound the destination.", "Use the destination array size, and check that source length fits.", "medium", cert="ARR38-C"),
    "CR014": R("Possibly uninitialised variable", "medium", "robustness", "CWE-457", "The variable is read before any visible assignment.", "Initialise at declaration.", "low", cert="EXP33-C", misra="9.1"),
    "CR015": R("Time-of-check to time-of-use", "medium", "security", "CWE-367", "access() followed by open() is racy; the file can change in between.", "Open the file directly and check the result of open().", "medium", cert="FIO45-C"),
    "CR016": R("Insecure temporary file or weak random", "medium", "security", "CWE-377", "tmpnam/mktemp/tempnam are predictable; rand() is not a secure random source.", "Use mkstemp; use a cryptographically secure generator for security purposes.", cert="CON33-C / MSC30-C"),
    "CR020": R("goto statement", "low", "style", "CWE-1119", "goto obscures control flow.", "Restructure with loops/early returns.", misra="15.1"),
    "CR021": R("Direct recursion", "medium", "safety", "CWE-674", "Recursion makes stack usage unbounded, which is unacceptable in most embedded safety code.", "Replace by iteration or prove a hard depth bound.", misra="17.2"),
    "CR022": R("switch without default", "medium", "safety", "CWE-478", "Unexpected values fall through silently.", "Add a default clause (with defensive handling).", misra="16.4"),
    "CR023": R("Implicit switch fall-through", "medium", "safety", "CWE-484", "A case falls through to the next without break and without a comment.", "Add break, or an explicit fallthrough comment if intended.", "medium", misra="16.3"),
    "CR024": R("Assignment in condition", "medium", "safety", "CWE-481", "= inside a condition is often a typo for ==.", "Separate the assignment, or compare explicitly.", cert="EXP45-C", misra="13.4"),
    "CR025": R("Control statement without braces", "low", "style", "CWE-483", "Missing braces cause bugs when statements are added later.", "Always use braces.", misra="15.6"),
    "CR026": R("Unreachable code", "low", "safety", "CWE-561", "Code after return/break/continue/goto never executes.", "Remove it or fix the control flow.", "medium", misra="2.1"),
    "CR027": R("Shared variable written in ISR is not volatile", "high", "safety", "CWE-362", "Data shared with an interrupt handler can be cached by the compiler and read inconsistently.", "Declare it volatile and protect multi-byte access with a critical section or atomics.", "medium"),
    "CR028": R("Excessive complexity or length", "medium", "maintainability", "CWE-1121", "Very complex functions are hard to review and test.", "Split the function; aim for cyclomatic complexity <= 15.", misra=None),
    "CR029": R("Instruction-like text in a comment", "medium", "integrity", "CWE-1007", "Comments that address an AI/automated reviewer may be an attempt to manipulate review tools.", "Remove the comment and check the change history.", "medium"),
}

KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "else", "do", "case", "defined"}
_TOKENS = re.compile(r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.S)
_PP = re.compile(r"^[ \t]*#(?:[^\n\\]|\\.)*", re.M | re.S)


def norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


@dataclass
class Func:
    name: str
    params: str
    open: int
    close: int
    start_line: int
    end_line: int
    body: str
    head: str


class Ctx:
    def __init__(self, name: str, text: str):
        self.name, self.text = name, text.replace("\r\n", "\n").replace("\r", "\n")
        text = self.text
        self.lines = text.split("\n")
        self.starts = [0]
        for m in re.finditer("\n", text):
            self.starts.append(m.end())
        self.comments: list[tuple[int, str]] = []

        def repl(m):
            s = m.group(0)
            if s[0] == "/":
                self.comments.append((self.line(m.start()), s))
                return re.sub(r"[^\n]", " ", s)
            return s[0] + re.sub(r"[^\n]", " ", s[1:-1]) + s[-1] if len(s) >= 2 else s
        self.masked = _TOKENS.sub(repl, text)
        # comments blanked, string literals kept: the basis of stable fingerprints
        self.nclines = _TOKENS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)) if m.group(0)[0] == "/" else m.group(0), text).split("\n")
        self.code = _PP.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), self.masked)
        self.consts = self._consts()
        self.funcs = self._functions()
        self.top = self.code
        for f in self.funcs:
            self.top = self.top[:f.open] + re.sub(r"[^\n]", " ", self.top[f.open:f.close + 1]) + self.top[f.close + 1:]
        self.arrays = self._arrays()

    # ---------------------------------------------------------------- helpers
    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)

    def func_at(self, pos: int) -> Func | None:
        return next((f for f in self.funcs if f.open <= pos <= f.close), None)

    def in_code(self, pos: int) -> bool:
        return self.code[pos] == self.text[pos] or self.text[pos].isspace()

    def spans(self, open_idx: int) -> tuple[list[tuple[int, int]], int]:
        """Argument spans of the call whose '(' is at open_idx, and the index of the closing ')'."""
        depth, i, start, out = 0, open_idx, open_idx + 1, []
        c = self.code
        while i < len(c):
            ch = c[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    if c[start:i].strip():
                        out.append((start, i))
                    return out, i
            elif ch == "," and depth == 1:
                out.append((start, i))
                start = i + 1
            i += 1
        return out, len(c) - 1

    def calls(self, names, scope: Func | None = None):
        pat = re.compile(r"(?<![\w.>])(" + "|".join(map(re.escape, names)) + r")\s*\(")
        lo, hi = (scope.open, scope.close) if scope else (0, len(self.code))
        for m in pat.finditer(self.code, lo, hi):
            sp, end = self.spans(m.end() - 1)
            yield {"name": m.group(1), "pos": m.start(), "end": end + 1, "line": self.line(m.start()), "spans": sp,
                   "args": [self.code[a:b].strip() for a, b in sp], "raw": [self.text[a:b].strip() for a, b in sp]}

    def _consts(self) -> dict:
        c = {}
        for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]+\(?\s*(\d+)[uUlL]*\s*\)?[ \t]*(?://.*|/\*.*)?$", self.text, re.M):
            c[m.group(1)] = int(m.group(2))
        for m in re.finditer(r"\bconst\b[^;=()]*?\b(\w+)\s*=\s*(\d+)[uUlL]*\s*;", self.code):
            c[m.group(1)] = int(m.group(2))
        return c

    def val(self, expr: str) -> int | None:
        e = expr.strip().strip("()").strip()
        e = re.sub(r"(?<=\d)[uUlL]+$", "", e)
        return int(e) if e.isdigit() else self.consts.get(e)

    def _arrays(self) -> dict:
        out = {}
        for m in re.finditer(r"\b(?:char|uint8_t|int8_t|uint16_t|int16_t|uint32_t|int32_t|int|short|long|unsigned|float|double|bool|size_t)\s+(?:\*\s*)?(\w+)\s*\[\s*([^\]]*?)\s*\]", self.code):
            out.setdefault(m.group(1), (m.group(2), self.val(m.group(2))))
        return out

    def _functions(self) -> list[Func]:
        code, funcs, depth, i, n = self.code, [], 0, 0, len(self.code)
        while i < n:
            c = code[i]
            if c == "{":
                head = code[:i].rstrip()
                if depth == 0 and head.endswith(")"):
                    j, d = len(head) - 1, 0
                    while j >= 0:
                        d += head[j] == ")"
                        d -= head[j] == "("
                        if d == 0:
                            break
                        j -= 1
                    pre = head[:j].rstrip()
                    m = re.search(r"([A-Za-z_]\w*)$", pre)
                    if m and m.group(1) not in KEYWORDS and not pre[:m.start()].rstrip().endswith(("=", "struct", "enum", "union")):
                        d, k = 0, i
                        while k < n:
                            d += code[k] == "{"
                            d -= code[k] == "}"
                            if d == 0:
                                break
                            k += 1
                        funcs.append(Func(m.group(1), head[j + 1:-1], i, k, self.line(m.start(1)), self.line(k), code[i + 1:k], pre))
                        i = k + 1
                        continue
                depth += 1
            elif c == "}":
                depth = max(0, depth - 1)
            i += 1
        return funcs

    def array_len(self, name: str) -> int | None:
        return self.arrays.get(name, (None, None))[1]

    def block_end(self, open_idx: int) -> int:
        d, k = 0, open_idx
        while k < len(self.code):
            d += self.code[k] == "{"
            d -= self.code[k] == "}"
            if d == 0:
                return k
            k += 1
        return len(self.code) - 1


# ================================================================== rules
def _add(out, rule, ctx, pos, msg, **extra):
    fn = ctx.func_at(pos)
    out.append({"rule": rule, "line": ctx.line(pos), "message": msg, "function": fn.name if fn else "", "pos": pos, **extra})


def r_gets(ctx, out):
    for c in ctx.calls(["gets"]):
        _add(out, "CR001", ctx, c["pos"], "gets() reads unbounded input", call=c)


UNBOUNDED = {"strcpy", "strcat", "sprintf", "vsprintf"}


def r_unbounded(ctx, out):
    for c in ctx.calls(UNBOUNDED):
        a, raw = c["args"], c["raw"]
        if c["name"] in ("strcpy", "strcat") and len(a) == 2 and raw[1].startswith('"'):
            size = ctx.array_len(a[0])
            if size and len(raw[1]) - 2 < size and c["name"] == "strcpy":       # literal provably fits
                continue
        _add(out, "CR002", ctx, c["pos"], f"{c['name']}() does not check the destination size", call=c)


def r_scanf(ctx, out):
    for c in ctx.calls(["scanf", "sscanf", "fscanf"]):
        idx = 0 if c["name"] == "scanf" else 1
        if len(c["raw"]) > idx and re.search(r"%\*?(?:\d*)s", c["raw"][idx]) and re.search(r"%(?!\*)s", c["raw"][idx]):
            _add(out, "CR003", ctx, c["pos"], f"{c['name']} with %s and no field width")


FMT = {"printf": 0, "vprintf": 0, "fprintf": 1, "sprintf": 1, "vsprintf": 1, "syslog": 1, "snprintf": 2, "vsnprintf": 2, "dprintf": 1}


def r_format(ctx, out):
    for c in ctx.calls(FMT):
        i = FMT[c["name"]]
        if len(c["args"]) > i:
            f = c["args"][i]
            if not f.startswith('"') and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", f):
                _add(out, "CR004", ctx, c["pos"], f"{c['name']}() format argument '{f[:40]}' is not a string literal", call=c, fmt_index=i)


def r_shell(ctx, out):
    for c in ctx.calls(["system", "popen"]):
        _add(out, "CR005", ctx, c["pos"], f"{c['name']}() starts a shell")


RESTRICTED = {"atoi": "no error detection", "atol": "no error detection", "atoll": "no error detection", "atof": "no error detection",
              "exit": "terminates the program", "abort": "terminates the program", "getenv": "environment input is untrusted",
              "setjmp": "non-local jump", "longjmp": "non-local jump"}


def r_restricted(ctx, out):
    for c in ctx.calls(RESTRICTED):
        _add(out, "CR006", ctx, c["pos"], f"{c['name']}(): {RESTRICTED[c['name']]}")


def r_alloc(ctx, out):
    for f in ctx.funcs:
        dyn = list(ctx.calls(["malloc", "calloc", "realloc", "free"], f))
        if dyn:
            _add(out, "CR008", ctx, dyn[0]["pos"], f"{len(dyn)} dynamic memory call(s) in {f.name}()")
        for m in re.finditer(r"\b(\w+)\s*=\s*(?:\([^)=]*\)\s*)?(malloc|calloc|realloc|fopen)\s*\(", f.body):
            v, pos = m.group(1), f.open + 1 + m.start()
            tail = ctx.code[f.open + 1 + m.end(): f.close]
            tail = "\n".join(tail.split("\n")[:12])
            chk = re.search(rf"(!\s*{v}\b|\b{v}\s*[!=]=\s*(NULL|0)|\b(NULL|0)\s*[!=]=\s*{v}\b|\bif\s*\(\s*{v}\s*\)|assert\s*\(\s*{v}\b|\b{v}\s*&&|\b{v}\s*\?)", tail)
            if not chk:
                _add(out, "CR007", ctx, pos, f"Result of {m.group(2)}() stored in '{v}' is used without a NULL check", var=v, alloc=m.group(2))
        for c in ctx.calls(["malloc", "realloc"], f):
            e = c["args"][-1] if c["args"] else ""
            if "*" in e and re.search(r"[A-Za-z_]\w*", re.sub(r"sizeof\s*\([^)]*\)", "", e)):
                if not re.search(r"SIZE_MAX|UINT_MAX|overflow|>\s*\w*MAX", f.body):
                    _add(out, "CR011", ctx, c["pos"], f"Allocation size '{e[:40]}' is a product of runtime values")


def _block_tail(code: str, start: int, end: int) -> str:
    """Text after `start` up to the end of the enclosing block or the first unconditional exit."""
    d, i = 0, start
    while i < end:
        ch = code[i]
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
            if d < 0:
                return code[start:i]
        elif d == 0 and re.match(r"(return|break|continue|goto)\b", code[i:i + 9]):
            return code[start:i]
        i += 1
    return code[start:end]


def r_uaf(ctx, out):
    for f in ctx.funcs:
        for c in ctx.calls(["free"], f):
            if len(c["args"]) != 1 or not re.fullmatch(r"\w+", c["args"][0]):
                continue
            v, tail0 = c["args"][0], c["end"]
            tail = _block_tail(ctx.code, tail0, f.close)
            asg = re.search(rf"\b{v}\s*=(?!=)", tail)
            if asg:
                tail = tail[:asg.start()]
            free_re = rf"(?<![\w.>])free\s*\(\s*{v}\s*\)"
            m = re.search(free_re, tail)
            if m:
                _add(out, "CR009", ctx, tail0 + m.start(), f"'{v}' is freed twice", kind="double-free", var=v, cwe="CWE-415")
            blanked = re.sub(free_re, lambda x: " " * len(x.group(0)), tail)
            m = re.search(rf"(?<![\w.])\b{v}\b", blanked)
            if m:
                _add(out, "CR009", ctx, tail0 + m.start(), f"'{v}' is used after free()", kind="use-after-free", var=v)


CRED = re.compile(r"\b(\w*(?:passw(?:or)?d|pwd|secret|api_?key|token|private_?key|credential|auth_?key)\w*)\b[^;=\n]*=\s*\"([^\"\n]{4,})\"", re.I)
KEYARR = re.compile(r"\b(\w*(?:key|secret|iv)\w*)\s*\[[^\]]*\]\s*=\s*\{\s*(?:0x[0-9a-fA-F]{2}|\d+)\s*,", re.I)


def r_creds(ctx, out):
    for m in CRED.finditer(ctx.text):
        if ctx.in_code(m.start()) and not re.search(r"(?i)^(null|none|changeme|\*+|%s)$", m.group(2)):
            _add(out, "CR010", ctx, m.start(), f"Hard-coded secret in '{m.group(1)}'", lit=(m.start(2), m.end(2)))
    for m in KEYARR.finditer(ctx.text):
        if ctx.in_code(m.start()):
            _add(out, "CR010", ctx, m.start(), f"Hard-coded key material in '{m.group(1)}'", cwe="CWE-321")


def r_offbyone(ctx, out):
    for m in re.finditer(r"\bfor\s*\(([^;]*);\s*(\w+)\s*<=\s*([^;]+);", ctx.code):
        var, bound = m.group(2), m.group(3).strip()
        o = ctx.code.index("(", m.start())
        _, close = ctx.spans(o)
        j = close + 1
        while ctx.code[j:j + 1].isspace():
            j += 1
        body = ctx.code[j:ctx.block_end(j) + 1] if ctx.code[j:j + 1] == "{" else ctx.code[j:ctx.code.find(";", j) + 1]
        s = re.fullmatch(r"sizeof\s*\(\s*(\w+)\s*\)\s*/\s*sizeof.*", bound, re.S)
        for am in re.finditer(rf"\b(\w+)\s*\[\s*{var}\s*\]", body):
            arr = am.group(1)
            hit = (s and s.group(1) == arr) or (ctx.array_len(arr) is not None and ctx.array_len(arr) == ctx.val(bound)) or \
                  (arr in ctx.arrays and ctx.arrays[arr][0] == bound)
            if hit:
                cmp_pos = m.start(2) + len(var) + ctx.code[m.start(2) + len(var):].index("<=")
                _add(out, "CR012", ctx, m.start(), f"Loop over '{arr}' uses <= {bound}; index {var} reaches the array size", cmp_pos=cmp_pos)
                break


def r_sizes(ctx, out):
    for f in ctx.funcs:
        ptrs = {m.group(1) for m in re.finditer(r"\*\s*(\w+)\s*(?:[,)]|=|;)", f.params + ";" + f.body)} - set(ctx.arrays)
        for c in ctx.calls(["memcpy", "memmove", "memset", "strncpy", "snprintf", "fgets", "read"], f):
            for arg in c["args"][1:]:
                m = re.fullmatch(r"sizeof\s*\(?\s*(\w+)\s*\)?", arg)
                if m and m.group(1) in ptrs:
                    _add(out, "CR013", ctx, c["pos"], f"sizeof('{m.group(1)}') is the size of a pointer, not of the object it points to")
                    break
            if c["name"] in ("memcpy", "memmove") and len(c["args"]) == 3 and re.search(rf"strlen\s*\(\s*(?:\([^)]*\)\s*)?{re.escape(c['args'][1])}\s*\)", c["args"][2]):
                _add(out, "CR013", ctx, c["pos"], "Copy size comes from strlen(source): destination size is not checked and no terminator is copied", cwe="CWE-120")


def r_uninit(ctx, out):
    for f in ctx.funcs:
        for m in re.finditer(r"(?m)^[ \t]*(?:unsigned\s+|signed\s+)*(?:int|long|short|char|float|double|bool|size_t|u?int(?:8|16|32|64)_t)\s+(\w+)\s*;", f.body):
            v = m.group(1)
            rest = f.body[m.end():]
            u = re.search(rf"(?<![\w.>])\b{v}\b", rest)
            if not u:
                continue
            after = rest[u.end():u.end() + 3]
            before = rest[max(0, u.start() - 1):u.start()]
            write = re.match(r"\s*(=(?!=)|\+\+|--)", after) or before == "&" or re.match(r"\s*\)?\s*,?", "") and False
            if not write:
                _add(out, "CR014", ctx, f.open + 1 + m.end() + u.start(), f"'{v}' may be read before it is assigned")


def r_toctou(ctx, out):
    for f in ctx.funcs:
        acc = list(ctx.calls(["access"], f))
        for a in acc:
            if a["args"] and any(o["args"] and o["args"][0] == a["args"][0] and o["pos"] > a["pos"] for o in ctx.calls(["open", "fopen"], f)):
                _add(out, "CR015", ctx, a["pos"], "access() check followed by open() on the same path")


def r_weak(ctx, out):
    for c in ctx.calls(["tmpnam", "mktemp", "tempnam"]):
        _add(out, "CR016", ctx, c["pos"], f"{c['name']}() creates predictable file names")
    for c in ctx.calls(["rand", "srand"]):
        _add(out, "CR016", ctx, c["pos"], f"{c['name']}() is not a secure random source", cwe="CWE-338")


def r_goto(ctx, out):
    for m in re.finditer(r"\bgoto\b", ctx.code):
        _add(out, "CR020", ctx, m.start(), "goto statement")


def r_recursion(ctx, out):
    for f in ctx.funcs:
        for c in ctx.calls([f.name], f):
            _add(out, "CR021", ctx, c["pos"], f"{f.name}() calls itself")
            break


def _switches(ctx):
    for m in re.finditer(r"\bswitch\s*\(", ctx.code):
        _, close = ctx.spans(m.end() - 1)
        j = close + 1
        while ctx.code[j:j + 1].isspace():
            j += 1
        if ctx.code[j:j + 1] == "{":
            yield m, j, ctx.block_end(j)


def _top(ctx, lo, hi):
    """Body of a block with nested braces blanked (so only depth-0 labels remain visible)."""
    s, d = list(ctx.code[lo + 1:hi]), 0
    for i, ch in enumerate(s):
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
        elif d > 0 and ch != "\n":
            s[i] = " "
    return "".join(s)


def r_switch(ctx, out):
    for m, o, c in _switches(ctx):
        top = _top(ctx, o, c)
        if not re.search(r"\bdefault\s*:", top):
            _add(out, "CR022", ctx, m.start(), "switch has no default clause", close=c, open=o)
        labels = [(x.start(), x.end()) for x in re.finditer(r"\b(?:case\b[^:;]*?|default)\s*:(?!:)", top)]
        for k in range(len(labels) - 1):
            seg = ctx.code[o + 1 + labels[k][1]: o + 1 + labels[k + 1][0]]
            if not seg.strip():
                continue
            raw = ctx.text[o + 1 + labels[k][1]: o + 1 + labels[k + 1][0]]
            ends = re.search(r"(break|continue|goto\s+\w+|return\b[^;]*)\s*;\s*\}?\s*$", seg.strip() + "") or re.search(r"\}\s*$", seg.strip()) and re.search(r"(break|return\b[^;]*|continue)\s*;\s*\}\s*$", seg.strip())
            if not ends and not re.search(r"fall\s*-?\s*through", raw, re.I):
                _add(out, "CR023", ctx, o + 1 + labels[k + 1][0], "Case falls through into the next label")


def r_cond_assign(ctx, out):
    for m in re.finditer(r"\b(if|while)\s*\(", ctx.code):
        _, close = ctx.spans(m.end() - 1)
        cond = ctx.code[m.end():close]
        if re.fullmatch(r"\s*\(.*\)\s*", cond, re.S):
            continue                                        # deliberate double parentheses
        d = 0
        for i, ch in enumerate(cond):
            d += ch == "("
            d -= ch == ")"
            if ch == "=" and d == 0 and not re.match(r"[=]", cond[i + 1:i + 2]) and not re.search(r"[=!<>+\-*/%&|^]$", cond[:i]):
                _add(out, "CR024", ctx, m.start(), f"Assignment inside '{m.group(1)}' condition")
                break


def r_braces(ctx, out):
    for m in re.finditer(r"\b(if|for|while)\s*\(", ctx.code):
        _, close = ctx.spans(m.end() - 1)
        j = close + 1
        while ctx.code[j:j + 1].isspace():
            j += 1
        nxt = ctx.code[j:j + 1]
        if nxt not in ("{", ";", ""):
            semi = ctx.code.find(";", j)
            same_line = semi != -1 and "\n" not in ctx.code[close + 1:semi].strip("\n ")
            _add(out, "CR025", ctx, m.start(), f"'{m.group(1)}' body is not enclosed in braces", stmt=(j, semi + 1) if same_line and "{" not in ctx.code[j:semi] else None)
    for m in re.finditer(r"\belse\b(?!\s*(?:if\b|\{))", ctx.code):
        _add(out, "CR025", ctx, m.start(), "'else' body is not enclosed in braces")


def r_unreachable(ctx, out):
    for f in ctx.funcs:
        for m in re.finditer(r"\b(return\b[^;]*|break|continue|goto\s+\w+)\s*;", f.body):
            rest = f.body[m.end():]
            n = re.match(r"\s*(\S+)", rest)
            if not n:
                continue
            tok = n.group(1)
            if tok[0] == "}" or re.match(r"(case|default|else)\b", rest.lstrip()) or re.match(r"\w+\s*:(?!:)", rest.lstrip()):
                continue
            _add(out, "CR026", ctx, f.open + 1 + m.end() + n.start(1), "Statement after return/break/continue/goto is unreachable")


def r_isr(ctx, out):
    isr = [f for f in ctx.funcs if re.search(r"(?i)(^|_)(isr|irq)(_|$)|_handler$|^isr$|__interrupt", f.name + " " + f.head)]
    decls = {m.group(2): (m, "volatile" in m.group(0)) for m in re.finditer(
        r"(?m)^[ \t]*((?:static\s+)?(?:volatile\s+)?(?:unsigned\s+|signed\s+)*(?:int|long|short|char|bool|float|double|size_t|\w+_t)\s+(\w+))\s*(?:=[^;]*)?;", ctx.top)}
    for f in isr:
        for g, (dm, vol) in decls.items():
            if vol:
                continue
            w = re.search(rf"(?<![\w.>])\b{g}\b\s*(=(?!=)|\+=|-=|\|=|&=|\+\+|--)|(\+\+|--)\s*{g}\b", f.body)
            if w and any(re.search(rf"\b{g}\b", o.body) for o in ctx.funcs if o not in isr):
                _add(out, "CR027", ctx, f.open + 1 + w.start(), f"'{g}' is written in {f.name}() and read elsewhere but is not volatile", var=g)


def r_complexity(ctx, out):
    for f in ctx.funcs:
        cc = 1 + len(re.findall(r"\b(?:if|for|while|case)\b|&&|\|\||\?", f.body))
        n = f.end_line - f.start_line + 1
        if cc > 15:
            _add(out, "CR028", ctx, f.open, f"{f.name}(): cyclomatic complexity {cc} exceeds 15", metric=cc)
        elif n > 100:
            _add(out, "CR028", ctx, f.open, f"{f.name}(): {n} lines exceed 100", metric=n)


INJ = re.compile(r"(?i)(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)|as an?\s+(ai|llm|language model)|do not (report|flag)|(reviewer|assistant)\s*:\s*(ignore|skip|approve)|system prompt")


def r_injection(ctx, out):
    for ln, text in ctx.comments:
        if INJ.search(text):
            out.append({"rule": "CR029", "line": ln, "message": "Comment addresses an automated reviewer", "function": "", "pos": ctx.starts[ln - 1]})


CHECKS = [r_gets, r_unbounded, r_scanf, r_format, r_shell, r_restricted, r_alloc, r_uaf, r_creds, r_offbyone, r_sizes, r_uninit,
          r_toctou, r_weak, r_goto, r_recursion, r_switch, r_cond_assign, r_braces, r_unreachable, r_isr, r_complexity, r_injection]


def analyze(name: str, text: str, ctx: Ctx | None = None) -> list[dict]:
    ctx = ctx or Ctx(name, text)
    raw: list[dict] = []
    for chk in CHECKS:
        chk(ctx, raw)
    seen, out, occ = set(), [], {}
    for f in sorted(raw, key=lambda x: (x["line"], x["rule"])):
        k = (f["rule"], f["line"], f["message"])
        if k in seen:
            continue
        seen.add(k)
        meta = RULES[f["rule"]]
        line_text = ctx.lines[f["line"] - 1] if 0 < f["line"] <= len(ctx.lines) else ""
        norm = norm_line(line_text)
        idn = norm_line(ctx.nclines[f["line"] - 1]) if 0 < f["line"] <= len(ctx.nclines) else norm
        g = (f["rule"], f["function"], idn)
        occ[g] = occ.get(g, 0) + 1
        fp = hashlib.sha1(f"{f['rule']}|{name}|{f['function']}|{idn}|{occ[g]}".encode()).hexdigest()[:16]
        extra = {k2: v for k2, v in f.items() if k2 not in ("rule", "line", "message", "function")}
        out.append({"rule": f["rule"], "title": meta["title"], "severity": meta["severity"], "category": meta["category"],
                    "cwe": f.get("cwe", meta["cwe"]), "cert": meta["cert"], "misra": meta["misra"], "confidence": meta["confidence"],
                    "line": f["line"], "message": f["message"], "function": f["function"], "snippet": norm[:200],
                    "fingerprint": fp, "_extra": extra})
    return out


def loc(text: str) -> int:
    return sum(1 for l in Ctx("x", text).code.split("\n") if l.strip())


def redact_secrets(text: str) -> str:
    """Remove secret literals before code is sent to any LLM."""
    ctx, out = Ctx("x", text), []
    r_creds(ctx, out)
    for f in sorted(out, key=lambda x: -x["pos"]):
        if "lit" in f:
            a, b = f["lit"]
            text = text[:a] + "<REDACTED>" + text[b:]
        else:                                          # key material array: blank the initialiser
            m = re.compile(r"\{[^}]*\}").search(text, f["pos"])
            if m:
                text = text[:m.start()] + "{ /* REDACTED */ }" + text[m.end():]
    return text
