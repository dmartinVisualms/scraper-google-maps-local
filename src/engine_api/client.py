from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import aiohttp

from src.engine_api.errors import FatalHTTPError, RetryableHTTPError
from src.utils.retry import retry_async

LOGGER = logging.getLogger("src.engine_api.client")

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
    "places.internationalPhoneNumber,places.websiteUri,places.rating,"
    "places.primaryTypeDisplayName,places.googleMapsUri,places.location,nextPageToken"
)


@dataclass
class SearchTextResult:
    places: List[dict] = field(default_factory=list)
    pages: int = 0       # successful page-requests (billable units)
    requests: int = 0    # == pages; retries are not counted (Google does not bill 429/5xx)


async def search_text(
    text_query: str,
    api_key: str,
    *,
    session: aiohttp.ClientSession,
    language_code: str = "es",
    region_code: str = "ES",
    page_size: int = 20,
    max_pages: int = 3,
    included_type: Optional[str] = None,
    retry_attempts: int = 4,
    retry_base_delay: float = 1.0,
) -> SearchTextResult:
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
        "Content-Type": "application/json",
    }
    result = SearchTextResult()
    page_token = None

    for _ in range(max_pages):
        body = {
            "textQuery": text_query,
            "languageCode": language_code,
            "regionCode": region_code,
            "pageSize": page_size,
        }
        if included_type:
            # strict: API devuelve SOLO ese tipo, sin relleno de categorías laxas
            body["includedType"] = included_type
            body["strictTypeFiltering"] = True
        if page_token:
            body["pageToken"] = page_token

        async def _attempt():
            resp = await session.post(ENDPOINT, headers=headers, json=body)
            if resp.status == 429 or resp.status >= 500:
                raise RetryableHTTPError(resp.status)
            return resp.status, await resp.json()

        status, payload = await retry_async(
            _attempt, attempts=retry_attempts, base_delay=retry_base_delay
        )
        if status >= 400:
            raise FatalHTTPError(status, payload)
        result.pages += 1
        result.requests += 1
        places = payload.get("places") or []
        result.places.extend(places)
        page_token = payload.get("nextPageToken")
        if not page_token or not places:
            break

    return result
