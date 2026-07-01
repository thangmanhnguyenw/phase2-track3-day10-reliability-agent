from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"\w+", text.lower())
    tokens: list[str] = list(words)
    for word in words:
        if len(word) >= 3:
            for i in range(len(word) - 2):
                tokens.append(word[i : i + 3])
    return tokens


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory semantic cache with n-gram cosine similarity and guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        self._entries = [
            e for e in self._entries if now - e.created_at <= self.ttl_seconds
        ]

        best_value: str | None = None
        best_key = ""
        best_score = 0.0

        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key

        if best_score >= self.similarity_threshold and best_value is not None:
            if _looks_like_false_hit(query, best_key):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_key,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                    }
                )
                return None, best_score
            return best_value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=metadata or {},
            )
        )

    @staticmethod
    def similarity(a: str, b: str) -> float:
        if a == b:
            return 1.0
        tokens_a = _tokenize(a)
        tokens_b = _tokenize(b)
        if not tokens_a or not tokens_b:
            return 0.0
        vec_a = Counter(tokens_a)
        vec_b = Counter(tokens_b)
        dot = sum(vec_a[t] * vec_b[t] for t in vec_a if t in vec_b)
        mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
        mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        exact_response = self._redis.hget(exact_key, "response")
        if exact_response is not None:
            return exact_response, 1.0

        best_value: str | None = None
        best_key = ""
        best_score = 0.0

        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query = self._redis.hget(key, "query")
            if cached_query is None:
                continue
            score = ResponseCache.similarity(query, cached_query)
            if score > best_score:
                best_score = score
                best_value = self._redis.hget(key, "response")
                best_key = cached_query

        if best_score >= self.similarity_threshold and best_value is not None:
            if _looks_like_false_hit(query, best_key):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_key,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                    }
                )
                return None, best_score
            return best_value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
