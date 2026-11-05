"""Turning text into vectors so memories can be found by meaning. Runs on this machine, no outside service.

FastEmbedder uses a small ONNX model (fastembed). HashEmbedder is a plain word-hashing fallback that needs nothing
installed: weaker (it only knows shared words) but it keeps memory working if the model can't load, and it makes tests fast.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path

import numpy as np

log = logging.getLogger("astrid.embed")

_STOP = set("a an the and or but if of to in on at for with from by is are was were be been am do does did have has had i you he she it we they "
            "me my your his her our their this that these those so as not no yes just really very can could would should will its it's "
            "i'm you're he's she's that's there what when who how why about up out into over then than too also".split())


def _tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9']+", text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        for suf in ("ing", "ed", "es", "s", "ly"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


class Embedder:
    name = "base"
    # Similarity cut-offs differ per model: where a memory counts as related, as linked, as a duplicate, as mergeable.
    min_sim = 0.5
    link_sim = 0.7
    dup_sim = 0.92
    merge_sim = 0.78

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class HashEmbedder(Embedder):
    name = "hash-256"
    min_sim = 0.22
    link_sim = 0.3
    dup_sim = 0.85
    merge_sim = 0.5
    DIM = 256

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self.DIM, dtype=np.float32)
        toks = _tokens(text)
        for t in toks:
            h = int.from_bytes(hashlib.sha1(t.encode()).digest()[:8], "big")
            v[h % self.DIM] += 1.0
        for a, b in zip(toks, toks[1:]):
            h = int.from_bytes(hashlib.sha1((a + " " + b).encode()).digest()[:8], "big")
            v[h % self.DIM] += 0.5
        n = float(np.linalg.norm(v))
        return (v / n).tolist() if n else v.tolist()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class FastEmbedder(Embedder):
    # bge similarities sit high even for unrelated text, so the cut-offs are high too.
    min_sim = 0.55
    link_sim = 0.72
    dup_sim = 0.92
    merge_sim = 0.80

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.model_name = model_name
        self.name = model_name
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):
        from fastembed import TextEmbedding  # imported late: it is slow and optional
        cache = os.environ.get("FASTEMBED_CACHE") or str(Path.home() / ".cache" / "astrid" / "fastembed")
        Path(cache).mkdir(parents=True, exist_ok=True)
        return TextEmbedding(model_name=self.model_name, cache_dir=cache)

    def _run(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            self._model = self._load()
        out = []
        for v in self._model.embed(texts):
            v = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(v))
            out.append((v / n if n else v).tolist())
        return out

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with self._lock:  # one at a time; the model isn't thread-safe
            return await asyncio.to_thread(self._run, texts)


def make_embedder(model: str = "") -> Embedder:
    """The real model if fastembed is installed, otherwise the fallback."""
    if (model or "").lower() == "hash":
        return HashEmbedder()
    try:
        import fastembed  # noqa: F401
    except ImportError:
        log.warning("fastembed isn't installed: memory will match on shared words only. pip install fastembed for meaning-based recall")
        return HashEmbedder()
    return FastEmbedder(model or "BAAI/bge-small-en-v1.5")
