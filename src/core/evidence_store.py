"""Evidence ledger — append-only sources / evidence / claims store.

Modeled on the 199-biotechnologies/deep-research convention. Three parallel
JSONL files per decision day:

  sources.jsonl  — one row per unique source (URL/domain-clustered).
                   Sources are de-duplicated: 5 articles from the same outlet
                   get clustered into one source-cluster, which is what
                   "cluster-independent" counts measure.

  evidence.jsonl — one row per piece of evidence the agent encountered.
                   Each row references a source_id and a cluster_id.
                   Append-only; nothing is ever rewritten.

  claims.jsonl   — one row per atomic claim the analyzer makes in its
                   reasoning. Each claim cites one or more evidence_ids.
                   Mechanically verified: a claim with unknown evidence_ids
                   is rejected at submission time.

The store is per-decision-day. All sub-agents (research × N, analyzer, critic)
write to the same store, which lets us count cluster-independent support
across angles and detect overlap between for_yes and for_no research.
"""

import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("pm-trader.evidence")


def _normalize_url(url: str) -> str:
    """Strip query/fragment for de-dup; lowercase host."""
    if not url:
        return ""
    try:
        u = urlparse(url.strip())
        host = (u.hostname or "").lower()
        path = u.path or "/"
        # Drop common trackers
        return f"{u.scheme or 'https'}://{host}{path}".rstrip("/")
    except Exception:
        return url.strip()


def _domain_cluster(url: str) -> str:
    """Cluster ID for source independence: registrable domain.

    foo.cnn.com / cnn.com / amp.cnn.com all map to "cnn.com".
    Wire stories cross-published at multiple outlets won't be de-duplicated
    by this — that's a deeper problem; but at least same-outlet copies are.
    """
    if not url:
        return "unknown"
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "unknown"
    if not host:
        return "unknown"
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


class EvidenceStore:
    """Per-decision-day, thread-safe ledger.

    Multiple research sub-agents run in parallel; each appends without
    coordinating. Source de-dup is by normalized URL → existing source_id.
    """

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.sources_path = self.out_dir / "sources.jsonl"
        self.evidence_path = self.out_dir / "evidence.jsonl"
        self.claims_path = self.out_dir / "claims.jsonl"

        self._lock = threading.Lock()
        # url_norm -> source_id
        self._url_to_source: dict[str, str] = {}
        # source_id -> {"id", "cluster_id", "url", "domain", "title"}
        self._sources: dict[str, dict] = {}
        # cluster_id -> set[source_id]
        self._clusters: dict[str, set[str]] = {}
        # evidence_id -> evidence dict
        self._evidence: dict[str, dict] = {}

        self._next_source = 1
        self._next_evidence = 1
        self._next_claim = 1

    # ── source management ──────────────────────────────────────────────

    def add_source(self, *, url: str, title: str = "", date: str = "") -> str:
        """Idempotent. Returns the source_id."""
        norm = _normalize_url(url)
        with self._lock:
            existing = self._url_to_source.get(norm)
            if existing:
                return existing
            sid = f"S{self._next_source}"
            self._next_source += 1
            cluster = _domain_cluster(url)
            entry = {
                "id": sid,
                "url": url,
                "url_normalized": norm,
                "domain": cluster,
                "cluster_id": cluster,
                "title": title,
                "date": date,
            }
            self._url_to_source[norm] = sid
            self._sources[sid] = entry
            self._clusters.setdefault(cluster, set()).add(sid)
            self._append(self.sources_path, entry)
            return sid

    # ── evidence management ────────────────────────────────────────────

    def add_evidence(self, *, source_id: str, claim: str, snippet: str = "",
                     stance: str = "neutral", weight: str = "medium",
                     contributing_research: str = "") -> str:
        """Append a piece of evidence pointing at an existing source.

        Returns evidence_id (e.g. "E12"). Raises if source_id is unknown.
        """
        with self._lock:
            if source_id not in self._sources:
                raise ValueError(f"unknown source_id: {source_id}")
            eid = f"E{self._next_evidence}"
            self._next_evidence += 1
            entry = {
                "id": eid,
                "source_id": source_id,
                "cluster_id": self._sources[source_id]["cluster_id"],
                "claim": claim,
                "snippet": snippet,
                "stance": stance,
                "weight": weight,
                "contributing_research": contributing_research,
            }
            self._evidence[eid] = entry
            self._append(self.evidence_path, entry)
            return eid

    # ── claim management ───────────────────────────────────────────────

    def add_claim(self, *, statement: str, supporting_evidence_ids: list[str],
                  stance: str = "neutral", made_by: str = "analyzer") -> tuple[str, list[str]]:
        """Append a claim. Returns (claim_id, missing_evidence_ids).

        If `missing_evidence_ids` is non-empty, the claim was still recorded
        (so the trail is complete), but the caller should treat this as a
        validation failure and either reject the agent's output or surface it.
        """
        with self._lock:
            cid = f"C{self._next_claim}"
            self._next_claim += 1
            missing = [eid for eid in supporting_evidence_ids
                       if eid not in self._evidence]
            entry = {
                "id": cid,
                "statement": statement,
                "supporting_evidence": supporting_evidence_ids,
                "missing_evidence": missing,
                "stance": stance,
                "made_by": made_by,
                "verified": len(missing) == 0 and len(supporting_evidence_ids) > 0,
            }
            self._append(self.claims_path, entry)
            return cid, missing

    # ── queries ────────────────────────────────────────────────────────

    def cluster_independent_count(self, evidence_ids: list[str]) -> int:
        """How many distinct source-clusters back this set of evidence?

        Used by research_agent to estimate "strength" honestly: if all 5 cited
        evidence pieces come from cnn.com/foxnews.com, that's 2 clusters,
        not 5 independent sources.
        """
        with self._lock:
            clusters = set()
            for eid in evidence_ids:
                e = self._evidence.get(eid)
                if e:
                    clusters.add(e["cluster_id"])
            return len(clusters)

    def evidence_summary(self) -> dict:
        with self._lock:
            return {
                "n_sources": len(self._sources),
                "n_clusters": len(self._clusters),
                "n_evidence": len(self._evidence),
                "clusters": {c: sorted(list(s)) for c, s in self._clusters.items()},
            }

    def get_evidence(self, eid: str) -> Optional[dict]:
        with self._lock:
            return self._evidence.get(eid)

    # ── persistence ───────────────────────────────────────────────────

    def _append(self, path: Path, entry: dict) -> None:
        # Caller already holds the lock.
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write_summary(self) -> None:
        """Write a per-day summary with cluster counts and verification stats."""
        with self._lock:
            summary = {
                "n_sources": len(self._sources),
                "n_clusters": len(self._clusters),
                "n_evidence": len(self._evidence),
                "cluster_breakdown": {
                    c: len(s) for c, s in sorted(self._clusters.items(),
                                                  key=lambda x: -len(x[1]))
                },
            }
        with open(self.out_dir / "ledger_summary.json", "w") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
