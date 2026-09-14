from __future__ import annotations

from typing import Any

import pytest

from kma import has_next_page, iter_pages, next_page_no
from kma.pagination import PaginationLimitWarning


def _body(
    *, page_no: int, num_of_rows: int, total_count: int, item_count: int = 0
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "totalCount": total_count,
    }
    if item_count:
        body["items"] = {"item": [{"i": i} for i in range(item_count)]}
    return body


def test_has_next_page_true_when_more_rows_remain() -> None:
    assert has_next_page(_body(page_no=1, num_of_rows=10, total_count=25)) is True


def test_has_next_page_false_on_last_page() -> None:
    assert has_next_page(_body(page_no=3, num_of_rows=10, total_count=25)) is False


def test_has_next_page_false_on_missing_or_zero_metadata() -> None:
    assert has_next_page({}) is False
    assert has_next_page(_body(page_no=1, num_of_rows=0, total_count=25)) is False
    assert has_next_page(_body(page_no=0, num_of_rows=10, total_count=25)) is False


def test_has_next_page_tolerates_string_metadata() -> None:
    body = {"pageNo": "1", "numOfRows": "10", "totalCount": "25"}
    assert has_next_page(body) is True


def test_next_page_no_increments_then_stops() -> None:
    assert next_page_no(_body(page_no=1, num_of_rows=10, total_count=25)) == 2
    assert next_page_no(_body(page_no=3, num_of_rows=10, total_count=25)) is None


async def test_iter_pages_follows_metadata_until_last_page() -> None:
    pages = {
        1: _body(page_no=1, num_of_rows=2, total_count=5, item_count=2),
        2: _body(page_no=2, num_of_rows=2, total_count=5, item_count=2),
        3: _body(page_no=3, num_of_rows=2, total_count=5, item_count=1),
    }
    fetched: list[int] = []

    async def fetch(page_no: int) -> dict[str, Any]:
        fetched.append(page_no)
        return pages[page_no]

    collected = [item async for item in iter_pages(fetch)]

    assert fetched == [1, 2, 3]
    assert len(collected) == 3


async def test_iter_pages_respects_max_pages_safety_valve() -> None:
    # totalCount never satisfied -> would loop forever without max_pages.
    async def fetch(page_no: int) -> dict[str, Any]:
        return _body(page_no=page_no, num_of_rows=1, total_count=10_000, item_count=1)

    with pytest.warns(PaginationLimitWarning):
        collected = [item async for item in iter_pages(fetch, max_pages=4)]

    assert len(collected) == 4


async def test_iter_pages_respects_max_items_safety_valve() -> None:
    async def fetch(page_no: int) -> dict[str, Any]:
        return _body(page_no=page_no, num_of_rows=3, total_count=10_000, item_count=3)

    collected = [item async for item in iter_pages(fetch, max_items=5)]

    # stops once items_seen (3 + 3 = 6) >= 5
    assert len(collected) == 2


async def test_iter_pages_validates_arguments() -> None:
    async def fetch(_page_no: int) -> dict[str, Any]:
        return _body(page_no=1, num_of_rows=1, total_count=1)

    with pytest.raises(ValueError):
        [item async for item in iter_pages(fetch, start_page=0)]
    with pytest.raises(ValueError):
        [item async for item in iter_pages(fetch, max_pages=0)]
    with pytest.raises(ValueError):
        [item async for item in iter_pages(fetch, max_items=0)]


async def test_iter_pages_honors_start_page() -> None:
    seen: list[int] = []

    async def fetch(page_no: int) -> dict[str, Any]:
        seen.append(page_no)
        return _body(page_no=page_no, num_of_rows=10, total_count=10, item_count=1)

    [item async for item in iter_pages(fetch, start_page=5)]

    assert seen == [5]
