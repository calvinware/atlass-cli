"""Deterministic feature candidate search. Ranks candidates; never selects one."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from atlass_cli.config import JiraConfig
from atlass_cli.contracts import AtlError
from atlass_cli.metadata import get_issue
from atlass_cli.response import nested_str
from atlass_cli.transport import ServiceClient

MAX_TOKENS = 10
CANDIDATE_POOL = 100
WEIGHT_EXACT_KEY = 100
WEIGHT_TOKEN = 10
WEIGHT_SAME_PROJECT = 5

KEY_IN_TEXT_RE = re.compile(r"\b([A-Z][A-Z0-9_]*-[1-9][0-9]*)\b")
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
STOPWORDS = frozenset(
    {
        *("a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in", "into"),
        *("is", "it", "not", "of", "on", "or", "that", "the", "this", "to", "was", "will", "with"),
    }
)


def jql_quote(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def tokenize(text: str) -> list[str]:
    seen: list[str] = []
    for raw in TOKEN_RE.findall(text):
        token = raw.lower()
        if len(token) < 3 or token in STOPWORDS or token in seen:
            continue
        seen.append(token)
    return seen


def _key_sort(key: str) -> tuple[str, int]:
    project, _, number = key.rpartition("-")
    return (project, int(number) if number.isdigit() else 0)


@dataclass(frozen=True)
class Candidate:
    key: str
    project: str
    summary: str
    status: str | None
    score: int
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "project": self.project,
            "summary": self.summary,
            "status": self.status,
            "score": self.score,
            "reasons": list(self.reasons),
        }


def rank(
    issues: list[dict[str, Any]],
    tokens: list[str],
    mentioned_keys: set[str],
    source_project: str | None,
    exclude_key: str | None,
    limit: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    token_set = set(tokens)
    for issue in issues:
        key = issue.get("key")
        fields = issue.get("fields") or {}
        if not isinstance(key, str) or not isinstance(fields, dict) or key == exclude_key:
            continue
        summary = str(fields.get("summary") or "")
        project = nested_str(fields.get("project"), "key") or key.rpartition("-")[0]
        status = nested_str(fields.get("status"))
        score = 0
        reasons: list[str] = []
        if key in mentioned_keys:
            score += WEIGHT_EXACT_KEY
            reasons.append("exact key mentioned in the query")
        shared = [t for t in tokenize(summary) if t in token_set]
        if shared:
            score += WEIGHT_TOKEN * len(shared)
            reasons.append(f"summary shares {len(shared)} word(s): {', '.join(shared)}")
        if score == 0:
            continue
        if source_project is not None and project == source_project:
            score += WEIGHT_SAME_PROJECT
            reasons.append(f"same project as {source_project}")
        candidates.append(
            Candidate(
                key=key,
                project=project,
                summary=summary,
                status=status,
                score=score,
                reasons=tuple(reasons),
            )
        )
    candidates.sort(key=lambda c: (-c.score, *_key_sort(c.key)))
    return candidates[:limit]


def find(
    client: ServiceClient,
    jira: JiraConfig,
    *,
    issue_key: str | None,
    text: str | None,
    limit: int,
) -> dict[str, Any]:
    source_project: str | None = None
    if issue_key is not None:
        snapshot = get_issue(client, issue_key)
        query = snapshot.summary
        source_project = snapshot.project
        mode = "issue"
        mentioned: set[str] = set()
    else:
        assert text is not None
        query = text
        mode = "text"
        mentioned = {
            k for k in KEY_IN_TEXT_RE.findall(text) if k.rpartition("-")[0] in jira.projects
        }

    tokens = tokenize(query)[:MAX_TOKENS]
    if not tokens and not mentioned:
        raise AtlError(
            "usage_error",
            "The query has no searchable words (3+ letters, not stop words).",
            "Pass more descriptive --text, or an --issue whose summary has meaningful words.",
            {"mode": mode},
        )

    scope = " OR ".join(
        f"(project = {jql_quote(p.key)} AND issuetype = {jql_quote(p.feature_type)})"
        for p in sorted(jira.projects.values(), key=lambda p: p.key)
    )
    pool: dict[str, dict[str, Any]] = {}
    truncated = False
    issues = _named_issues(client, scope, mentioned) if mentioned else []
    if tokens:
        text_issues, truncated = _text_pool(client, scope, tokens)
        issues.extend(text_issues)
    for issue in issues:
        if isinstance(issue, dict) and isinstance(issue.get("key"), str):
            pool.setdefault(issue["key"], issue)
    candidates = rank(list(pool.values()), tokens, mentioned, source_project, issue_key, limit)
    return {
        "mode": mode,
        "query": query,
        "candidates": [c.as_dict() for c in candidates],
        "candidate_pool_limit": CANDIDATE_POOL,
        "truncated": truncated,
        "selection": "caller",
    }


def _search(
    client: ServiceClient, jql: str, start: int, max_results: int
) -> tuple[list[Any], int | None]:
    raw = client.get(
        "/rest/api/2/search",
        {
            "jql": jql,
            "startAt": start,
            "maxResults": max_results,
            "fields": "summary,project,status",
        },
    )
    issues = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(issues, list):
        raise AtlError(
            "response_invalid",
            "Jira returned an unexpected search response.",
            "Check that jira.base_url points at Jira Data Center.",
        )
    total = raw.get("total")
    return issues[:max_results], total if type(total) is int and total >= 0 else None


def _text_pool(client: ServiceClient, scope: str, tokens: list[str]) -> tuple[list[Any], bool]:
    """Fetch the most recently updated matches; report whether the pool may omit any."""
    clauses = " OR ".join(f"summary ~ {jql_quote(t)}" for t in tokens)
    jql = f"({scope}) AND ({clauses}) ORDER BY updated DESC"
    issues, total = _search(client, jql, 0, CANDIDATE_POOL)
    truncated = total > len(issues) if total is not None else len(issues) >= CANDIDATE_POOL
    return issues, truncated


def _named_issues(client: ServiceClient, scope: str, keys: set[str]) -> list[Any]:
    """Fetch every mentioned key in scope, independent of the text pool and its limit."""
    jql = f"({scope}) AND key in ({', '.join(jql_quote(k) for k in sorted(keys))}) ORDER BY key ASC"
    found: list[Any] = []
    seen: set[str] = set()
    while True:
        issues, total = _search(client, jql, len(found), len(keys))
        for issue in issues:
            key = issue.get("key") if isinstance(issue, dict) else None
            if (
                not isinstance(key, str)
                or key not in keys
                or key in seen
                or not isinstance(issue.get("fields"), dict)
            ):
                raise AtlError(
                    "response_invalid",
                    "Jira returned an invalid named feature result.",
                    "Check the Jira search response before relying on feature candidates.",
                )
            seen.add(key)
        found.extend(issues)
        # `key in (...)` matches at most len(keys) issues; an empty page means no progress.
        expected = len(keys) if total is None else min(total, len(keys))
        if not issues or len(found) >= expected:
            return found
