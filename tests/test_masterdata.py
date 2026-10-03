# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""The masterdata service: upsert semantics, bulk reporting, GPC mapping."""

import csv
import io
from typing import Any

import pytest

from openepcis_client.core import gs1
from openepcis_client.core.errors import OpenEpcisError
from openepcis_client.masterdata import InvalidKey, Masterdata
from openepcis_client.masterdata import service as service_module

GTIN = gs1.with_check_digit("401234567890")
GTIN_2 = gs1.with_check_digit("401234567891")
GTIN_3 = gs1.with_check_digit("401234567892")
GLN = gs1.with_check_digit("401234500001")


class StubClient:
    """Records calls; answers post_file from a scripted sequence."""

    def __init__(self, bulk_answers: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.bulk_answers = bulk_answers or []

    def put(self, path: str, payload: Any) -> Any:
        self.calls.append(("PUT", path, payload))
        return {"ok": True}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        return []

    def post_file(self, path: str, filename: str, content: bytes, form: Any = None) -> Any:
        self.calls.append(("POST_FILE", path, content))
        return self.bulk_answers[min(len(self.calls) - 1, len(self.bulk_answers) - 1)]


def masterdata(client: StubClient) -> Masterdata:
    return Masterdata(client)  # type: ignore[arg-type]


def rows_sent(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode())))


class TestUpsert:
    def test_a_product_goes_to_its_key_path_with_the_key_term_set(self) -> None:
        client = StubClient()
        document = {"productName": {"en": "Chair"}}
        masterdata(client).upsert_product(f" {GTIN[:4]}-{GTIN[4:]} ", document)
        verb, path, payload = client.calls[0]
        assert (verb, path) == ("PUT", f"/products/{GTIN}")
        assert payload["gtin"] == GTIN
        # The resolver's schema refuses a product without its GS1 class.
        assert payload["type"] == "Product"
        assert payload["productName"] == {"en": "Chair"}
        assert "gtin" not in document  # the caller's document is not mutated

    def test_a_caller_supplied_type_is_not_overridden(self) -> None:
        client = StubClient()
        masterdata(client).upsert_product(GTIN, {"type": ["Product", "TextileApparel"]})
        assert client.calls[0][2]["type"] == ["Product", "TextileApparel"]

    def test_an_organization_carries_its_gln_term(self) -> None:
        client = StubClient()
        masterdata(client).upsert_organization(GLN, {})
        verb, path, payload = client.calls[0]
        assert (verb, path) == ("PUT", f"/organizations/{GLN}")
        assert payload["globalLocationNumber"] == GLN

    def test_an_invalid_key_never_reaches_the_wire(self) -> None:
        client = StubClient()
        with pytest.raises(InvalidKey) as caught:
            masterdata(client).upsert_product("4012345678", {})
        assert caught.value.problem.kind == "GTIN"
        assert client.calls == []


class TestBulk:
    def test_the_csv_carries_the_manifest_header_and_nothing_else(self) -> None:
        client = StubClient([{"total": 1, "successCount": 1, "errorCount": 0, "errors": []}])
        report = masterdata(client).bulk_products(
            [{"gtin": GTIN, "productName_en": "Chair", "unknownColumn": "dropped"}]
        )
        sent = rows_sent(client.calls[0][2])
        assert list(sent[0].keys()) == [
            "gtin",
            "productName_en",
            "gpcCategoryCode",
            "brandName",
            "countryOfOriginCode",
            "hasBatchLotNumber",
            "hasSerialNumber",
            "isAnonymousAccessAllowed",
        ]
        assert sent[0]["gtin"] == GTIN
        assert report.accepted == 1
        assert report.failures == ()

    def test_an_invalid_key_is_refused_here_not_sent(self) -> None:
        client = StubClient([{"successCount": 1, "errors": []}])
        report = masterdata(client).bulk_products(
            [{"gtin": "not-a-gtin"}, {"gtin": GTIN, "productName_en": "Chair"}]
        )
        assert len(rows_sent(client.calls[0][2])) == 1
        assert report.total == 2
        assert report.accepted == 1
        assert [(f.row, f.code) for f in report.failures] == [(1, "INVALID_KEY")]

    def test_a_duplicate_counts_as_present_not_failed(self) -> None:
        # Bulk loading only creates; a key the catalog already holds is the
        # state the load was aiming for.
        client = StubClient(
            [
                {
                    "successCount": 0,
                    "errors": [
                        {"rowNumber": 1, "errorCode": "DUPLICATE_GTIN", "errorMessage": "held"}
                    ],
                }
            ]
        )
        report = masterdata(client).bulk_products([{"gtin": GTIN}])
        assert report.duplicates == 1
        assert report.failures == ()

    def test_server_row_numbers_survive_chunking_and_local_skips(self) -> None:
        # Three rows: the first is refused here, the remaining two are sent in
        # two chunks of one. The server reports its failure as row 1 of the
        # second upload; the caller must read it as row 3 of the input.
        client = StubClient(
            [
                {"successCount": 1, "errors": []},
                {
                    "successCount": 0,
                    "errors": [{"rowNumber": 1, "errorCode": "MISSING_NAME", "errorMessage": "x"}],
                },
            ]
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(service_module, "CHUNK_ROWS", 1)
            report = masterdata(client).bulk_products(
                [{"gtin": "bad"}, {"gtin": GTIN_2}, {"gtin": GTIN_3}]
            )
        assert [(f.row, f.code) for f in report.failures] == [
            (1, "INVALID_KEY"),
            (3, "MISSING_NAME"),
        ]
        assert report.accepted == 1

    def test_organizations_use_their_own_endpoint_and_key(self) -> None:
        client = StubClient([{"successCount": 1, "errors": []}])
        masterdata(client).bulk_organizations([{"globalLocationNumber": GLN}])
        assert client.calls[0][1] == "/bulk/organizations"
        assert rows_sent(client.calls[0][2])[0]["globalLocationNumber"] == GLN


class TestGpcSearch:
    def test_nodes_are_mapped_and_codeless_ones_dropped(self) -> None:
        client = StubClient()
        client.get = lambda path, params=None: [  # type: ignore[method-assign]
            {"code": "10003269", "title": "Chairs", "definition": "d", "path": "Furniture > ..."},
            {"title": "no code"},
        ]
        nodes = masterdata(client).search_gpc("  chair ")
        assert len(nodes) == 1
        assert nodes[0].code == "10003269"
        assert nodes[0].lineage == "Furniture > ..."


class ReadingClient:
    """Answers GET and POST from a script keyed by path; raises where told to."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, Any]] = []

    def _answer(self, verb: str, path: str, params: Any) -> Any:
        self.calls.append((verb, path, params))
        answer = self.answers.get(path)
        if isinstance(answer, OpenEpcisError):
            raise answer
        if callable(answer):
            return answer(params)
        return answer

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._answer("GET", path, params)

    def post(self, path: str, payload: Any = None) -> Any:
        return self._answer("POST", path, payload)


def not_found(path: str) -> OpenEpcisError:
    return OpenEpcisError("not found", status=404, path=path)


class TestReadingBack:
    def test_an_organization_is_read_by_its_cleaned_gln(self) -> None:
        client = ReadingClient({f"/organizations/{GLN}": {"organizationName": "Acme"}})
        record = masterdata(client).get_organization(f" {GLN[:4]} {GLN[4:]} ")  # type: ignore[arg-type]
        assert record == {"organizationName": "Acme"}
        assert client.calls[0][1] == f"/organizations/{GLN}"

    def test_an_unknown_gln_reads_as_none(self) -> None:
        path = f"/organizations/{GLN}"
        client = ReadingClient({path: not_found(path)})
        assert masterdata(client).get_organization(GLN) is None  # type: ignore[arg-type]

    def test_other_errors_are_not_swallowed(self) -> None:
        path = f"/organizations/{GLN}"
        client = ReadingClient({path: OpenEpcisError("down", status=503, path=path)})
        with pytest.raises(OpenEpcisError):
            masterdata(client).get_organization(GLN)  # type: ignore[arg-type]

    def test_an_invalid_gln_is_refused_before_any_request(self) -> None:
        client = ReadingClient({})
        with pytest.raises(InvalidKey):
            masterdata(client).get_organization(GLN[:-1] + str((int(GLN[-1]) + 1) % 10))  # type: ignore[arg-type]
        assert client.calls == []

    def test_the_walk_follows_the_pages_newest_change_first(self) -> None:
        pages = {
            1: {
                "organizations": [{"globalLocationNumber": "a"}, {"globalLocationNumber": "b"}],
                "totalPages": 2,
            },
            2: {"organizations": [{"globalLocationNumber": "c"}], "totalPages": 2},
        }
        client = ReadingClient({"/organizations": lambda params: pages[params["page"]]})
        walked = list(masterdata(client).iter_organizations(page_size=2))  # type: ignore[arg-type]
        assert [r["globalLocationNumber"] for r in walked] == ["a", "b", "c"]
        first = client.calls[0][2]
        assert first == {"page": 1, "pageSize": 2, "sortBy": "updatedAt", "sortOrder": "desc"}
        assert len(client.calls) == 2

    def test_an_empty_tenant_is_one_request_and_no_records(self) -> None:
        client = ReadingClient({"/organizations": {"organizations": [], "totalPages": 0}})
        assert list(masterdata(client).iter_organizations()) == []  # type: ignore[arg-type]
        assert len(client.calls) == 1


class TestGs1Import:
    def test_a_gln_preview_carries_organization_and_place(self) -> None:
        path = f"/masterdata/sync/{GLN}/preview"
        answer = {
            "key": GLN,
            "type": "GLN",
            "organization": {"organizationName": "Acme"},
            "place": {"physicalLocationName": "Lager"},
        }
        client = ReadingClient({path: answer})
        record = masterdata(client).preview_from_gs1(GLN)  # type: ignore[arg-type]
        assert record is not None
        assert record.key_type == "GLN"
        assert record.organization == {"organizationName": "Acme"}
        assert record.place == {"physicalLocationName": "Lager"}
        assert record.product is None

    def test_a_key_gs1_does_not_know_previews_as_none(self) -> None:
        path = f"/masterdata/sync/{GLN}/preview"
        client = ReadingClient({path: not_found(path)})
        assert masterdata(client).preview_from_gs1(GLN) is None  # type: ignore[arg-type]

    def test_import_reports_whether_anything_was_stored(self) -> None:
        known = f"/masterdata/sync/{GLN}"
        client = ReadingClient({known: {"status": "success"}})
        assert masterdata(client).import_from_gs1(GLN) is True  # type: ignore[arg-type]
        assert client.calls == [("POST", known, None)]

        client = ReadingClient({known: not_found(known)})
        assert masterdata(client).import_from_gs1(GLN) is False  # type: ignore[arg-type]
