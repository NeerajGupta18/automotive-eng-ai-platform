"""Structured logging and Prometheus-format metrics without external dependencies."""
from __future__ import annotations

import json
import logging
import re
import threading
import time

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.created)) + f".{int(r.msecs):03d}Z", "level": r.levelname,
             "logger": r.name, "msg": r.getMessage()}
        d.update({k[2:]: v for k, v in r.__dict__.items() if k.startswith("x_")})
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> logging.Logger:
    """Idempotent: safe to call for every container (tests create many)."""
    log = logging.getLogger("aeai")
    log.setLevel(getattr(logging, level, logging.INFO))
    log.propagate = True
    for h in list(log.handlers):
        if getattr(h, "_aeai", False):
            log.removeHandler(h)
    h = logging.StreamHandler()
    h._aeai = True
    h.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(h)
    return log


_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def clean_id(v: str | None, n: int = 64) -> str | None:
    """Untrusted header values are reduced to a safe token before they reach logs or responses (log injection)."""
    v = _SAFE.sub("", v or "")[:n]
    return v or None


class Metrics:
    """Counters, gauges and histograms rendered in the Prometheus text exposition format."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[tuple, float] = {}
        self.hist: dict[tuple, list] = {}
        self.help: dict[str, tuple[str, str]] = {}
        self.started = time.time()

    def declare(self, name: str, kind: str, text: str) -> None:
        self.help[name] = (kind, text)

    def inc(self, name: str, labels: dict | None = None, v: float = 1) -> None:
        k = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self.counters[k] = self.counters.get(k, 0) + v

    def observe(self, name: str, seconds: float, labels: dict | None = None) -> None:
        k = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            h = self.hist.setdefault(k, [[0] * (len(BUCKETS) + 1), 0.0, 0])
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    h[0][i] += 1
            h[0][-1] += 1
            h[1] += seconds
            h[2] += 1

    @staticmethod
    def _lbl(labels: tuple, extra: dict | None = None) -> str:
        items = list(labels) + list((extra or {}).items())
        return "{" + ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in items) + "}" if items else ""

    def render(self, gauges: dict[str, float] | None = None) -> str:
        out, done = [], set()
        with self._lock:
            for (name, labels), v in sorted(self.counters.items()):
                if name not in done:
                    kind, text = self.help.get(name, ("counter", name))
                    out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
                    done.add(name)
                out.append(f"{name}{self._lbl(labels)} {v:g}")
            for (name, labels), (counts, total, n) in sorted(self.hist.items()):
                if name not in done:
                    out += [f"# HELP {name} {self.help.get(name, ('', name))[1]}", f"# TYPE {name} histogram"]
                    done.add(name)
                for i, b in enumerate(BUCKETS):
                    out.append(f"{name}_bucket{self._lbl(labels, {'le': b})} {counts[i]}")
                out += [f"{name}_bucket{self._lbl(labels, {'le': '+Inf'})} {counts[-1]}", f"{name}_sum{self._lbl(labels)} {total:.6f}", f"{name}_count{self._lbl(labels)} {n}"]
        for g, v in {"aeai_uptime_seconds": time.time() - self.started, **(gauges or {})}.items():
            out += [f"# TYPE {g} gauge", f"{g} {v:g}"]
        return "\n".join(out) + "\n"


def new_metrics() -> Metrics:
    m = Metrics()
    m.declare("aeai_http_requests_total", "counter", "HTTP requests by method, route template and status")
    m.declare("aeai_http_request_duration_seconds", "histogram", "HTTP request duration")
    m.declare("aeai_auth_events_total", "counter", "Authentication and authorization outcomes")
    m.declare("aeai_llm_calls_total", "counter", "LLM generate() calls by outcome")
    m.declare("aeai_llm_call_duration_seconds", "histogram", "LLM call duration")
    m.declare("aeai_rate_limited_total", "counter", "Requests rejected by a rate limit")
    return m


class InstrumentedLLM:
    """Transparent wrapper: counts calls, errors and latency of whatever backend is configured."""

    def __init__(self, inner, metrics: Metrics):
        self._inner, self._m = inner, metrics
        self.name = inner.name

    def generate(self, system: str, prompt: str, temperature: float = 0.1) -> str:
        t = time.perf_counter()
        try:
            out = self._inner.generate(system, prompt, temperature)
            self._m.inc("aeai_llm_calls_total", {"backend": self.name, "outcome": "ok"})
            return out
        except Exception:
            self._m.inc("aeai_llm_calls_total", {"backend": self.name, "outcome": "error"})
            raise
        finally:
            self._m.observe("aeai_llm_call_duration_seconds", time.perf_counter() - t, {"backend": self.name})
