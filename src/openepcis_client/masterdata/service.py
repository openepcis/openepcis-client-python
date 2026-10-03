# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Publishing master data: per-record upsert, bulk onboarding, GPC search.

Two ways into the catalog, with different semantics:

- :meth:`Masterdata.upsert_product` / :meth:`Masterdata.upsert_organization`
  are idempotent merges. ``PUT`` creates or updates, and an absent key means
  "leave alone", so publishing never clears a value by accident.
- :meth:`Masterdata.bulk_products` / :meth:`Masterdata.bulk_organizations`
  wrap the CSV endpoint, which **creates and does not update**: a key the
  catalog already holds comes back as a duplicate rather than being
  overwritten. It is an onboarding tool for the first load, not a
  synchroniser; day-to-day changes go record by record.
"""

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from ..core import gs1
from ..core.client import Client
from ..core.errors import OpenEpcisError
from . import vocabulary

#: Records per page when walking a list.
LIST_PAGE_SIZE = 100

#: Rows per CSV upload. Keeps every chunk well under the endpoint's 10 MB cap,
#: so a chunk cannot be refused for size.
CHUNK_ROWS = 2000

#: Bulk error codes that mean "the catalog already holds this key" — the state
#: onboarding was trying to reach, so they are counted as present, not failed.
DUPLICATE_CODES = frozenset({"DUPLICATE_GTIN", "DUPLICATE_GLN"})

#: The code this side stamps on a row it refused to send because its key is
#: not a valid GS1 key. Kept apart from server codes on purpose.
INVALID_KEY = "INVALID_KEY"


class InvalidKey(ValueError):
    """A GS1 key that fails validation before any request is made.

    Carries the structured :class:`~openepcis_client.core.gs1.KeyProblem`, so a
    host adapter can phrase the fault in its own language instead of parsing
    an English sentence.
    """

    def __init__(self, key: str, problem: gs1.KeyProblem) -> None:
        super().__init__(f"{key!r} is not a valid {problem.kind} ({problem.fault})")
        self.key = key
        self.problem = problem


@dataclass(frozen=True)
class BulkRowError:
    """One row the bulk load did not accept."""

    row: int
    """1-based position in the rows handed to the bulk call, not in any chunk."""

    code: str
    """The server's error code, or :data:`INVALID_KEY` for a row refused here."""

    message: str


@dataclass(frozen=True)
class BulkReport:
    """What became of a bulk load, over all chunks."""

    total: int
    """Rows handed in."""

    accepted: int
    """Rows the catalog newly created."""

    duplicates: int
    """Rows whose key the catalog already held. Bulk loading only creates, so
    these are already in the state the load was aiming for."""

    failures: tuple[BulkRowError, ...]
    """Everything else, including rows refused here for an invalid key."""


@dataclass(frozen=True)
class Gs1Record:
    """The catalog records GS1 Germany's answer for one key derives into.

    A GTIN derives a product. A GLN derives an organization, a place, or both,
    because GS1 describes a party and its location under one number; at least
    one of the two is present.
    """

    key: str
    key_type: str
    """``GTIN`` or ``GLN``, as the server classified the key."""

    product: dict[str, Any] | None = None
    organization: dict[str, Any] | None = None
    place: dict[str, Any] | None = None


@dataclass(frozen=True)
class GpcNode:
    """One node of the GS1 Global Product Classification."""

    code: str
    title: str
    definition: str
    lineage: str
    """The human-readable path from segment down to this node."""


class Masterdata:
    """Master data calls against the resolver, through a configured client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    # -- Per-record upsert ---------------------------------------------------

    def upsert_product(self, gtin: str, document: dict[str, Any]) -> Any:
        """Create or update one product; ``PUT`` merges, so this is idempotent."""
        return self._upsert(vocabulary.kind("PRODUCT"), gtin, document)

    def upsert_organization(self, gln: str, document: dict[str, Any]) -> Any:
        """Create or update one organization."""
        return self._upsert(vocabulary.kind("ORGANIZATION"), gln, document)

    def _upsert(self, kind: vocabulary.Kind, key: str, document: dict[str, Any]) -> Any:
        cleaned = gs1.clean(key)
        problem = gs1.problem_with(cleaned, kind.key_type)
        if problem:
            raise InvalidKey(key, problem)
        payload = dict(document)
        payload[kind.key_term] = cleaned
        # The resolver's schema validation requires the GS1 class at the root
        # ("required property 'type' not found" otherwise). Callers stating
        # their own — e.g. ["Product", "TextileApparel"] — are left alone.
        payload.setdefault("type", kind.record_type)
        return self._client.put(f"{kind.endpoint}/{cleaned}", payload)

    # -- Reading back --------------------------------------------------------

    def get_organization(self, gln: str) -> dict[str, Any] | None:
        """One organization of the caller's tenant, or ``None`` when it holds none.

        The read is scoped to the tenant server side, so a GLN another tenant
        holds answers ``None`` exactly like one nobody holds.
        """
        return self._get_record(vocabulary.kind("ORGANIZATION"), gln)

    def iter_organizations(
        self,
        page_size: int = LIST_PAGE_SIZE,
        sort_by: str = "updatedAt",
        order: str = "desc",
    ) -> Iterator[dict[str, Any]]:
        """Every organization of the caller's tenant, page by page.

        The default order is most recently changed first. The resolver sorts
        by its change time but does not return it, so there is no cursor to
        resume from: a synchroniser walks the whole list and compares each
        record with what it last saw (a payload hash, say). That is fine for
        the few hundred parties a tenant holds; a ``modifiedSince`` filter on
        the server is the step after.

        Pages are fetched lazily. A record changed while the walk is under way
        can move between pages and be seen twice or not at all in this walk;
        the next walk sees it.
        """
        kind = vocabulary.kind("ORGANIZATION")
        page = 1
        while True:
            answer = (
                self._client.get(
                    kind.endpoint,
                    params={
                        "page": page,
                        "pageSize": page_size,
                        "sortBy": sort_by,
                        "sortOrder": order,
                    },
                )
                or {}
            )
            records = answer.get("organizations") or []
            yield from records
            total_pages = int(answer.get("totalPages") or 0)
            if not records or page >= total_pages:
                return
            page += 1

    def _get_record(self, kind: vocabulary.Kind, key: str) -> dict[str, Any] | None:
        cleaned = gs1.clean(key)
        problem = gs1.problem_with(cleaned, kind.key_type)
        if problem:
            raise InvalidKey(key, problem)
        try:
            answer = self._client.get(f"{kind.endpoint}/{cleaned}")
        except OpenEpcisError as exc:
            if exc.status == 404:
                return None
            raise
        return dict(answer) if answer else None

    # -- Import from GS1 -----------------------------------------------------

    def preview_from_gs1(self, key: str) -> Gs1Record | None:
        """What GS1 Germany knows about a key, derived into a catalog record.

        Read-only: nothing is stored. ``None`` when GS1 does not know the key.
        Keys in the GS1 example range (``952…``) are refused by the server with
        a 400, because Verified by GS1 does not answer for them.
        """
        cleaned = gs1.clean(key)
        try:
            answer = self._client.get(f"/masterdata/sync/{cleaned}/preview")
        except OpenEpcisError as exc:
            if exc.status == 404:
                return None
            raise
        return self._gs1_record(cleaned, answer or {})

    def import_from_gs1(self, key: str) -> bool:
        """Store what GS1 Germany knows about a key under the caller's tenant.

        Writes a product, place or organization, whichever the key is. Single
        attempt, as every ``POST`` through the client: the server answers what
        it did, and a repeat after a lost answer is harmless but not ours to
        decide.

        :returns: ``True`` when the record was stored, ``False`` when GS1 does
            not know the key.
        """
        cleaned = gs1.clean(key)
        try:
            self._client.post(f"/masterdata/sync/{cleaned}")
        except OpenEpcisError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    @staticmethod
    def _gs1_record(key: str, answer: dict[str, Any]) -> Gs1Record:
        def part(name: str) -> dict[str, Any] | None:
            value = answer.get(name)
            return dict(value) if isinstance(value, dict) else None

        return Gs1Record(
            key=str(answer.get("key") or key),
            key_type=str(answer.get("type") or ""),
            product=part("product"),
            organization=part("organization"),
            place=part("place"),
        )

    # -- Bulk onboarding -----------------------------------------------------

    def bulk_products(self, rows: Iterable[dict[str, Any]]) -> BulkReport:
        """Load products in bulk. Creates only; see the module docstring.

        Rows are dicts keyed by the bulk column names of the vocabulary
        manifest (:func:`~openepcis_client.masterdata.vocabulary.bulk_columns`).
        Unknown keys are dropped; a row whose GTIN is not a valid GS1 key is
        refused here and reported, because the server would fail it one opaque
        row at a time.
        """
        return self._bulk(vocabulary.kind("PRODUCT"), rows)

    def bulk_organizations(self, rows: Iterable[dict[str, Any]]) -> BulkReport:
        """Load organizations in bulk. Same semantics as :meth:`bulk_products`."""
        return self._bulk(vocabulary.kind("ORGANIZATION"), rows)

    def _bulk(self, kind: vocabulary.Kind, rows: Iterable[dict[str, Any]]) -> BulkReport:
        columns = vocabulary.bulk_columns(kind.name)
        failures: list[BulkRowError] = []
        sendable: list[tuple[int, dict[str, Any]]] = []

        total = 0
        for position, row in enumerate(rows, start=1):
            total = position
            key = gs1.clean(str(row.get(kind.key_term) or ""))
            problem = gs1.problem_with(key, kind.key_type)
            if problem:
                failures.append(
                    BulkRowError(
                        row=position,
                        code=INVALID_KEY,
                        message=f"not a valid {kind.key_type} ({problem.fault})",
                    )
                )
                continue
            sendable.append((position, {**row, kind.key_term: key}))

        accepted = 0
        duplicates = 0
        for start in range(0, len(sendable), CHUNK_ROWS):
            chunk = sendable[start : start + CHUNK_ROWS]
            answer = self._client.post_file(
                kind.bulk_endpoint,
                "openepcis-import.csv",
                self._csv(columns, [row for _, row in chunk]),
                form={"format": "csv"},
            )
            accepted += int(answer.get("successCount") or 0)
            for problem_row in answer.get("errors") or []:
                code = str(problem_row.get("errorCode") or "")
                if code in DUPLICATE_CODES:
                    duplicates += 1
                    continue
                # The server numbers rows per upload; map back to the caller's
                # numbering so the report survives chunking.
                sent_index = int(problem_row.get("rowNumber") or 0)
                position = chunk[sent_index - 1][0] if 1 <= sent_index <= len(chunk) else 0
                failures.append(
                    BulkRowError(
                        row=position,
                        code=code,
                        message=str(problem_row.get("errorMessage") or ""),
                    )
                )

        return BulkReport(
            total=total, accepted=accepted, duplicates=duplicates, failures=tuple(failures)
        )

    @staticmethod
    def _csv(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buffer.getvalue().encode("utf-8")

    # -- Classification ------------------------------------------------------

    def search_gpc(self, query: str, level: str = "BRICK", size: int = 40) -> list[GpcNode]:
        """Search the GS1 Global Product Classification by free text."""
        nodes = self._client.get(
            "/gpc/search", params={"q": query.strip(), "level": level, "size": size}
        )
        return [
            GpcNode(
                code=str(node.get("code")),
                title=str(node.get("title") or ""),
                definition=str(node.get("definition") or ""),
                # The field has appeared under both names in live answers.
                lineage=str(node.get("lineage") or node.get("path") or ""),
            )
            for node in (nodes or [])
            if node.get("code")
        ]
