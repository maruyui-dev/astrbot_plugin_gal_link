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
        response = await self.client.post(
            "https://api.vndb.org/kana/vn",
            json={
                "filters": ["search", "=", query],
                "fields": "title,alttitle,titles{lang,title},aliases",
                "sort": "searchrank",
                "results": 6,
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
    return len(key) >= 3 and any(
        SequenceMatcher(None, key, title_key(name)).ratio() >= 0.5
        or (len(key) >= 4 and key in title_key(name))
        for name in work.names
    )
