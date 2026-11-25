"""What the catalog's masks match in the index: the counts ``get_catalog``, ``get_domain`` and
``put_project`` report.

Pure functions over a :class:`~memini.search.catalog.CodeMap` and the file paths the index holds
of each repository (:func:`memini.catalog.store.source_files`). Every match is made by the map's
matcher, the one that computes a hit's context and whose expressions the search filters send to
Postgres (:mod:`memini.search.catalog`), so a count here is the number of files a search of that
domain looks at.

The work is linear in the files of the repositories involved times the masks written for them:
fine for a call an agent makes while it fills the catalog, not something to run per search.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from memini.catalog.model import (
    DomainCode,
    LanguageFiles,
    MaskFiles,
    MaskReport,
    Overlap,
    ServiceCoverage,
)
from memini.chunking import detect_language
from memini.search.catalog import CodeArea, CodeMap, ServiceArea
from memini.search.patterns import glob_regex

__all__ = [
    "MAX_OVERLAP_EXAMPLES",
    "ProjectReport",
    "domain_code",
    "languages",
    "matched_files",
    "project_report",
    "service_files",
]

MAX_OVERLAP_EXAMPLES = 3
"""Paths an overlap shows."""

type Files = Mapping[UUID, Sequence[str]]
"""The file paths the index holds of each git source, by source."""


@dataclass(frozen=True, slots=True)
class ProjectReport:
    masks: list[MaskReport]
    overlaps: list[Overlap]
    services: list[ServiceCoverage]


def service_files(catalog: CodeMap, service: ServiceArea, files: Files) -> list[str]:
    """The files below the service's root."""
    return catalog.matching(service.regex, files.get(service.source_id, ()))


def matched_files(catalog: CodeMap, area: CodeArea, files: Files) -> set[str]:
    """The files one of the entry's masks matches."""
    return set(catalog.matching(area.regex, files.get(area.service.source_id, ())))


def languages(paths: Sequence[str]) -> list[LanguageFiles]:
    """``paths`` by the language of their extension, most files first, then by name; a file
    without a grammar is counted in none."""
    counts = Counter(language for path in paths if (language := detect_language(path)) is not None)
    return [
        LanguageFiles(language=language, files=count)
        for language, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _mask_counts(catalog: CodeMap, area: CodeArea, files: Files) -> list[MaskFiles]:
    paths = files.get(area.service.source_id, ())
    return [
        MaskFiles(
            path=mask,
            files=len(catalog.matching(glob_regex(mask, root=area.service.root), paths)),
        )
        for mask in area.paths
    ]


def domain_code(
    catalog: CodeMap, areas: Sequence[CodeArea], files: Files, repositories: Mapping[UUID, str]
) -> tuple[list[DomainCode], set[tuple[UUID, str]]]:
    """The code entries ``areas`` with what each mask matches, by project and service, and the
    files they match together, each as ``(source, path)``."""
    entries: list[DomainCode] = []
    together: set[tuple[UUID, str]] = set()
    for area in sorted(areas, key=lambda each: (each.service.project, each.service.name)):
        matched = matched_files(catalog, area, files)
        together.update((area.service.source_id, path) for path in matched)
        entries.append(
            DomainCode(
                project=area.service.project,
                service=area.service.name,
                repository=repositories.get(area.service.source_id, ""),
                root=area.service.root,
                files=len(matched),
                paths=_mask_counts(catalog, area, files),
            )
        )
    return entries, together


def _related(one: CodeArea, other: CodeArea) -> bool:
    """The same domain, or a domain and its own subdomain: no overlap to report."""
    return one.domain == other.domain and (
        one.subdomain == other.subdomain or one.subdomain is None or other.subdomain is None
    )


def project_report(
    catalog: CodeMap, project: str, files: Files, repositories: Mapping[UUID, str]
) -> ProjectReport:
    """What the masks of ``project`` match: each mask's files, the pairs of its domains that
    name the same files, and how much of each of its services no domain of any project names."""
    own = sorted(
        (area for area in catalog.areas if area.project == project),
        key=lambda area: (area.address, area.service.project, area.service.name),
    )
    masks = [
        MaskReport(
            domain=area.address,
            project=area.service.project,
            service=area.service.name,
            path=counted.path,
            files=counted.files,
        )
        for area in own
        for counted in _mask_counts(catalog, area, files)
    ]

    matched = {area: matched_files(catalog, area, files) for area in own}
    shared: dict[tuple[str, str, UUID], set[str]] = {}
    for index, one in enumerate(own):
        for other in own[index + 1 :]:
            if one.service.source_id != other.service.source_id or _related(one, other):
                continue
            common = matched[one] & matched[other]
            if common:
                key = (one.address, other.address, one.service.source_id)
                shared.setdefault(key, set()).update(common)
    overlaps = [
        Overlap(
            domains=[first, second],
            repository=repositories.get(source_id, ""),
            files=len(common),
            examples=sorted(common)[:MAX_OVERLAP_EXAMPLES],
        )
        for (first, second, source_id), common in sorted(
            shared.items(), key=lambda item: (item[0][0], item[0][1], item[0][2].hex)
        )
    ]

    services: list[ServiceCoverage] = []
    for service in sorted(catalog.services, key=lambda each: each.name):
        if service.project != project:
            continue
        below = service_files(catalog, service, files)
        named: set[str] = set()
        for area in catalog.areas_in(service.source_id):
            named.update(catalog.matching(area.regex, below))
        unassigned = len(below) - len(named)
        services.append(
            ServiceCoverage(
                service=service.name,
                repository=repositories.get(service.source_id, ""),
                files=len(below),
                unassigned=unassigned,
                unassigned_share=round(unassigned / len(below), 4) if below else None,
            )
        )
    return ProjectReport(masks=masks, overlaps=overlaps, services=services)
