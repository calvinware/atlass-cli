"""MkDocs hooks: site-only rendering that keeps the shared Markdown readable on GitHub.

- OS sections between `<!-- os-tabs:start -->` and `<!-- os-tabs:end -->` are ordinary
  `### Windows`, `### macOS`, `### Linux` headings in source. The site renders them as tabs.
- Relative links that leave `docs/` (for example `../AGENTS.md`) work on GitHub as written. The
  site has no copy of those files, so they are rewritten to the GitHub source URL.

This module does not import MkDocs, so its pure functions are testable with the dev extra alone.
"""

from __future__ import annotations

import functools
import posixpath
import re
from collections.abc import Callable
from typing import Any

OS_TABS_START = "<!-- os-tabs:start -->"
OS_TABS_END = "<!-- os-tabs:end -->"
OS_TAB_TITLES = ("Windows", "macOS", "Linux")
SOURCE_BRANCH = "main"

_HEADING_RE = re.compile(r"^### (?P<title>.+?)\s*$")
_LINK_TARGET_RE = re.compile(r"(?P<open>\]\()(?P<target>[^)\s]+)(?P<close>\))")
_CODE_SPAN_RE = re.compile(r"(`+).+?\1")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


class DocsRenderError(ValueError):
    """Shared Markdown does not follow the site rendering contract."""


def _is_fence(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def render_os_tabs(markdown: str) -> str:
    """Turn each marked block of OS headings into Material content tabs."""
    lines = markdown.split("\n")
    out: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_fence(line):
            in_fence = not in_fence
        marker = line.strip()
        if not in_fence and marker == OS_TABS_END:
            raise DocsRenderError(f"{OS_TABS_END} without a matching {OS_TABS_START}")
        if in_fence or marker != OS_TABS_START:
            out.append(line)
            index += 1
            continue
        block: list[str] = []
        block_fence = False
        index += 1
        while True:
            if index >= len(lines):
                raise DocsRenderError(f"{OS_TABS_START} without a matching {OS_TABS_END}")
            inner = lines[index]
            if _is_fence(inner):
                block_fence = not block_fence
            if not block_fence and inner.strip() == OS_TABS_START:
                raise DocsRenderError("os-tabs blocks cannot be nested")
            if not block_fence and inner.strip() == OS_TABS_END:
                break
            block.append(inner)
            index += 1
        out.extend(_tabs(block))
        index += 1
    return "\n".join(out)


def _tabs(block: list[str]) -> list[str]:
    sections: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in block:
        if _is_fence(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING_RE.match(line)
        if match:
            sections.append((match.group("title"), []))
        elif sections:
            sections[-1][1].append(line)
        elif line.strip():
            raise DocsRenderError("text before the first OS heading in an os-tabs block")
    titles = tuple(title for title, _ in sections)
    if titles != OS_TAB_TITLES:
        raise DocsRenderError(
            f"os-tabs headings must be exactly {', '.join(OS_TAB_TITLES)} as '### ' headings, "
            f"in that order; found {', '.join(titles) or 'none'}"
        )
    out: list[str] = []
    for title, body in sections:
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if not body:
            raise DocsRenderError(f"the {title} section in an os-tabs block is empty")
        out += [f'=== "{title}"', ""]
        out += [f"    {line}" if line.strip() else "" for line in body]
        out.append("")
    return out


def rewrite_external_links(markdown: str, page_path: str, repo_url: str) -> str:
    """Point relative links that leave docs/ at the GitHub source for that file."""
    base = f"{repo_url.rstrip('/')}/blob/{SOURCE_BRANCH}/"
    page_dir = posixpath.dirname(page_path)

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        if target.startswith(("#", "/")) or _SCHEME_RE.match(target):
            return match.group(0)
        path, _, fragment = target.partition("#")
        if not posixpath.normpath(posixpath.join(page_dir, path)).startswith("../"):
            return match.group(0)
        repo_path = posixpath.normpath(posixpath.join("docs", page_dir, path))
        if repo_path.startswith("../"):
            raise DocsRenderError(f"{page_path}: link {target!r} leaves the repository")
        url = base + repo_path + (f"#{fragment}" if fragment else "")
        return f"{match.group('open')}{url}{match.group('close')}"

    out: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if _is_fence(line):
            in_fence = not in_fence
        if in_fence:
            out.append(line)
            continue
        spans = [m.span() for m in _CODE_SPAN_RE.finditer(line)]
        out.append(_LINK_TARGET_RE.sub(functools.partial(_outside_code, spans, replace), line))
    return "\n".join(out)


def _outside_code(
    spans: list[tuple[int, int]], replace: Callable[[re.Match[str]], str], match: re.Match[str]
) -> str:
    if any(start <= match.start() < end for start, end in spans):
        return match.group(0)
    return replace(match)


def on_page_markdown(markdown: str, *, page: Any, config: Any, files: Any) -> str:
    repo_url = config["repo_url"]
    if not repo_url:
        raise DocsRenderError("mkdocs.yml must set repo_url to rewrite links outside docs/")
    rendered = render_os_tabs(markdown)
    return rewrite_external_links(rendered, page.file.src_uri, repo_url)
