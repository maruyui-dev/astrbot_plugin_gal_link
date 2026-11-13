import asyncio
import logging
import re
from dataclasses import dataclass
from itertools import zip_longest
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .titles import title_key

SHINNKU = "https://www.shinnku.com"
GAL_GAMES = "https://gal.games"
GAL_GAMES_API = "https://api.gal.games/v1"
INARI = "https://inarigal.com"
TOUCHGAL = "https://www.touchgal.ink"
TOUCHGAL_API = "https://developer.touchgal.com/api/v1/games"
CATCAT = "https://catcat.cloud"
MMPAN = "https://pan.mmpan.top"

TYPE_LABEL = re.compile(
    r"(?<![A-Za-z])(?:PC(?:安装直装|免安装|直装|安装版)?|Windows|Android|APK|iOS|"
    r"Ty(?:rano(?:Builder)?)?|KRKR|KR|ONS|JoiPlay|Switch|NS|PSP|"
    r"安卓(?:直装)?|安装直装|免安装(?:版)?|直装(?:版)?|安装版|硬盘版|便携版)(?![A-Za-z])",
    re.IGNORECASE,
)
ARCHIVE = re.compile(r"\.(?:7z|rar|zip|apk|iso|tar|gz|xz|exe)(?:\.\d+)?$", re.I)
SHARE_URL = re.compile(r'https://[^\s<>"「」『』【】。；，！？]+')
SHARE_HOSTS = {
    "pan.baidu.com": "百度网盘",
    "pan.quark.cn": "夸克网盘",
    "drive.uc.cn": "UC 网盘",
    "pan.xunlei.com": "迅雷网盘",
    "www.alipan.com": "阿里云盘",
    "www.aliyundrive.com": "阿里云盘",
    "www.123pan.com": "123 云盘",
    "www.123865.com": "123 云盘",
    "www.123684.com": "123 云盘",
    "cloud.189.cn": "天翼云盘",
}


@dataclass(frozen=True)
class Resource:
    source: str
    title: str
    url: str
    type_label: str = ""
    from_filename: bool = False
    link_label: str = "下载"


def type_label(text: str) -> str:
    return " / ".join(dict.fromkeys(match[0] for match in TYPE_LABEL.finditer(text)))


def share_links(text: str) -> list[tuple[str, str]]:
    links = []
    for match in SHARE_URL.finditer(text):
        url = match[0].rstrip(".,;!?)]}）")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        provider = SHARE_HOSTS.get(parsed.netloc.lower())
        is_share = parsed.path.startswith(("/s/", "/t/")) and len(parsed.path) > 3
        is_baidu_share = (
            provider == "百度网盘"
            and parsed.path in ("/share/init", "/share/link")
            and bool(parsed.query)
        )
        if provider and (is_share or is_baidu_share):
            links.append((provider, url))
    return list(dict.fromkeys(links))


class Sites:
    def __init__(
        self, client: httpx.AsyncClient, touchgal_token: str, logger: logging.Logger
    ):
        self.client = client
        self.touchgal_token = touchgal_token
        self.logger = logger
        self.limit = asyncio.Semaphore(16)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        async with self.limit:
            response = await self.client.request(method, url, **kwargs)
            response.raise_for_status()
            return response

    async def gather(self, requests):
        responses = await asyncio.gather(*requests, return_exceptions=True)
        results = []
        error = None
        for response in responses:
            if isinstance(
                response,
                (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError),
            ):
                error = response
            elif isinstance(response, BaseException):
                raise response
            else:
                results.append(response)
        if not results and error:
            raise error
        return results

    async def search(self, terms: tuple[str, ...]) -> tuple[list[Resource], int]:
        """Search enabled sites concurrently with independent deadlines.

        Args:
            terms: The original search term and known aliases of one work.

        Returns:
            Public resources and the number of sites that could not be searched.
        """
        # Preserve punctuation variants for remote search, but collapse case variants.
        terms = tuple(dict.fromkeys(terms))
        keys = {title_key(term) for term in terms} - {""}
        tasks = [
            ("真红小站", self.shinnku(terms, keys)),
            ("梓澪の妙妙屋", self.library(GAL_GAMES, "梓澪の妙妙屋", terms, keys)),
            ("喵盘", self.openlist(CATCAT, "喵盘", "/GalGame", terms, keys)),
            ("喵喵盘", self.openlist(MMPAN, "喵喵盘", "/", terms, keys)),
            ("稻荷ACG", self.library(INARI, "稻荷ACG", terms, keys)),
        ]
        if self.touchgal_token:
            tasks.append(("TouchGal", self.touchgal(terms, keys)))
        batches = await asyncio.gather(
            *(asyncio.wait_for(task, 14) for _, task in tasks),
            return_exceptions=True,
        )
        successful = []
        failed = 0
        for (source, _), batch in zip(tasks, batches, strict=True):
            if isinstance(batch, asyncio.CancelledError):
                raise batch
            if isinstance(batch, Exception):
                failed += 1
                self.logger.warning(
                    "Gal Link search failed for %s (%s)", source, type(batch).__name__
                )
                continue
            successful.append(batch)
        found = {}
        # Show distinct sources early even when one catalog returns many files.
        for row in zip_longest(*successful):
            for result in row:
                if result is None:
                    continue
                # Several resources on the same page can have different labels.
                key = (
                    result.url,
                    result.title if result.link_label == "资源页" else "",
                )
                found.setdefault(key, result)
        return list(found.values()), failed

    async def shinnku(self, terms, keys):
        pages = await self.gather(
            self.request("GET", f"{SHINNKU}/search", params={"q": term})
            for term in terms
        )
        files = {}
        for page in pages:
            for item in BeautifulSoup(page.text, "html.parser").select(
                'a[href^="/files/"]'
            ):
                title = item.get_text(" ", strip=True)
                if ARCHIVE.search(title) and any(
                    key in title_key(title) for key in keys
                ):
                    files.setdefault(item["href"], title)

        async def resolve(path, title):
            url = SHINNKU + path
            response = await self.request("GET", url)
            page = BeautifulSoup(response.text, "html.parser")
            for link in page.select("a[href]"):
                if link.get_text(" ", strip=True) == "点击此处下载":
                    target = urljoin(url, link["href"])
                    if target.startswith("https://") and not re.search(r"\s", target):
                        provider = SHARE_HOSTS.get(
                            urlsplit(target).netloc.lower(), "下载"
                        )
                        return Resource(
                            "真红小站", title, target, type_label(title), True, provider
                        )
            return Resource("真红小站", title, url, type_label(title), True, "资源页")

        return await self.gather(
            resolve(path, title) for path, title in list(files.items())[:20]
        )

    async def openlist(self, host, source, parent, terms, keys):
        async def lookup(term):
            response = await self.request(
                "POST",
                f"{host}/api/fs/search",
                json={
                    "parent": parent,
                    "keywords": term,
                    "scope": 2,
                    "page": 1,
                    "per_page": 35,
                },
            )
            payload = response.json()
            if payload["code"] != 200:
                raise ValueError("Search not available")
            return payload["data"]["content"] or []

        paths = {}
        for items in await self.gather(lookup(term) for term in terms):
            for item in items:
                name = item["name"]
                if (
                    not item["is_dir"]
                    and ARCHIVE.search(name)
                    and any(key in title_key(name) for key in keys)
                ):
                    paths.setdefault(item["parent"].rstrip("/") + "/" + name, name)

        async def resolve(path, name):
            response = await self.request(
                "POST", f"{host}/api/fs/get", json={"path": path}
            )
            payload = response.json()
            if payload["code"] != 200:
                return None
            sign = payload["data"].get("sign")
            # Use OpenList's public download route with the signature it supplied.
            url = f"{host}/d{quote(path, safe='/')}"
            if sign:
                url += f"?sign={quote(sign, safe='')}"
            return Resource(source, name, url, type_label(name), True)

        return [
            item
            for item in await self.gather(
                resolve(path, name) for path, name in list(paths.items())[:20]
            )
            if item is not None
        ]

    async def library(self, host, source, terms, keys):
        inari = host == INARI
        search_url, parameter = (
            (f"{host}/", "search") if inari else (f"{host}/games", "q")
        )
        pages = await self.gather(
            self.request("GET", search_url, params={parameter: term}) for term in terms
        )
        matched = {}
        for page in pages:
            soup = BeautifulSoup(page.text, "html.parser")
            for link in soup.select(
                'a[href^="/detail/"]' if inari else 'h2 a[href^="/games/"]'
            ):
                path = link["href"].split("?", 1)[0]
                if not re.fullmatch(r"/(?:detail|games)/[0-9]+", path):
                    continue
                labels = (
                    link.select(".title-cn, .title-original, h3, p")
                    if inari
                    else [link]
                )
                for label in labels:
                    title = label.get_text(" ", strip=True)
                    if title_key(title) in keys:
                        matched.setdefault(host + path, title)
                        break

        async def resources(url, title):
            if not inari:
                response = await self.request(
                    "GET", GAL_GAMES_API + url.removeprefix(GAL_GAMES)
                )
                found = []
                for item in response.json()["data"]["attributes"]["downloads"] or []:
                    name = item["name"] or title
                    description = item.get("description") or ""
                    kind = type_label(description) or " / ".join(
                        item.get("platforms") or []
                    )
                    filename_kind = (
                        type_label(name) if not kind and ARCHIVE.search(name) else ""
                    )
                    links = share_links(description)
                    if links:
                        found.extend(
                            Resource(
                                source,
                                name,
                                link,
                                kind or filename_kind,
                                bool(filename_kind),
                                provider,
                            )
                            for provider, link in links
                        )
                    elif item.get("path"):
                        link = f"{GAL_GAMES_API}/files/download?{urlencode({'path': item['path']})}"
                        found.append(
                            Resource(
                                source,
                                name,
                                link,
                                kind or filename_kind,
                                bool(filename_kind),
                            )
                        )
                return found
            response = await self.request("GET", url)
            soup = BeautifulSoup(response.text, "html.parser")
            found = []
            for entry in soup.select(".resource-item[data-resource-id]"):
                heading = entry.select_one(".resource-title-text h3")
                if (
                    heading is None
                    or entry.select_one(".download-btn:not([disabled])") is None
                ):
                    continue
                name = heading.get_text(" ", strip=True)
                labels = [
                    type_label(tag.get_text(" ", strip=True))
                    for tag in entry.select(".resource-meta span[data-slot=base]")
                ]
                kind = " / ".join(dict.fromkeys(filter(None, labels))) or type_label(
                    name
                )
                content = entry.select_one(".content-text")
                links = (
                    share_links(content.get_text(" ", strip=True)) if content else []
                )
                if content:
                    for link in content.select("a[href]"):
                        links.extend(share_links(link["href"]))
                if links:
                    found.extend(
                        Resource(source, name, link, kind, link_label=provider)
                        for provider, link in dict.fromkeys(links)
                    )
                else:
                    found.append(Resource(source, name, url, kind, link_label="资源页"))
            return found

        batches = await self.gather(
            resources(url, title) for url, title in matched.items()
        )
        return [resource for batch in batches for resource in batch]

    async def touchgal(self, terms, keys):
        async def api(path, params=None):
            response = await self.request(
                "GET",
                TOUCHGAL_API + path,
                params=params,
                headers={"Authorization": f"Bearer {self.touchgal_token}"},
            )
            payload = response.json()
            if payload["success"] is not True:
                raise ValueError("TouchGal API request failed")
            return payload["data"]["items"]

        matches = set()
        batches = await self.gather(
            api("/search", {"keyword": term, "limit": 10})
            for term in terms
            if 3 <= len(term) <= 100
        )
        for batch in batches:
            for item in batch:
                if title_key(item["name"]) in keys and re.fullmatch(
                    "[A-Za-z0-9]{8}", item["uniqueId"]
                ):
                    matches.add(item["uniqueId"])
        found = []
        for batch in await self.gather(
            api(f"/{uid}/resources") for uid in sorted(matches)
        ):
            for item in batch:
                url = item["deepLink"]
                if url.startswith(f"{TOUCHGAL}/"):
                    found.append(
                        Resource(
                            "TouchGal",
                            item["name"],
                            url,
                            type_label(item["name"]),
                            link_label="资源页",
                        )
                    )
        return found
