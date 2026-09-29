"""Rule catalogue of the built-in analyser.

MISRA C:2012 references are given for orientation only. Titles here are short paraphrases, not the licensed
text: check rule numbers, classification and applicability against your licensed MISRA document and your
project's compliance matrix. Security rules map to CWE identifiers."""
from __future__ import annotations

RULES: dict[str, dict] = {}


def _r(id, cat, sev, title, why, fix, bad="", good="", misra=None, cwe=None):
    RULES[id] = {"id": id, "category": cat, "severity": sev, "title": title, "rationale": why, "fix": fix,
                 "bad": bad, "good": good, "misra": misra, "cwe": cwe or []}


# ---------------------------------------------------------------- security (CWE)
_r("CR-SEC-001", "security", "warning", "Unbounded string or buffer function",
   "gets/strcpy/strcat/sprintf/scanf(\"%s\") cannot limit the number of bytes written, so external input can overflow the destination.",
   "Use bounded functions and check the result: snprintf, strncpy/strlcpy with explicit termination, or memcpy after a length check. Never use gets.",
   "strcpy(line, user_text);", "snprintf(line, sizeof line, \"%s\", user_text);", cwe=[120])
_r("CR-SEC-002", "security", "error", "Format string controlled by a variable",
   "A non-literal format string lets data containing % conversions read or write memory.",
   "Pass a constant format string and give the data as an argument.", "printf(msg);", "printf(\"%s\", msg);", cwe=[134])
_r("CR-SEC-003", "security", "error", "Command built from non-constant input",
   "system/popen/exec with data from outside the ECU or tool allows command injection.",
   "Avoid shell execution. If unavoidable, use a fixed command and validate arguments against an allow-list.", "system(cmd);", "", cwe=[78])
_r("CR-SEC-004", "security", "warning", "Copy length not checked against the destination",
   "memcpy/memmove/memset/strncpy with a length that no earlier condition bounds can write past the destination buffer.",
   "Compare the length with the destination size before copying and reject or clamp oversized input.",
   "memcpy(buf, in, n);", "if (n > sizeof buf) { return E_RANGE; }\nmemcpy(buf, in, n);", cwe=[787, 119])
_r("CR-SEC-005", "security", "warning", "Allocation size computed by multiplication",
   "count * size can wrap around, producing a buffer smaller than expected.",
   "Use calloc, or check count against SIZE_MAX / size before multiplying.",
   "p = malloc(n * sizeof *p);", "if (n > SIZE_MAX / sizeof *p) { return NULL; }\np = malloc(n * sizeof *p);", cwe=[190])
_r("CR-SEC-006", "security", "error", "Allocation result used without a NULL check",
   "malloc/calloc/realloc return NULL on failure; using the pointer then dereferences NULL.",
   "Test the pointer before its first use and handle failure.", "p = malloc(n); p[0] = 0;", "p = malloc(n);\nif (p == NULL) { return E_NOMEM; }", cwe=[476])
_r("CR-SEC-007", "security", "error", "Use after free or double free",
   "A pointer is used or freed again after free() without being reassigned.",
   "Set the pointer to NULL after free and restructure the code so the memory has one owner.", "free(p);\np[0] = 1;", "free(p);\np = NULL;", cwe=[416, 415])
_r("CR-SEC-008", "security", "error", "Hard-coded credential or key",
   "Secrets in source code end up in every ECU image and repository clone and cannot be rotated.",
   "Provision keys at manufacturing/service time into protected storage (HSM, secure NVM) and reference them by handle.",
   "const char *password = \"s3cr3t\";", "", cwe=[798])
_r("CR-SEC-009", "security", "warning", "Predictable random number generator",
   "rand()/srand()/random() are predictable and must not produce seeds, nonces, challenges or keys.",
   "Use the platform's cryptographically secure generator (HSM/TRNG, CSM RNG).", "seed = rand();", "", cwe=[338])
_r("CR-SEC-010", "security", "warning", "Possible off-by-one in loop bound",
   "A loop using <= against a size/length constant and indexing with the loop variable runs one element past the array.",
   "Use < with the element count.", "for (i = 0; i <= N; i++) a[i] = 0;", "for (i = 0; i < N; i++) a[i] = 0;", cwe=[193])
_r("CR-SEC-011", "security", "warning", "Result of an error-prone library call ignored",
   "Ignoring the result of fopen, fread, send, snprintf ... hides failures that later become undefined behaviour.",
   "Check the return value and handle the failure path.", "fread(buf, 1, n, f);", "if (fread(buf, 1, n, f) != n) { ... }", cwe=[252])
_r("CR-SEC-014", "security", "warning", "Stack allocation of runtime size (alloca)",
   "alloca has no failure indication and an attacker-controlled size can exhaust the stack.",
   "Use a fixed-size buffer with a range check.", "p = alloca(n);", "", cwe=[770])

# ---------------------------------------------------------------- MISRA-oriented
_r("CR-MSR-0701", "misra", "warning", "Octal constant", "A leading zero makes a constant octal, which is often a typo (010 is 8).",
   "Write decimal or hexadecimal constants.", "x = 010;", "x = 8;", misra="Rule 7.1")
_r("CR-MSR-0201", "misra", "warning", "Unreachable code", "Statements after return/break/continue/goto can never execute.",
   "Remove the dead statements or fix the control flow.", misra="Rule 2.1")
_r("CR-MSR-1304", "misra", "warning", "Assignment used inside a condition",
   "if (a = b) is nearly always a typo for a comparison, and the result of an assignment should not be used.",
   "Compare with == or assign in a separate statement.", "if (state = 3)", "if (state == 3)", misra="Rule 13.4", cwe=[481])
_r("CR-MSR-1501", "misra", "info", "goto statement", "goto obscures control flow and complicates review and testing.",
   "Restructure with structured control flow or a single cleanup block.", misra="Rule 15.1")
_r("CR-MSR-1506", "misra", "warning", "Loop or branch body is not a compound statement",
   "Without braces, later edits easily move statements out of the intended branch.", "Always use braces.", "if (x) y();", "if (x) { y(); }", misra="Rule 15.6")
_r("CR-MSR-1603", "misra", "warning", "Switch clause falls through", "A case that does not end in break/return/goto executes the next case.",
   "End every non-empty clause with break, or document intentional fall-through.", misra="Rule 16.3")
_r("CR-MSR-1604", "misra", "warning", "Switch without default", "Unexpected values are silently ignored.", "Add a default clause that handles or reports the error.", misra="Rule 16.4")
_r("CR-MSR-1702", "misra", "warning", "Recursion", "Recursion makes stack usage unbounded and hard to analyse in embedded systems.",
   "Rewrite iteratively or prove a bound on the depth.", misra="Rule 17.2")
_r("CR-MSR-1707", "misra", "warning", "Return value of a function is unused", "The result of a non-void function is discarded.",
   "Use the result, or cast to (void) where ignoring it is deliberate and documented.", "compute_crc(buf, n);", "(void)compute_crc(buf, n);", misra="Rule 17.7", cwe=[252])
_r("CR-MSR-2103", "misra", "warning", "Dynamic memory allocation", "malloc/calloc/realloc/free are not deterministic and fragment memory.",
   "Use static allocation or a qualified memory pool.", misra="Rule 21.3")
_r("CR-MSR-2104", "misra", "warning", "setjmp/longjmp", "Non-local jumps bypass normal control flow.", "Use structured error handling.", misra="Rule 21.4")
_r("CR-MSR-2106", "misra", "warning", "Standard I/O library function", "stdio functions are unsuitable for production embedded code.",
   "Use the project's logging and storage abstractions.", misra="Rule 21.6")
_r("CR-MSR-2107", "misra", "warning", "atoi/atof/atol", "These functions have undefined behaviour on invalid input and give no error indication.",
   "Use strtol/strtoul with error checking.", misra="Rule 21.7")
_r("CR-MSR-2108", "misra", "warning", "abort/exit/getenv/system", "Process-level functions are not available or not wanted in embedded systems.",
   "Use the ECU's error and reset handling.", misra="Rule 21.8")
_r("CR-MSR-D406", "misra", "info", "Basic numeric type used", "int/short/long do not have a defined size across compilers.",
   "Use fixed-width types (uint8_t, int32_t ...).", misra="Dir 4.6")

# ---------------------------------------------------------------- quality metrics
_r("CR-QLT-001", "quality", "warning", "High cyclomatic complexity", "Many decision points make a function hard to test and review.",
   "Split the function; extract decision tables or helper functions.")
_r("CR-QLT-002", "quality", "warning", "Long function", "Long functions are hard to review.", "Split into smaller functions.")
_r("CR-QLT-003", "quality", "warning", "Deep nesting", "Deeply nested blocks hide the main flow.", "Use early returns and helper functions.")
_r("CR-QLT-004", "quality", "info", "Many parameters", "A long parameter list suggests a missing data structure.", "Group parameters into a struct.")

# ---------------------------------------------------------------- LLM-assisted and tools
_r("CR-LLM-001", "llm", "info", "LLM-suggested issue (unverified)", "Reported by a language model from the code text; not confirmed by a rule or tool.",
   "Validate by reading the code; confirm or mark as false positive.")

DEFAULT_CONFIG = {"max_complexity": 10, "error_complexity": 20, "max_length": 100, "max_nesting": 4, "max_params": 6}
