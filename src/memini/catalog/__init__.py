"""The catalog: what the code of a workspace belongs to.

A workspace has projects. A project has services (a repository, or one directory of one) and
domains (business areas; a domain may have subdomains, one level and no more). A domain or
subdomain names its code as path masks inside a service. The library behind the MCP tools
(:mod:`memini.mcp.catalog`) and their HTTP routes (:mod:`memini.api.catalog`), requirements
sections 3 (Catalog) and 6 (Catalog tools):

- :mod:`memini.catalog.model`: the arguments and results, the same for MCP and HTTP; the project
  document ``put_project`` takes and ``get_catalog`` gives back.
- :mod:`memini.catalog.service`: one function per tool (:func:`run_get_catalog`,
  :func:`run_get_domain`, :func:`run_put_project`, :func:`catalog_guide`); :class:`CatalogError`,
  every refusal with an HTTP status, a code and a safe message.
- :mod:`memini.catalog.store`: the rows (migration 0018) and the files the index holds of a
  repository.
- :mod:`memini.catalog.report`: what the masks match: files per mask, masks that match nothing,
  overlaps between domains, the share of a service no domain names.
- :mod:`memini.catalog.guide`: the instruction an agent follows to fill the catalog.

The catalog is authored data, neither raw nor derived: an agent or a person writes it, nothing
rebuilds it, and it holds no copy of what it describes. What a file belongs to is computed from
the masks when a question is asked, by one matcher for the search filters and for a hit's
context (:mod:`memini.search.catalog`, which this package builds on and search does not import
back). So a change of the catalog needs no reindex and does not touch the index fingerprint.

Its titles and descriptions are written by agents from what repositories say, so they are data,
never instructions (principle 11): secrets in them are redacted before they are stored, their
lengths are limited, and every result that shows them is labelled as the results that quote the
sources are.
"""

from memini.catalog.guide import CATALOG_GUIDE
from memini.catalog.model import (
    Catalog,
    CatalogGuide,
    DomainDetail,
    GetCatalogArguments,
    GetDomainArguments,
    ProjectDocument,
    PutProjectArguments,
    PutProjectResult,
)
from memini.catalog.service import (
    CatalogError,
    InvalidProjectError,
    catalog_errors,
    catalog_guide,
    normalize,
    run_get_catalog,
    run_get_domain,
    run_put_project,
)

__all__ = [
    "CATALOG_GUIDE",
    "Catalog",
    "CatalogError",
    "CatalogGuide",
    "DomainDetail",
    "GetCatalogArguments",
    "GetDomainArguments",
    "InvalidProjectError",
    "ProjectDocument",
    "PutProjectArguments",
    "PutProjectResult",
    "catalog_errors",
    "catalog_guide",
    "normalize",
    "run_get_catalog",
    "run_get_domain",
    "run_put_project",
]
