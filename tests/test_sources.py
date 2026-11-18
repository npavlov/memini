"""Source settings, write access and the administration routes, without a database.

The database-backed paths are in tests/test_api_sources_db.py. Every name, URL and path here is
synthetic.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from memini.api import create_app
from memini.auth import (
    AuthenticationError,
    InsufficientScopeError,
    Principal,
    Scope,
    WorkspaceLimitError,
    WriteAccess,
    authorize_write,
    generate_token,
)
from memini.config import Settings
from memini.git import GitOptions, InvalidRevision, check_tracked_ref
from memini.runs import RunCursor
from memini.sources import (
    MAX_PATTERNS,
    MAX_REFS,
    GitConfig,
    InvalidSourceError,
    check_location,
    check_patterns,
    check_ref,
    check_refs,
    check_schedule,
    path_matches,
)

OWNER = uuid.UUID("00000000-0000-7000-8000-0000000000f0")
WS_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
SECRET = "fake-password-for-tests-only-5b2f"  # noqa: S105 - fake, for leak checks


def principal(*scopes: Scope, workspace_id: uuid.UUID | None = None) -> Principal:
    return Principal(
        principal_id=OWNER,
        token_id=uuid.uuid4(),
        scopes=frozenset(scopes),
        workspace_id=workspace_id,
    )


# --- Write access --------------------------------------------------------------------------------


def rendered(access: WriteAccess) -> str:
    return access.where(
        workspace_id=sql.Identifier("t", "workspace_id"),
        owner_id=sql.Identifier("t", "owner_id"),
    ).as_string(None)


def test_a_write_needs_its_scope() -> None:
    with pytest.raises(InsufficientScopeError) as refused:
        authorize_write(principal("read", "ingest"), "admin")
    assert refused.value.scope == "admin"


def test_an_unlimited_token_writes_anywhere_as_its_principal() -> None:
    access = authorize_write(principal("admin"), "admin")
    assert access.owner_id == OWNER
    assert access.workspace_ids is None
    assert access.allows(WS_A)
    assert rendered(access) == "TRUE"
    access.ensure_unlimited()


def test_a_token_limited_to_a_workspace_writes_only_there() -> None:
    access = authorize_write(principal("admin", workspace_id=WS_A), "admin")
    assert access.workspace_ids == {WS_A}
    assert access.allows(WS_A)
    assert not access.allows(uuid.uuid4())
    condition = rendered(access)
    assert condition.startswith('"t"."workspace_id" = ANY(')
    assert WS_A.hex in condition.replace("-", "")
    with pytest.raises(WorkspaceLimitError):
        access.ensure_unlimited()


def test_the_cli_writes_as_its_owner_without_a_limit() -> None:
    access = WriteAccess.for_owner(OWNER)
    assert access.owner_id == OWNER
    assert access.workspace_ids is None
    assert rendered(WriteAccess(owner_id=OWNER, workspace_ids=frozenset())) == "FALSE"


# --- Refs ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "key"),
    [
        ("main", "refs/heads/main"),
        ("release/2026", "refs/heads/release/2026"),
        ("refs/heads/main", "refs/heads/main"),
        ("refs/tags/v1.0", "refs/tags/v1.0"),
    ],
)
def test_a_ref_becomes_the_key_of_its_stream(ref: str, key: str) -> None:
    assert check_ref(ref) == key


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "HEAD",
        "refs/heads/",
        "refs/pull/1/head",
        "refs/remotes/origin/main",
        "main~1",
        "main^",
        "a..b",
        "with space",
        "-flag",
        "ends.lock",
        ".hidden",
        "trailing/",
        "x" * 1100,
    ],
)
def test_a_ref_that_is_not_a_branch_or_tag_is_refused(ref: str) -> None:
    with pytest.raises(InvalidSourceError) as refused:
        check_ref(ref)
    assert refused.value.setting == "refs"


def test_refs_are_sorted_without_duplicates() -> None:
    assert check_refs(["refs/tags/v1", "main", "refs/heads/main", "dev"]) == (
        "refs/heads/dev",
        "refs/heads/main",
        "refs/tags/v1",
    )


def test_a_source_tracks_at_least_one_ref_and_not_too_many() -> None:
    with pytest.raises(InvalidSourceError):
        check_refs([])
    with pytest.raises(InvalidSourceError):
        check_refs([f"branch-{n}" for n in range(MAX_REFS + 1)])
    with pytest.raises(TypeError):
        check_refs("main")


def test_the_git_helper_takes_only_full_branch_and_tag_names() -> None:
    assert check_tracked_ref("refs/heads/main") == "refs/heads/main"
    for ref in ("main", "refs/tags/", "refs/notes/x", "HEAD"):
        with pytest.raises(InvalidRevision):
            check_tracked_ref(ref)


# --- Path patterns -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("*.py", "setup.py", True),
        ("*.py", "src/app.py", False),
        ("**/*.py", "setup.py", True),
        ("**/*.py", "src/deep/app.py", True),
        ("src/**", "src/a/b.txt", True),
        ("src/**", "srcx/a.txt", False),
        ("docs/**/*.md", "docs/guide.md", True),
        ("docs/**/*.md", "docs/a/b/guide.md", True),
        ("docs/**/*.md", "other/docs/guide.md", False),
        ("**", "anything/at/all", True),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("[abc].txt", "b.txt", True),
        ("[!abc].txt", "b.txt", False),
        ("[!abc].txt", "d.txt", True),
        ("[a-c]x", "bx", True),
        ("[unclosed", "[unclosed", True),
        ("file.(x)+", "file.(x)+", True),
        ("**/vendor/**", "lib/vendor/x.js", True),
        ("**/vendor/**", "vendor/x.js", True),
    ],
)
def test_path_patterns_match_whole_paths(pattern: str, path: str, matches: bool) -> None:
    assert path_matches(pattern, path) is matches


def test_a_file_is_selected_by_include_and_exclude() -> None:
    every = GitConfig(location="https://git.example.invalid/a.git")
    assert every.selects("anything.bin")
    narrowed = GitConfig(
        location="https://git.example.invalid/a.git",
        include=("src/**", "README.md"),
        exclude=("**/*.min.js",),
    )
    assert narrowed.selects("src/app.js")
    assert narrowed.selects("README.md")
    assert not narrowed.selects("src/app.min.js")
    assert not narrowed.selects("docs/guide.md")


def test_patterns_keep_their_order_without_duplicates() -> None:
    assert check_patterns("include", ["src/**", "*.md", "src/**"]) == ("src/**", "*.md")


@pytest.mark.parametrize(
    "pattern", ["", "/abs/**", "src/", "a//b", "./src", "src/../etc", "tab\there", "x" * 1100]
)
def test_a_bad_pattern_is_refused(pattern: str) -> None:
    with pytest.raises(InvalidSourceError) as refused:
        check_patterns("exclude", [pattern])
    assert refused.value.setting == "exclude"


def test_too_many_patterns_are_refused() -> None:
    with pytest.raises(InvalidSourceError):
        check_patterns("include", [f"dir{n}/**" for n in range(MAX_PATTERNS + 1)])


def test_the_config_round_trips_through_json_and_reads_leniently() -> None:
    config = GitConfig(location="/srv/repos/app", include=("src/**",), exclude=("*.lock",))
    stored = config.to_json()
    assert stored == {"location": "/srv/repos/app", "include": ["src/**"], "exclude": ["*.lock"]}
    assert GitConfig.from_json(stored) == config
    assert GitConfig.from_json({"include": "not a list", "exclude": [1, "x"]}) == GitConfig(
        location="", include=(), exclude=("x",)
    )


def test_builtin_excludes_are_stored_only_when_switched_off() -> None:
    off = GitConfig(location="/srv/repos/app", builtin_excludes=False)
    assert off.to_json() == {
        "location": "/srv/repos/app",
        "include": [],
        "exclude": [],
        "builtin_excludes": False,
    }
    assert GitConfig.from_json(off.to_json()) == off
    assert GitConfig.from_json({"location": "/a"}).builtin_excludes is True
    # Read leniently: only false switches them off.
    assert GitConfig.from_json({"builtin_excludes": "false"}).builtin_excludes is True
    assert GitConfig.from_json({"builtin_excludes": 0}).builtin_excludes is True


# --- Schedule ------------------------------------------------------------------------------------


def test_a_schedule_is_from_a_minute_to_a_year_or_none() -> None:
    assert check_schedule(None) is None
    assert check_schedule(timedelta(hours=1)) == timedelta(hours=1)
    for schedule in (timedelta(seconds=59), timedelta(days=367), timedelta(0)):
        with pytest.raises(InvalidSourceError):
            check_schedule(schedule)


# --- Locations -----------------------------------------------------------------------------------


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    path = tmp_path / "repos"
    (path / "app").mkdir(parents=True)
    return path


@pytest.fixture
def options(tmp_path: Path, roots: Path) -> GitOptions:
    return GitOptions(mirror_dir=tmp_path / "mirrors", local_roots=(roots,))


def test_an_https_url_is_stored_in_canonical_form(options: GitOptions) -> None:
    assert (
        check_location("HTTPS://git.example.invalid/team/app.git", options)
        == "https://git.example.invalid/team/app.git"
    )


def test_a_local_path_is_stored_resolved(options: GitOptions, roots: Path) -> None:
    link = roots / "link"
    link.symlink_to(roots / "app")
    assert check_location(str(link), options) == os.path.realpath(roots / "app")


def test_ssh_needs_the_key_settings(options: GitOptions, tmp_path: Path) -> None:
    with pytest.raises(InvalidSourceError, match="MEMINI_GIT_SSH_KEY_PATH"):
        check_location("git@git.example.invalid:team/app.git", options)
    with_key = GitOptions(
        mirror_dir=options.mirror_dir,
        ssh_key_path=tmp_path / "key",
        ssh_known_hosts_path=tmp_path / "known_hosts",
    )
    assert (
        check_location("git@git.example.invalid:team/app.git", with_key)
        == "git@git.example.invalid:team/app.git"
    )


@pytest.mark.parametrize(
    "location",
    [
        "http://git.example.invalid/team/app.git",
        "ext::sh -c touch% /tmp/x",
        f"https://deploy:{SECRET}@git.example.invalid/team/app.git",
        f"https://{SECRET}@git.example.invalid/team/app.git",
        "git://git.example.invalid/team/app.git",
        "/nonexistent-root/repo",
        "file:///nonexistent-root/repo",
        "relative/repo",
        " https://git.example.invalid/team/app.git",
        "https://git.example.invalid/" + "x" * 2100,
    ],
)
def test_a_refused_location_never_appears_in_the_error(options: GitOptions, location: str) -> None:
    with pytest.raises(InvalidSourceError) as refused:
        check_location(location, options)
    assert refused.value.setting == "location"
    assert SECRET not in str(refused.value)
    assert location.strip() not in str(refused.value)


def test_a_local_path_inside_the_mirror_directory_is_refused(tmp_path: Path) -> None:
    mirrors = tmp_path / "mirrors"
    (mirrors / "other").mkdir(parents=True)
    options = GitOptions(mirror_dir=mirrors, local_roots=(tmp_path,))
    with pytest.raises(InvalidSourceError, match="mirror directory"):
        check_location(str(mirrors / "other"), options)


# --- Run cursors ---------------------------------------------------------------------------------


def test_a_run_cursor_round_trips_as_an_opaque_string() -> None:
    cursor = RunCursor(datetime(2026, 11, 18, 12, 0, 1, 234567, tzinfo=UTC), uuid.uuid4())
    text = cursor.encode()
    assert "|" not in text
    assert "=" not in text
    assert RunCursor.decode(text) == cursor


@pytest.mark.parametrize("text", ["", "garbage!", "bm90LWEtY3Vyc29y", "MjAyNi0xMS0xOHxub3Q"])
def test_a_bad_run_cursor_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        RunCursor.decode(text)


# --- Routes, before the database -----------------------------------------------------------------


class FakeAuthenticator:
    def __init__(self) -> None:
        self.tokens: dict[str, Principal] = {}

    def issue(self, *scopes: Scope) -> dict[str, str]:
        token = generate_token()
        self.tokens[token] = principal(*scopes)
        return {"Authorization": f"Bearer {token}"}

    async def __call__(self, token: str) -> Principal:
        try:
            return self.tokens[token]
        except KeyError:
            raise AuthenticationError("unknown") from None


WRITES = [
    ("POST", "/v1/workspaces", {"name": "alpha"}),
    (
        "POST",
        "/v1/workspaces/alpha/sources",
        {
            "kind": "git",
            "name": "app",
            "location": "https://git.example.invalid/a.git",
            "refs": ["main"],
        },
    ),
    ("PATCH", "/v1/workspaces/alpha/sources/app", {"schedule_seconds": 3600}),
    ("DELETE", "/v1/workspaces/alpha/sources/app", None),
    ("POST", "/v1/workspaces/alpha/sources/app/sync", None),
]

READS = [
    "/v1/workspaces/alpha/sources",
    "/v1/workspaces/alpha/sources/app",
    "/v1/workspaces/alpha/sources/app/streams",
    "/v1/workspaces/alpha/runs",
    f"/v1/runs/{uuid.UUID(int=1)}",
]


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()


@pytest.fixture
def client(settings: Settings, authenticator: FakeAuthenticator) -> TestClient:
    # No pool: a request that gets past the scope check answers 503 for the database.
    return TestClient(create_app(settings, authenticator=authenticator))


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_a_write_without_the_admin_scope_is_refused_before_the_database(
    client: TestClient,
    authenticator: FakeAuthenticator,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    headers = authenticator.issue("read", "ingest")
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert response.headers["WWW-Authenticate"].endswith('scope="admin"')

    admitted = client.request(method, path, json=body, headers=authenticator.issue("admin"))
    assert admitted.status_code == 503  # past the scope, at the missing database


@pytest.mark.parametrize("path", READS)
def test_a_read_needs_the_read_scope(
    client: TestClient, authenticator: FakeAuthenticator, path: str
) -> None:
    response = client.get(path, headers=authenticator.issue("admin"))
    assert response.status_code == 403
    assert response.headers["WWW-Authenticate"].endswith('scope="read"')
    assert client.get(path, headers=authenticator.issue("read")).status_code == 503


def test_the_schema_documents_the_administration_routes(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    expected = {
        "/v1/workspaces": {"get": "200", "post": "201"},
        "/v1/workspaces/{workspace}/sources": {"get": "200", "post": "201"},
        "/v1/workspaces/{workspace}/sources/{source}": {
            "get": "200",
            "patch": "200",
            "delete": "204",
        },
        "/v1/workspaces/{workspace}/sources/{source}/streams": {"get": "200"},
        "/v1/workspaces/{workspace}/sources/{source}/sync": {"post": "202"},
        "/v1/workspaces/{workspace}/runs": {"get": "200"},
        "/v1/runs/{run_id}": {"get": "200"},
    }
    for path, operations in expected.items():
        for method, status in operations.items():
            responses = paths[path][method]["responses"]
            assert status in responses, (method, path)
            assert "default" in responses
    components = schema["components"]["schemas"]
    for model in ("SourceInfo", "StreamInfo", "SyncAccepted", "RunList", "RunDetail"):
        assert model in components
    assert "location" not in components["SourceChanges"]["properties"]
