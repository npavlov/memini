"""Person routes under ``/v1/workspaces/{workspace}/persons``: the HTTP face of
:mod:`memini.persons`.

| Route | Scope | Does |
|---|---|---|
| ``GET …/persons`` | read | list persons, or search them by name and alias |
| ``POST …/persons`` | admin | make a person by hand, with aliases (201) |
| ``GET …/persons/lookup`` | read | the canonical person an alias resolves to |
| ``GET …/persons/me`` | read | the caller's "me" |
| ``PUT …/persons/me`` | admin | mark a person as "me" |
| ``DELETE …/persons/me`` | admin | mark no one (204) |
| ``GET …/persons/{id}`` | read | one person with its aliases |
| ``PATCH …/persons/{id}`` | admin | change its display name |
| ``POST …/persons/{id}/aliases`` | admin | add an alias; merges the person that holds it |
| ``DELETE …/persons/{id}/aliases/{alias}`` | admin | remove an alias |
| ``POST …/persons/{id}/merge`` | admin | merge the person into another |
| ``POST …/persons/{id}/split`` | admin | undo the merge of the person |

A token limited to one workspace sees and changes nothing in another: such a workspace, and its
persons, answer 404 as ones that do not exist. A merge or split that cannot be made answers 409
with the reason as its code: ``already_merged``, ``same_person``, ``not_merged``. An alias that is
not valid for its kind answers 422 ``invalid_alias``, naming the field, never the value.
"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from memini import persons
from memini.api.auth import AdminAccess, ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.auth import WorkspaceLimitError
from memini.persons import AliasKind
from memini.workspaces import NotFoundError

__all__ = [
    "AliasAddedInfo",
    "AliasBody",
    "AliasInfo",
    "MeBody",
    "MergeBody",
    "NewPerson",
    "PersonChanges",
    "PersonInfo",
    "PersonList",
    "build_persons_router",
]

_KIND = (
    "email: a mail address. chat_user_id: a user id in a chat or meeting system, as sources send"
    " it. display_name: a name as a source shows it. git_author: an author name in commits."
)
_VALUE = (
    f"As seen in the source. At most {persons.MAX_VALUE_LENGTH} characters once trimmed. Looked"
    " up normalized: a mail address lowercased, a name case-folded with its spaces collapsed, a"
    " user id as it is."
)
_NAME = f"The name to show, at most {persons.MAX_NAME_LENGTH} characters."


class AliasInfo(BaseModel):
    id: UUID
    person_id: UUID = Field(
        description="The person that holds the alias: this one, or one merged into it."
    )
    kind: AliasKind = Field(description=_KIND)
    value: str = Field(description="As seen in the source, or as given.")
    normalized_value: str = Field(description="The lookup key.")
    created_at: datetime

    @classmethod
    def of(cls, alias: persons.Alias) -> "AliasInfo":
        return cls(
            id=alias.id,
            person_id=alias.person_id,
            kind=alias.kind,
            value=alias.value,
            normalized_value=alias.normalized_value,
            created_at=alias.created_at,
        )


class PersonInfo(BaseModel):
    id: UUID
    workspace: str = Field(description="The name of the workspace.")
    display_name: str
    canonical_id: UUID = Field(
        description="The person this one resolves to: itself, unless it is merged into another."
    )
    merged_into: UUID | None = Field(
        description="The person it was merged into; null for a canonical person. A split clears it."
    )
    merged_at: datetime | None
    members: list[UUID] = Field(description="The persons merged into this one directly.")
    aliases: list[AliasInfo] = Field(
        description="Its aliases and those of every person merged into it; by kind and key."
    )
    is_me: bool = Field(description='Whether the caller\'s "me" resolves to the same person.')
    created_at: datetime

    @classmethod
    def of(cls, person: persons.Person) -> "PersonInfo":
        return cls(
            id=person.id,
            workspace=person.workspace,
            display_name=person.display_name,
            canonical_id=person.canonical_id,
            merged_into=person.merged_into,
            merged_at=person.merged_at,
            members=list(person.members),
            aliases=[AliasInfo.of(alias) for alias in person.aliases],
            is_me=person.is_me,
            created_at=person.created_at,
        )


class PersonList(BaseModel):
    persons: list[PersonInfo] = Field(description="By display name.")
    next_after: str | None = Field(
        description="Pass it as after= for the next page; null on the last page."
    )


class AliasBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: AliasKind = Field(description=_KIND)
    value: str = Field(min_length=1, max_length=1000, description=_VALUE)


class NewPerson(BaseModel):
    """A person made by hand, e.g. "me" before any record names it. Its aliases must be free."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=1000, description=_NAME)
    aliases: list[AliasBody] = Field(default_factory=list[AliasBody], max_length=100)


class PersonChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=1000, description=_NAME)


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    into: UUID = Field(description="The person to merge this one into.")


class MeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: UUID = Field(description="The person who is the caller in this workspace.")


class AliasAddedInfo(BaseModel):
    person: PersonInfo = Field(description="The person the alias was added to, as it is now.")
    alias: AliasInfo
    created: bool = Field(
        description="false: the person, or a person merged into it, held the alias already."
    )
    merged: UUID | None = Field(
        description="The person that held the alias and is now merged into this one; POST"
        " …/persons/{merged}/split undoes it. null if none was."
    )


@contextmanager
def _person_errors() -> Generator[None]:
    try:
        yield
    except NotFoundError as error:
        raise ApiError(404, "not_found", f"No such {error.what}.") from None
    except WorkspaceLimitError:
        raise ApiError(
            403,
            "workspace_limited",
            "The token is limited to one workspace; this needs a token that is not.",
        ) from None
    except persons.InvalidAliasError as error:
        raise ApiError(
            422, "invalid_alias", f"The {error.field} is not valid: {error.reason}."
        ) from None
    except persons.AliasHeldError as error:
        raise ApiError(
            409,
            "alias_held",
            f"Person {error.person_id} holds the alias: add the other aliases to it, or merge.",
        ) from None
    except persons.MergeError as error:
        raise ApiError(409, error.refusal, f"{str(error).capitalize()}.") from None


def build_persons_router() -> APIRouter:
    """The person routes; see the module."""
    router = APIRouter(prefix="/workspaces/{workspace}/persons", tags=["persons"])

    @router.get("", summary="List persons")
    async def list_persons(
        workspace: str,
        access: ReadAccess,
        conn: Connection,
        q: Annotated[
            str | None,
            Query(
                max_length=1000,
                description="Only persons whose display name or an alias contains this,"
                " ignoring case.",
            ),
        ] = None,
        merged: Annotated[
            bool,
            Query(description="List persons merged into others too; by default only canonical"),
        ] = False,
        limit: Annotated[
            int, Query(ge=1, le=persons.MAX_PERSON_PAGE)
        ] = persons.DEFAULT_PERSON_PAGE,
        after: Annotated[str | None, Query(description="next_after of the previous page.")] = None,
    ) -> PersonList:
        """The persons of a workspace by display name, each with the aliases of everyone merged
        into it."""
        cursor = None
        if after is not None:
            try:
                cursor = persons.PersonCursor.decode(after)
            except ValueError:
                raise ApiError(
                    422, "invalid_cursor", "after is not the next_after of a person listing."
                ) from None
        with _person_errors():
            page = await persons.list_persons(
                conn, access, workspace, query=q, merged=merged, limit=limit, after=cursor
            )
        return PersonList(
            persons=[PersonInfo.of(person) for person in page.persons],
            next_after=None if page.next is None else page.next.encode(),
        )

    @router.post("", status_code=201, summary="Add a person")
    async def create_person(
        workspace: str, body: NewPerson, access: AdminAccess, conn: Connection
    ) -> PersonInfo:
        """Make a person by hand with its aliases, so that records naming them resolve to it.
        409 ``alias_held`` if another person holds one of them: add it to that person instead."""
        with _person_errors():
            person = await persons.create_person(
                conn,
                access,
                workspace,
                body.display_name,
                [(alias.kind, alias.value) for alias in body.aliases],
            )
        return PersonInfo.of(person)

    @router.get("/lookup", summary="Find the person of an alias")
    async def lookup_person(
        workspace: str,
        access: ReadAccess,
        conn: Connection,
        kind: Annotated[AliasKind, Query(description=_KIND)],
        value: Annotated[str, Query(min_length=1, max_length=1000, description=_VALUE)],
    ) -> PersonInfo:
        """The canonical person the alias resolves to, as ingest resolves it. 404 if no person
        holds it."""
        with _person_errors():
            person = await persons.find_person(conn, access, workspace, kind, value)
        return PersonInfo.of(person)

    @router.get("/me", summary="Get me")
    async def get_me(workspace: str, access: ReadAccess, conn: Connection) -> PersonInfo:
        """The person marked as the caller in the workspace, resolved to its canonical person.
        404 ``no_me`` if no person is marked."""
        with _person_errors():
            person = await persons.get_me(conn, access, workspace)
        if person is None:
            raise ApiError(404, "no_me", "No person is marked as me in the workspace.")
        return PersonInfo.of(person)

    @router.put("/me", summary="Set me")
    async def set_me(
        workspace: str, body: MeBody, access: AdminAccess, conn: Connection
    ) -> PersonInfo:
        """Mark the person as the caller in the workspace, instead of any other."""
        with _person_errors():
            person = await persons.set_me(conn, access, workspace, body.person_id)
        return PersonInfo.of(person)

    @router.delete("/me", status_code=204, summary="Unset me")
    async def unset_me(workspace: str, access: AdminAccess, conn: Connection) -> None:
        """Mark no person as the caller in the workspace."""
        with _person_errors():
            await persons.unset_me(conn, access, workspace)

    @router.get("/{person_id}", summary="Get a person")
    async def get_person(
        workspace: str, person_id: UUID, access: ReadAccess, conn: Connection
    ) -> PersonInfo:
        """One person, merged or canonical, with the aliases of everyone merged into it."""
        with _person_errors():
            person = await persons.get_person(conn, access, workspace, person_id)
        return PersonInfo.of(person)

    @router.patch("/{person_id}", summary="Change a person")
    async def rename_person(
        workspace: str, person_id: UUID, body: PersonChanges, access: AdminAccess, conn: Connection
    ) -> PersonInfo:
        """Change the display name. Ingest never changes it."""
        with _person_errors():
            person = await persons.rename_person(
                conn, access, workspace, person_id, body.display_name
            )
        return PersonInfo.of(person)

    @router.post("/{person_id}/aliases", summary="Add an alias")
    async def add_alias(
        workspace: str, person_id: UUID, body: AliasBody, access: AdminAccess, conn: Connection
    ) -> AliasAddedInfo:
        """Add an alias to the person. If another person holds it, the two are one person: that
        one is merged into this one (``merged``), and a split of it undoes that. If this person,
        or one merged into it, holds it already, nothing changes (``created`` false)."""
        with _person_errors():
            added = await persons.add_alias(
                conn, access, workspace, person_id, body.kind, body.value
            )
        return AliasAddedInfo(
            person=PersonInfo.of(added.person),
            alias=AliasInfo.of(added.alias),
            created=added.created,
            merged=added.merged,
        )

    @router.delete("/{person_id}/aliases/{alias_id}", summary="Remove an alias")
    async def remove_alias(
        workspace: str, person_id: UUID, alias_id: UUID, access: AdminAccess, conn: Connection
    ) -> PersonInfo:
        """Remove an alias of the person or of one merged into it. The records that named it keep
        their links; a record written again with it resolves anew."""
        with _person_errors():
            person = await persons.remove_alias(conn, access, workspace, person_id, alias_id)
        return PersonInfo.of(person)

    @router.post("/{person_id}/merge", summary="Merge a person into another")
    async def merge_person(
        workspace: str, person_id: UUID, body: MergeBody, access: AdminAccess, conn: Connection
    ) -> PersonInfo:
        """Merge the person into ``into``; answers the canonical person they are now. Nothing is
        deleted or moved: the person keeps its aliases, records and decisions, which reads now
        give its canonical person. POST …/persons/{person_id}/split undoes it. 409
        ``already_merged`` if the person is merged into another already, ``same_person`` if the
        two are one person."""
        with _person_errors():
            person = await persons.merge_person(conn, access, workspace, person_id, body.into)
        return PersonInfo.of(person)

    @router.post("/{person_id}/split", summary="Split a person off")
    async def split_person(
        workspace: str, person_id: UUID, access: AdminAccess, conn: Connection
    ) -> PersonInfo:
        """Undo the merge of the person: it is canonical again, with everything it had, and the
        persons merged into it stay with it. 409 ``not_merged`` if it is not merged."""
        with _person_errors():
            person = await persons.split_person(conn, access, workspace, person_id)
        return PersonInfo.of(person)

    return router
