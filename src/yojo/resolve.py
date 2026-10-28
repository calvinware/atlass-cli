"""Deterministic natural-name resolution: a unique exact match or a clear refusal, never a guess.

Names compare case-insensitively. When several candidates match ignoring case, a unique
case-sensitive match wins; otherwise the result is ambiguous and the candidates are listed. When
nothing matches, a single clearly closest name is offered as a suggestion, never applied.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from yojo.contracts import YojoError
from yojo.transport import ServiceClient, sanitize_text

MAX_CANDIDATES = 10
SUGGESTION_CUTOFF = 0.75
USER_SEARCH_LIMIT = 10


@dataclass(frozen=True)
class Candidate:
    """One resolvable thing: ``label`` is shown, ``token`` is the exact input that selects it."""

    label: str
    value: Any
    names: tuple[str, ...] = ()
    token: str | None = None

    @property
    def pick_as(self) -> str:
        return self.token or self.label

    def shown(self) -> str:
        return self.label if self.pick_as == self.label else f"{self.label} ({self.pick_as})"


@dataclass
class Unresolved(Exception):
    """``given`` named no candidate (``not_found``) or several (``ambiguous``)."""

    kind: str
    what: str
    given: str
    candidates: list[Candidate] = field(default_factory=list)
    suggestion: Candidate | None = None
    scope: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def message(self) -> str:
        given = sanitize_text(self.given, 100)
        where = f" {self.scope}" if self.scope else ""
        if self.kind == "ambiguous":
            shown = ", ".join(c.shown() for c in self.candidates[:MAX_CANDIDATES])
            count = len(self.candidates)
            return f"{self.what.capitalize()} {given!r} matches {count}{where}: {shown}."
        text = f"No {self.what} {given!r}{where}."
        if self.suggestion is not None:
            text += f" Did you mean {self.suggestion.label!r}?"
        return text

    @property
    def fix(self) -> str:
        if self.kind == "ambiguous":
            shown = ", ".join(repr(c.pick_as) for c in self.candidates[:MAX_CANDIDATES])
            given = sanitize_text(self.given, 100)
            return f"Pass one of these exact values instead of {given!r}: {shown}."
        if self.suggestion is not None:
            return f"Use {self.suggestion.pick_as!r}."
        known = [c.label for c in self.candidates[:MAX_CANDIDATES]]
        if known:
            more = " ..." if len(self.candidates) > MAX_CANDIDATES else ""
            return f"Use one of: {', '.join(repr(k) for k in known)}{more}."
        return f"Check the {self.what} name; none is available{where_suffix(self.scope)}."

    @property
    def replacement(self) -> str | None:
        """The value a corrected command should carry, when one is certain."""
        return self.suggestion.pick_as if self.suggestion is not None else None

    def details(self) -> dict[str, Any]:
        out: dict[str, Any] = {"given": sanitize_text(self.given, 100), "kind": self.kind}
        if self.kind == "ambiguous":
            out["candidates"] = [c.pick_as for c in self.candidates[:MAX_CANDIDATES]]
        elif self.suggestion is not None:
            out["suggestion"] = self.suggestion.pick_as
        return out


def where_suffix(scope: str) -> str:
    return f" {scope}" if scope else ""


def _names(candidate: Candidate) -> tuple[str, ...]:
    return (candidate.label, *candidate.names)


def pick(given: str, candidates: Iterable[Candidate], what: str, scope: str = "") -> Candidate:
    """Return the one candidate named by ``given`` or raise ``Unresolved``."""
    pool = list(candidates)
    wanted = given.strip()
    folded = wanted.casefold()
    hits = [c for c in pool if folded in {n.casefold() for n in _names(c)}]
    if len(hits) > 1:
        exact = [c for c in hits if wanted in _names(c)]
        if len(exact) == 1:
            return exact[0]
        raise Unresolved("ambiguous", what, given, hits, scope=scope)
    if hits:
        return hits[0]
    raise Unresolved("not_found", what, given, pool, suggest(wanted, pool), scope)


def suggest(given: str, pool: list[Candidate]) -> Candidate | None:
    """Return the single closest candidate, or None if there is no clear winner."""
    folded = given.casefold()
    scored: list[tuple[float, int]] = []
    for index, candidate in enumerate(pool):
        best = max(
            difflib.SequenceMatcher(None, folded, name.casefold()).ratio()
            for name in _names(candidate)
        )
        if best >= SUGGESTION_CUTOFF:
            scored.append((best, index))
    scored.sort(reverse=True)
    if not scored or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05):
        return None
    return pool[scored[0][1]]


# --------------------------------------------------------------------------- users


def _user_candidate(raw: dict[str, Any]) -> Candidate | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    display = raw.get("displayName") if isinstance(raw.get("displayName"), str) else name
    names = tuple(
        str(v) for v in (raw.get("key"), raw.get("emailAddress"), name) if isinstance(v, str) and v
    )
    return Candidate(label=str(display), value={"name": name}, names=names, token=name)


UserLookup = Callable[[str], dict[str, Any]]


def user_lookup(client: ServiceClient) -> UserLookup:
    """Resolve a person by username, display name, or email with one bounded user search."""
    cache: dict[str, dict[str, Any]] = {}

    def lookup(given: str) -> dict[str, Any]:
        wanted = given.strip()
        if wanted.casefold() in cache:
            return cache[wanted.casefold()]
        raw = client.get(
            "/rest/api/2/user/search", {"username": wanted, "maxResults": USER_SEARCH_LIMIT}
        )
        if not isinstance(raw, list):
            raise YojoError(
                "response_invalid",
                "Jira returned an unexpected user search response.",
                "Check that the Jira URL points at Jira Data Center including any context path.",
                {"what": "user search"},
            )
        pool = [c for item in raw if isinstance(item, dict) and (c := _user_candidate(item))]
        try:
            chosen = pick(wanted, pool, "person")
        except Unresolved as exc:
            # A search hit that is not an exact name is a suggestion at most, never a choice.
            if exc.kind == "not_found" and len(pool) == 1:
                exc.suggestion = pool[0]
            raise
        cache[wanted.casefold()] = chosen.value
        return dict(chosen.value)

    return lookup
