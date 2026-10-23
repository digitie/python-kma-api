from datetime import datetime

import pytest
from pydantic import ValidationError
from test_client import FakeSession, PagedFakeSession, _error_payload, _paged_payload, _payload

from kma import KmaClient, PaginationReceipt
from kma.exceptions import KmaParseError
from kma.time_utils import KST

WHEN = datetime(2026, 4, 30, 14, 45, tzinfo=KST)
ROW = {"category": "T1H", "obsrValue": "18.4"}


async def test_complete_receipt_counts_every_page_and_keeps_secrets_out():
    session = PagedFakeSession(
        {
            1: _paged_payload([ROW], page_no=1, num_of_rows=1, total_count=2),
            2: _paged_payload(
                [{"category": "REH", "obsrValue": "52"}], page_no=2, num_of_rows=1, total_count=2
            ),
        }
    )
    snapshot = await KmaClient("opaque-secret", session=session).now(nx=60, ny=127, when=WHEN)
    receipt = snapshot.metadata.pagination
    assert receipt == PaginationReceipt(
        expected_total_count=2, received_item_count=2, page_count=2, complete=True
    )
    assert snapshot.temperature == 18.4 and snapshot.humidity == 52
    assert session.requested_pages == [1, 2]
    assert "opaque-secret" not in snapshot.metadata.model_dump_json()
    with pytest.raises(ValidationError):
        receipt.page_count = 100


async def test_missing_metadata_remains_usable_but_never_proves_completeness():
    snapshot = await KmaClient("key", session=FakeSession(_payload([ROW]))).now(
        nx=60, ny=127, when=WHEN
    )
    assert snapshot.temperature == 18.4
    assert snapshot.metadata.pagination == PaginationReceipt(
        expected_total_count=None,
        received_item_count=1,
        page_count=1,
        complete=False,
    )


async def test_missing_page_echo_does_not_claim_complete():
    payload = _paged_payload([ROW], page_no=1, num_of_rows=1, total_count=1)
    del payload["response"]["body"]["pageNo"]
    snapshot = await KmaClient("key", session=FakeSession(payload)).now(nx=60, ny=127, when=WHEN)
    assert not snapshot.metadata.pagination.complete
    assert snapshot.metadata.pagination.expected_total_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("totalCount", True),
        ("totalCount", -1),
        ("totalCount", 1.1),
        ("totalCount", "1.0"),
        ("totalCount", "١"),
        ("totalCount", "9" * 5000),
        ("pageNo", 0),
        ("numOfRows", 0),
        ("numOfRows", "opaque-secret"),
    ],
)
async def test_invalid_counter_metadata_is_a_typed_parse_failure(field, value):
    payload = _paged_payload([ROW], page_no=1, num_of_rows=1, total_count=1)
    payload["response"]["body"][field] = value
    with pytest.raises(KmaParseError) as failure:
        await KmaClient("opaque-secret", session=FakeSession(payload)).now(nx=60, ny=127, when=WHEN)
    assert failure.value.failure_kind == "parse" and not failure.value.retryable
    assert "opaque-secret" not in str(failure.value)


async def test_short_page_fails_before_wasting_another_request():
    session = FakeSession(_paged_payload([ROW], page_no=1, num_of_rows=1000, total_count=1001))
    with pytest.raises(KmaParseError):
        await KmaClient("key", session=session).now(nx=60, ny=127, when=WHEN)
    assert len(session.calls) == 1


async def test_total_count_cannot_change_between_pages():
    session = PagedFakeSession(
        {
            1: _paged_payload([ROW], page_no=1, num_of_rows=1, total_count=2),
            2: _paged_payload([ROW], page_no=2, num_of_rows=1, total_count=3),
        }
    )
    with pytest.raises(KmaParseError):
        await KmaClient("key", session=session).now(nx=60, ny=127, when=WHEN)
    assert session.requested_pages == [1, 2]


async def test_repeated_page_echo_is_rejected():
    session = PagedFakeSession(
        {
            1: _paged_payload([ROW], page_no=1, num_of_rows=1, total_count=2),
            2: _paged_payload([ROW], page_no=1, num_of_rows=1, total_count=2),
        }
    )
    with pytest.raises(KmaParseError):
        await KmaClient("key", session=session).now(nx=60, ny=127, when=WHEN)


async def test_mid_fetch_no_data_cannot_erase_partial_data_as_success():
    session = PagedFakeSession(
        {
            1: _paged_payload([ROW], page_no=1, num_of_rows=1, total_count=2),
            2: _error_payload("03", "NO_DATA"),
        }
    )
    with pytest.raises(KmaParseError):
        await KmaClient("key", session=session).now(nx=60, ny=127, when=WHEN)


async def test_first_page_no_data_has_explicit_zero_receipt():
    snapshot = await KmaClient("key", session=FakeSession(_error_payload("03", "NO_DATA"))).now(
        nx=60, ny=127, when=WHEN
    )
    assert snapshot.metadata.pagination == PaginationReceipt(
        expected_total_count=0,
        received_item_count=0,
        page_count=1,
        complete=True,
    )


def test_inconsistent_complete_receipt_cannot_be_constructed():
    with pytest.raises(ValidationError):
        PaginationReceipt(
            expected_total_count=2, received_item_count=1, page_count=1, complete=True
        )
