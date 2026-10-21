"""Pure helpers for assembling the final Markdown report."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

from research_assistant.models import Finding, Source

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def normalize_url(url: str) -> str:
    """Key used to decide whether two URLs point to the same source.

    Scheme and host are case-insensitive, fragments are ignored and a trailing slash on the
    path does not make a URL different.
    """
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def dedupe_sources(sources: Iterable[Source]) -> list[Source]:
    """Merge sources with the same URL, keeping first-seen order.

    The first occurrence wins; a later duplicate only fills in a missing title or snippet.
    Sources without a URL are dropped.
    """
    merged: dict[str, Source] = {}
    for source in sources:
        if not source.url.strip():
            continue
        key = normalize_url(source.url)
        existing = merged.get(key)
        if existing is None:
            merged[key] = source.model_copy()
            continue
        if not existing.title and source.title:
            existing.title = source.title
        if not existing.snippet and source.snippet:
            existing.snippet = source.snippet
    return list(merged.values())


def collect_sources(findings: Iterable[Finding]) -> list[Source]:
    """Deduplicated sources across all successful findings, in finding order."""
    return dedupe_sources(
        source for finding in findings if finding.ok for source in finding.sources
    )


def _escape_link_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def render_sources(sources: Sequence[Source]) -> str:
    """Numbered ``## Sources`` section. Numbers match the ``[n]`` citations in the report."""
    if not sources:
        return "## Sources\n\n_No sources were gathered._"
    lines = []
    for number, source in enumerate(sources, start=1):
        title = " ".join(source.title.split())
        if title:
            lines.append(f"{number}. [{_escape_link_text(title)}](<{source.url}>)")
        else:
            lines.append(f"{number}. <{source.url}>")
    return "## Sources\n\n" + "\n".join(lines)


def render_gaps(findings: Iterable[Finding]) -> str:
    """``## Research gaps`` section for sub-questions whose research failed, or ``""``."""
    failed = [finding for finding in findings if not finding.ok]
    if not failed:
        return ""
    lines = [
        f"- **{finding.question}**: could not be researched ({finding.error})" for finding in failed
    ]
    return (
        "## Research gaps\n\n"
        "The following sub-questions could not be answered, so the report may be incomplete:\n\n"
        + "\n".join(lines)
    )


def extract_title(markdown: str) -> str | None:
    """Text of the first level-1 heading, if any."""
    for line in markdown.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 1 and match.group(2):
            return match.group(2)
    return None


def extract_section(markdown: str, names: Collection[str]) -> str | None:
    """Body of the first level-2 section whose heading (case-insensitive) is in ``names``."""
    wanted = {name.lower() for name in names}
    collected: list[str] | None = None
    for line in markdown.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) <= 2:
            if collected is not None:
                break
            if len(match.group(1)) == 2 and match.group(2).lower() in wanted:
                collected = []
            continue
        if collected is not None:
            collected.append(line)
    if collected is None:
        return None
    body = "\n".join(collected).strip()
    return body or None


def strip_sections(markdown: str, names: Collection[str]) -> str:
    """Remove level-2 sections whose heading (case-insensitive) is in ``names``."""
    wanted = {name.lower() for name in names}
    kept: list[str] = []
    skipping = False
    for line in markdown.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) <= 2:
            skipping = len(match.group(1)) == 2 and match.group(2).lower() in wanted
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def first_paragraph(markdown: str) -> str:
    """First paragraph that is not a heading (used when no executive summary is present)."""
    for block in re.split(r"\n\s*\n", markdown):
        text = block.strip()
        if text and not text.startswith("#"):
            return text
    return ""
