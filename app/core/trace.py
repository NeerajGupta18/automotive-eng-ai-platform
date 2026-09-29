"""Cross-artifact traceability graph shared by every module.

Nodes are reference strings (H-001, HE-002, SG-001, FSR-003, TS-010 ...). External artifacts use a
prefix: COMP:<component> (architecture element), TEST:<id> (verification artifact).
Edges are directed parent -> child (e.g. goal -> requirement -> component)."""
from __future__ import annotations

from collections import defaultdict, deque

from app.core.db import Database


class Trace:
    def __init__(self, db: Database):
        self.db = db

    def _adj(self, project: str) -> tuple[dict, dict]:
        down, up = defaultdict(list), defaultdict(list)
        for l in self.db.links(project):
            down[l["src"]].append((l["dst"], l["rel"]))
            up[l["dst"]].append((l["src"], l["rel"]))
        return down, up

    def children(self, project: str, ref: str, rel: str | None = None) -> list[str]:
        return [d for d, r in self._adj(project)[0].get(ref, []) if rel is None or r == rel]

    def parents(self, project: str, ref: str, rel: str | None = None) -> list[str]:
        return [d for d, r in self._adj(project)[1].get(ref, []) if rel is None or r == rel]

    def _walk(self, graph: dict, start: str) -> dict[str, int]:
        seen, q = {}, deque([(start, 0)])
        while q:
            n, d = q.popleft()
            for m, _ in graph.get(n, []):
                if m not in seen and m != start:
                    seen[m] = d + 1
                    q.append((m, d + 1))
        return seen

    def descendants(self, project: str, ref: str) -> dict[str, int]:
        return self._walk(self._adj(project)[0], ref)

    def ancestors(self, project: str, ref: str) -> dict[str, int]:
        return self._walk(self._adj(project)[1], ref)

    def replace_out(self, project: str, src: str, rel: str, dsts: list[str], actor: str) -> None:
        self.db.link_remove(project, src, rel)
        for d in dict.fromkeys(dsts):
            self.db.link_add(project, src, d, rel, actor)

    def dot(self, project: str, refs: set[str] | None = None) -> str:
        lines = ["digraph trace {", '  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef3fb", fontsize=10];']
        for l in self.db.links(project):
            if refs is None or (l["src"] in refs and l["dst"] in refs):
                lines.append(f'  "{l["src"]}" -> "{l["dst"]}" [label="{l["rel"]}", fontsize=8];')
        return "\n".join(lines + ["}"])
