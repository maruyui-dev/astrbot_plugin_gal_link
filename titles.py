import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx


def title_key(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum()
    )


@dataclass(frozen=True)
class Work:
    id: str
    title: str
    names: tuple[str, ...]

    def matches(self, title: str) -> bool:
        key = title_key(title)
        return bool(key) and any(title_key(name) == key for name in self.names)


SHORT_HAN_TITLE = re.compile(r"[\u3400-\u9fff]{2}")


class Titles:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def find(self, query: str) -> list[Work]:
        """Look up a title in VNDB and collect its published title variants.

        Args:
            query: A title or alias taken from the request.

        Returns:
            Works with Chinese, Japanese and English titles where available.
        """
        key = title_key(query)
        short = SHORT_HAN_TITLE.fullmatch(key) is not None
        response = await self.client.post(
            "https://api.vndb.org/kana/vn",
            json={
                "filters": ["search", "=", query],
                "fields": "title,alttitle,titles{lang,title},aliases",
                "sort": "votecount" if short else "searchrank",
                "reverse": short,
                "results": 12 if short else 6,
            },
        )
        response.raise_for_status()
        works = []
        for item in response.json()["results"]:
            titles = {entry["lang"]: entry["title"] for entry in item["titles"]}
            primary = [titles.get(lang) for lang in ("zh-Hans", "ja", "en", "zh-Hant")]
            names = tuple(
                dict.fromkeys(
                    name
                    for name in [
                        *primary,
                        item["title"],
                        item["alttitle"],
                        *item["aliases"],
                    ]
                    if name and len(name) <= 120
                )
            )
            if names:
                works.append(Work(item["id"], names[0], names))
        return works


def search_terms(query: str, work: Work | None) -> tuple[str, ...]:
    names = (query, *work.names) if work else (query,)
    unique: dict[str, str] = {}
    for name in names:
        clean = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip()
        key = title_key(clean)
        if key and clean.casefold() not in unique:
            unique[clean.casefold()] = clean
    return tuple(unique.values())[:6]


def resembles(query: str, work: Work) -> bool:
    key = title_key(query)
    if SHORT_HAN_TITLE.fullmatch(key):
        return any(
            re.search(f"{key[0]}.*{key[1]}", title_key(name)) for name in work.names
        )
    return len(key) >= 3 and any(
        SequenceMatcher(None, key, title_key(name)).ratio() >= 0.5
        or key in title_key(name)
        for name in work.names
    )


def looks_like_typo(query: str, work: Work) -> bool:
    """Check whether the input is close enough to a verified title to auto-search.

    Args:
        query: The title extracted from the user's message.
        work: A work returned by the catalog.

    Returns:
        Whether a title variant differs only by a likely typo.
    """
    key = title_key(query)
    if len(key) < 3:
        return False
    for name in work.names:
        candidate = title_key(name)
        if SequenceMatcher(None, key, candidate).ratio() >= 0.8:
            return True
        # Short Chinese titles can have several mistyped characters while retaining their shape.
        if len(key) == len(candidate) == 4 and re.fullmatch(
            r"[\u3400-\u9fff]{8}", key + candidate
        ):
            if (
                key[0] == candidate[0]
                and sum(a == b for a, b in zip(key, candidate, strict=True)) >= 2
            ):
                return True
    return False
