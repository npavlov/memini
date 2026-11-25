"""What a decision belongs to, without a database: the order of the ways a project and domains
come (push, default, manual, code, model), the model counting only when nothing else gives a
domain, names the catalog does not hold, the filters and the count of items without a domain, the
names a link takes, and the project and domains of the push contract. Every name is synthetic. The
SQL runs in test_decision_domains_db.py.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from memini.decisions.context import (
    CatalogNames,
    Contexts,
    EvidenceFacts,
    ItemContext,
    ItemFacts,
    PlacedDomain,
    PlaceError,
    UnknownPlaceError,
    item_context,
    resolve_places,
)
from memini.ingest import Batch, InvalidBatchError, RecordIn, prepare_batch
from memini.ingest.model import DEFAULT_LIMITS
from memini.search.catalog import CodeArea, CodeMap, ServiceArea

QUAY = UUID(int=1)  # a git source
MAIL = UUID(int=2)  # a push source
CHAT = UUID(int=3)  # a push source
WORKSPACE = UUID(int=9)
T = datetime(2026, 5, 4, 9, tzinfo=UTC)

SERVICE = ServiceArea("port", "quay", QUAY, "")
CATALOG = CodeMap(
    projects=["port", "fleet"],
    services=[SERVICE],
    domains=[
        ("port", "fees", None),
        ("port", "fees", "berth"),
        ("port", "fees", "pilot"),
        ("port", "bookings", None),
        ("fleet", "crew", None),
        ("fleet", "fees", None),
    ],
    areas=[CodeArea("port", "fees", "berth", SERVICE, ("quay/fees/berth.py",))],
)
NAMES = CatalogNames.of(CATALOG)


type Defaults = dict[tuple[UUID, str | None], tuple[str, tuple[str, ...]]]


def mail(pushed: tuple[str, tuple[str, ...]] | None = None, stream: str | None = None) -> Any:
    return EvidenceFacts(MAIL, "push", "thread", "inbox/1", stream, pushed)


def code(path: str) -> EvidenceFacts:
    return EvidenceFacts(QUAY, "git", "code_file", f"refs/heads/main:{path}", None, None)


def context(
    *evidence: EvidenceFacts,
    links: tuple[tuple[str, str | None, str], ...] = (),
    defaults: Defaults | None = None,
) -> ItemContext:
    return item_context(evidence, links, defaults or {}, CATALOG, NAMES)


def domains(found: ItemContext) -> list[tuple[str, str, str]]:
    return [(d.project, d.domain, d.via) for d in found.domains]


# --- The order -----------------------------------------------------------------------------------


def test_a_pushed_project_comes_before_the_default_of_its_source() -> None:
    defaults: Defaults = {(MAIL, None): ("fleet", ("crew",))}

    pushed = context(mail(("port", ("bookings",))), defaults=defaults)
    assert (pushed.project, pushed.project_via) == ("port", "push")
    assert domains(pushed) == [("port", "bookings", "push")]

    fallback = context(mail(), defaults=defaults)
    assert (fallback.project, fallback.project_via) == ("fleet", "default")
    assert domains(fallback) == [("fleet", "crew", "default")]


def test_a_stream_default_comes_before_its_source_default() -> None:
    defaults: Defaults = {(MAIL, None): ("fleet", ("crew",)), (MAIL, "inbox"): ("port", ())}

    found = context(mail(stream="inbox"), defaults=defaults)
    assert (found.project, found.project_via, found.domains) == ("port", "default", ())
    assert context(mail(stream="other"), defaults=defaults).project == "fleet"


def test_manual_links_and_code_add_domains_and_the_first_way_names_the_project() -> None:
    found = context(
        mail(("port", ())),
        code("quay/fees/berth.py"),
        links=(("port", "bookings", "manual"), ("fleet", "crew", "manual")),
    )

    assert (found.project, found.project_via) == ("port", "push")
    assert domains(found) == [
        ("fleet", "crew", "manual"),
        ("port", "bookings", "manual"),
        ("port", "fees/berth", "code"),
    ]


def test_code_evidence_gives_the_domains_its_masks_match_and_the_service_project() -> None:
    found = context(code("quay/fees/berth.py"))
    assert (found.project, found.project_via) == ("port", "code")
    assert domains(found) == [("port", "fees/berth", "code")]

    elsewhere = context(code("quay/tides.py"))
    assert (elsewhere.project, elsewhere.domains) == ("port", ())


def test_the_model_counts_only_when_nothing_else_gives_a_domain() -> None:
    model = ("port", "bookings", "model")

    alone = context(mail(), links=(model,))
    assert (alone.project, alone.project_via) == ("port", "model")
    assert domains(alone) == [("port", "bookings", "model")]
    assert not alone.settled
    assert alone.fixed_project is None

    # A domain by hand: the model's is not shown, and the item is settled.
    overruled = context(mail(), links=(model, ("port", "fees/pilot", "manual")))
    assert domains(overruled) == [("port", "fees/pilot", "manual")]
    assert overruled.settled

    # A project by push without domains: the model's domain shows, and the project is fixed.
    fixed = context(mail(("port", ())), links=(model,))
    assert domains(fixed) == [("port", "bookings", "model")]
    assert fixed.fixed_project == "port"


def test_names_the_catalog_does_not_hold_are_left_out() -> None:
    found = context(
        mail(("harbour", ("fees",))),
        links=(("port", "nowhere", "manual"), ("port", "fees/pilot", "manual")),
    )

    assert (found.project, found.project_via) == ("port", "manual")
    assert domains(found) == [("port", "fees/pilot", "manual")]
    assert context(mail(("harbour", ()))) == ItemContext()


# --- Filters and counts --------------------------------------------------------------------------


def contexts(**placed: ItemContext) -> tuple[Contexts, dict[str, UUID]]:
    ids = {name: UUID(int=100 + n) for n, name in enumerate(placed)}
    found = Contexts(names={WORKSPACE: NAMES}, maps={WORKSPACE: CATALOG})
    for name, each in placed.items():
        found.items[ids[name]] = ItemFacts(WORKSPACE, "decision", "active", T)
        found.contexts[ids[name]] = each
    return found, ids


def at(project: str, *addresses: str) -> ItemContext:
    return ItemContext(
        project=project,
        project_via="manual",
        domains=tuple(PlacedDomain(project, a, "manual") for a in addresses),
    )


def test_a_domain_includes_its_subdomains_and_a_project_its_domains() -> None:
    found, ids = contexts(
        berth=at("port", "fees/berth"),
        fees=at("port", "fees"),
        crew=at("fleet", "crew"),
        bare=at("port"),
        fleet_fees=at("fleet", "fees"),
    )

    def chosen(**filters: Any) -> set[str]:
        picked = set(found.select(**filters))
        return {name for name, item_id in ids.items() if item_id in picked}

    assert chosen(domains=["fees"], projects=["port"]) == {"berth", "fees"}
    assert chosen(domains=["fees"]) == {"berth", "fees", "fleet_fees"}
    assert chosen(domains=["fees/berth"]) == {"berth"}
    assert chosen(projects=["port"]) == {"berth", "fees", "bare"}
    assert found.without_domain(["port", "fleet"]) == {"fleet": 0, "port": 1}

    with pytest.raises(UnknownPlaceError) as unknown:
        found.select(domains=["crew"], projects=["port"])
    assert unknown.value.field == "domains[0]"
    with pytest.raises(UnknownPlaceError):
        found.select(projects=["harbour"])


def test_a_status_filter_leaves_out_items_of_other_statuses() -> None:
    found, ids = contexts(old=at("port", "fees"))
    found.items[ids["old"]] = ItemFacts(WORKSPACE, "decision", "superseded", T)

    assert found.select(domains=["fees"], statuses=("active",)) == []
    assert found.select(domains=["fees"], statuses=("active", "superseded")) == [ids["old"]]


# --- The names of a link -------------------------------------------------------------------------


def test_a_link_names_what_the_catalog_holds() -> None:
    assert resolve_places(NAMES, "port", ["fees/berth", "bookings", "bookings"]) == [
        ("port", "fees/berth"),
        ("port", "bookings"),
    ]
    assert resolve_places(NAMES, "port", []) == [("port", None)]
    assert resolve_places(NAMES, None, ["crew"]) == [("fleet", "crew")]

    for project, names, field in (
        (None, ["fees"], "domains[0]"),  # two projects have it
        ("port", ["crew"], "domains[0]"),
        ("harbour", [], "project"),
        (None, ["nowhere"], "domains[0]"),
    ):
        with pytest.raises(UnknownPlaceError) as refused:
            resolve_places(NAMES, project, names)
        assert refused.value.field == field
    with pytest.raises(PlaceError) as invalid:
        resolve_places(NAMES, "port", ["Fees!"])
    assert not isinstance(invalid.value, UnknownPlaceError)
    with pytest.raises(PlaceError):
        resolve_places(NAMES, "port", [f"d{n}" for n in range(17)])


# --- The push contract ---------------------------------------------------------------------------


def record(**fields: Any) -> RecordIn:
    return RecordIn(
        kind="thread",
        external_id="inbox/1",
        occurred_at=T,
        payload={"messages": [{"author": "Ann Example", "sent_at": T.isoformat(), "text": "Hi"}]},
        **fields,
    )


def test_a_pushed_record_carries_its_project_and_domains_outside_its_content() -> None:
    plain = prepare_batch(Batch(records=[record()]), DEFAULT_LIMITS).records[0]
    placed = prepare_batch(
        Batch(records=[record(project="port", domains=["fees/berth", "fees/berth", "bookings"])]),
        DEFAULT_LIMITS,
    ).records[0]

    assert (placed.project, placed.domains) == ("port", ("fees/berth", "bookings"))
    assert (plain.project, plain.domains) == (None, ())
    assert placed.content_hash == plain.content_hash
    assert placed.payload == plain.payload


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"domains": ["fees"]}, "records[0].domains"),
        ({"project": "Port"}, "records[0].project"),
        ({"project": "port", "domains": ["fees/berth/more"]}, "records[0].domains[0]"),
        ({"project": "port", "domains": [f"d{n}" for n in range(17)]}, "records[0].domains"),
    ],
)
def test_a_project_or_domain_that_is_not_a_name_is_refused(
    fields: dict[str, Any], field: str
) -> None:
    with pytest.raises(InvalidBatchError) as refused:
        prepare_batch(Batch(records=[record(**fields)]), DEFAULT_LIMITS)
    assert refused.value.field == field


def test_a_tombstone_carries_no_project() -> None:
    tombstone = RecordIn(kind="thread", external_id="inbox/1", deleted=True, project="port")
    with pytest.raises(InvalidBatchError):
        prepare_batch(Batch(records=[tombstone]), DEFAULT_LIMITS)
