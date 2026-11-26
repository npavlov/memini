"""``memini get-catalog``, ``catalog-guide``, ``get-project`` and ``get-domain``: thin clients of
``GET /v1/workspaces/{workspace}/catalog``, ``/v1/catalog/guide``, ``…/projects/{project}`` and
``…/projects/{project}/domains/{domain}``, the MCP tools of the same names.

The catalog is a document of projects, services, domains and links, so each command prints the
API's answer as JSON, indented; titles, descriptions and notes in it are data. ``--workspace``
names the workspace; without it, the only one the token may read. Needs the ``read`` scope.
"""

import argparse

from memini.cli.arguments import Commands
from memini.cli.client import Api, segment
from memini.cli.common import EXIT_OK
from memini.cli.output import print_json
from memini.cli.reading import add_filter, add_workspace, resolve_workspace

__all__ = [
    "add_commands",
    "catalog_guide_command",
    "get_catalog_command",
    "get_domain_command",
    "get_project_command",
]


def _workspace_path(api: Api, workspace: str | None) -> str:
    return f"v1/workspaces/{segment(resolve_workspace(api, workspace))}"


def catalog_guide_command(args: argparse.Namespace, api: Api) -> int:
    print_json(api.get("v1/catalog/guide"))
    return EXIT_OK


def get_catalog_command(args: argparse.Namespace, api: Api) -> int:
    print_json(api.get(f"{_workspace_path(api, args.workspace)}/catalog"))
    return EXIT_OK


def get_project_command(args: argparse.Namespace, api: Api) -> int:
    path = f"{_workspace_path(api, args.workspace)}/projects/{segment(args.project)}"
    print_json(api.get(path, {"linked_projects": args.linked_projects}))
    return EXIT_OK


def get_domain_command(args: argparse.Namespace, api: Api) -> int:
    domain = "/".join(segment(part) for part in args.domain.split("/"))
    path = (
        f"{_workspace_path(api, args.workspace)}/projects/{segment(args.project)}/domains/{domain}"
    )
    print_json(api.get(path))
    return EXIT_OK


def add_commands(commands: Commands) -> None:
    """Add ``get-catalog``, ``catalog-guide``, ``get-project`` and ``get-domain``."""
    catalog = commands.add_client(
        "get-catalog",
        help="the projects of a workspace with their services and domains",
        description="The catalog of a workspace: its projects with their services and domains,"
        " and the repositories no service is in. The MCP tool get_catalog; prints JSON.",
    )
    add_workspace(catalog, "catalog")
    catalog.set_defaults(client=get_catalog_command)

    guide = commands.add_client(
        "catalog-guide",
        help="how to fill the catalog",
        description="The instruction an agent follows to fill the catalog: the MCP tool"
        " catalog_guide; prints JSON.",
    )
    guide.set_defaults(client=catalog_guide_command)

    project = commands.add_client(
        "get-project",
        help="a project of the catalog and its links",
        description="A project in the shape of its document, and its links in both directions."
        " The MCP tool get_project; prints JSON.",
    )
    project.add_argument("project", help="the project's name")
    add_workspace(project, "project")
    add_filter(project, "linked_projects", "only the links between it and this project")
    project.set_defaults(client=get_project_command)

    domain = commands.add_client(
        "get-domain",
        help="a domain or subdomain of a project and its code",
        description="A domain (fees) or subdomain (fees/berth) of a project with the code that is"
        " responsible for it. The MCP tool get_domain; prints JSON.",
    )
    domain.add_argument("project", help="the project's name")
    domain.add_argument("domain", help="the domain, or domain/subdomain")
    add_workspace(domain, "project")
    domain.set_defaults(client=get_domain_command)
