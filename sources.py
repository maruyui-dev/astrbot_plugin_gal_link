import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
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
    r"(?<![A-Za-z])(?:PC(?:安装直装|免安装|直装|安装版)?|Windows|Android|APK|iOS|IPA|"
    r"Ty(?:rano(?:r|Builder)?)?|KRKR|KR|Kirikiri(?:2)?|Kirikiroid2|吉里吉里(?:2)?|"
    r"ONS(?:cripter)?|JoiPlay|Switch|NS|PSP|"
    r"安卓(?:直装)?|安装直装|免安装(?:版)?|直装(?:版)?|安装版|硬盘版|便携版)(?![A-Za-z])",
    re.IGNORECASE,
)
ARCHIVE = re.compile(
    r"\.(?:(?:7z|rar|zip|apk|ipa|iso|tar|gz|xz|exe)(?:\.[0-9]{1,5})?|[rz][0-9]{2})$",
    re.I,
)
VOLUME_SUFFIXES = (
    (re.compile(r"(?i)(.+?)[ ._-]part([0-9]{1,5})(\.(?:rar|zip|7z))"), "part"),
    (re.compile(r"(?i)(.+?)(\.(?:rar|zip|7z))\.([0-9]{3,5})"), "split"),
    (re.compile(r"(?i)(.+)\.([rz])([0-9]{2})"), "legacy"),
    (re.compile(r"(?i)(.*?[^0-9])([0-9]{2,3})(\.(?:rar|zip|7z))"), "suffix"),
)
PLATFORM_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"(?<![A-Za-z])APK(?![A-Za-z])|安卓直装",
        r"(?<![A-Za-z])(?:PC|Windows)(?![A-Za-z])",
        r"(?<![A-Za-z])(?:iOS|IPA)(?![A-Za-z])",
        r"(?<![A-Za-z])Ty(?:rano(?:r|Builder)?)?(?![A-Za-z])",
        r"(?<![A-Za-z])(?:KRKR|KR|Kirikiri2?|Kirikiroid2)(?![A-Za-z])|吉里吉里2?",
        r"(?<![A-Za-z])ONS(?:cripter)?(?![A-Za-z])",
    )
)
SHARE_URL = re.compile(r'https://[^\s<>"「」『』【】。；，！？]+')
ARCHIVE_PASSWORD = re.compile(
    r"(?:(?:解压(?:缩)?|压缩)\s*(?:密码|口令|码|密钥)|(?:archive|extraction)\s+password)"
    r"(?:\*\*)?\s*(?:[:：=]|是|为)\s*"
    r'(?:[`"“「](?P<quoted>[^`"”」\r\n]{1,100})[`"”」]|'
    r'(?P<plain>[^\s`"“”「」<>,;，；。！？、\[\]（）]{1,100}))',
    re.IGNORECASE,
)
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
    link_label: str = "下载"
    volumes: tuple["Resource", ...] = ()
    missing_volumes: bool = False
    archive_passwords: tuple[str, ...] = ()


def archive_passwords(note: str) -> tuple[str, ...]:
    """Read explicitly labelled archive passwords from a public resource note.

    Args:
        note: Plain text, Markdown or HTML published by the resource site.

    Returns:
        Distinct archive passwords in the site's order.
    """
    if re.search(r"<[a-zA-Z][^>]*>", note):
        note = BeautifulSoup(note, "html.parser").get_text(" ", strip=True)
    passwords = {}
    for match in ARCHIVE_PASSWORD.finditer(note):
        value = match["quoted"] or match["plain"]
        if match["plain"] and value.startswith("**") and value.endswith("**"):
            value = value[2:-2]
        if value and not re.match(
            r"^(?:请|详见|见|参考|待补充|未注明|提取码|访问码)", value
        ):
            passwords.setdefault(value, None)
    return tuple(passwords)


def volume_info(name: str) -> tuple[str, str, int] | None:
    """Identify a volume's archive name, naming scheme, and sequence number.

    Args:
        name: The filename supplied by the file listing.

    Returns:
        Part information, or None for an ordinary filename.
    """
    for pattern, scheme in VOLUME_SUFFIXES:
        if match := pattern.fullmatch(name):
            stem, second, third = match.groups()
            if scheme == "split":
                return stem + second, scheme, int(third)
            if scheme == "legacy":
                if second.lower() == "r":
                    return stem + ".rar", scheme, int(third) + 2
                return stem + ".zip", scheme, int(third)
            if scheme == "suffix":
                scheme += str(len(second))
            return stem + third, scheme, int(second)
    return None


def group_volumes(
    files: dict[str, Resource], directory_url: Callable[[str], str] | None = None
) -> list[Resource]:
    """Combine resolved links for each archive in a single directory.

    Args:
        files: Resources indexed by their site-specific file paths.
        directory_url: Optional builder for the corresponding public directory link.

    Returns:
        Standalone resources and grouped volumes in first-seen order.
    """
    groups: dict[tuple[str, str, str], list[tuple[int, str, Resource]]] = {}
    ordinary = {}
    for path, item in files.items():
        folder = path.rpartition("/")[0]
        info = volume_info(item.title)
        if info is None:
            ordinary[(folder, item.title.casefold())] = (path, item)
            continue
        name, scheme, index = info
        groups.setdefault((folder, name.casefold(), scheme), []).append(
            (index, path, item)
        )

    replacements = {}
    consumed = set()
    for (folder, name, scheme), parts in groups.items():
        if scheme.startswith("suffix") and len(parts) < 2:
            continue
        first_path = parts[0][1]
        companion = ordinary.get((folder, name)) if scheme == "legacy" else None
        indices = {index for index, _, _ in parts}
        if companion and name.endswith(".rar"):
            parts.append((1, *companion))
            indices.add(1)
        parts.sort(key=lambda part: part[0])
        if companion and name.endswith(".zip"):
            parts.append((0, *companion))
        volumes = tuple(part for _, _, part in parts)
        first = volumes[0]
        kinds = {part.type_label for part in volumes if part.type_label}
        passwords = {
            part.archive_passwords for part in volumes if part.archive_passwords
        }
        replacements[first_path] = Resource(
            first.source,
            volume_info(files[first_path].title)[0],
            directory_url(folder) if directory_url else first.url,
            next(iter(kinds)) if len(kinds) == 1 else "",
            "目录" if directory_url else first.link_label,
            volumes,
            min(indices) != 1
            or max(indices) != len(indices)
            or (scheme == "legacy" and companion is None),
            archive_passwords=next(iter(passwords)) if len(passwords) == 1 else (),
        )
        consumed.update(path for _, path, _ in parts if path != first_path)
    return [
        replacements.get(path, item)
        for path, item in files.items()
        if path not in consumed
    ]


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

    async def search(
        self,
        terms: tuple[str, ...],
        *,
        fallback_terms: tuple[str, ...] = (),
        platforms: frozenset[int] = frozenset(),
    ) -> tuple[list[Resource], int]:
        """Search enabled sites concurrently with independent deadlines.

        Args:
            terms: Preferred Chinese titles and nicknames of one work.
            fallback_terms: Titles queried only when a site has no preferred matches.
            platforms: Requested platform numbers; empty means unrestricted.

        Returns:
            Public resources and the number of sites that could not be searched.
        """
        # Preserve punctuation variants for remote search, but collapse case variants.
        terms = tuple(dict.fromkeys(terms))
        fallback_terms = tuple(
            term for term in dict.fromkeys(fallback_terms) if term not in terms
        )
        keys = {title_key(term) for term in (*terms, *fallback_terms)} - {""}
        tasks = [
            ("真红小站", self.shinnku, ()),
            ("梓澪の妙妙屋", self.library, (GAL_GAMES, "梓澪の妙妙屋")),
            ("喵盘", self.openlist, (CATCAT, "喵盘", "/GalGame")),
            ("喵喵盘", self.openlist, (MMPAN, "喵喵盘", "/")),
            ("稻荷ACG", self.library, (INARI, "稻荷ACG")),
        ]
        if self.touchgal_token:
            tasks.append(("TouchGal", self.touchgal, ()))

        async def search_site(searcher, args):
            """Query a site with preferred titles before trying fallback titles.

            Args:
                searcher: The site's existing search coroutine.
                args: Site-specific arguments preceding the titles and matching keys.

            Returns:
                The first nonempty phase of resource results.
            """
            error = None
            for phase in (terms, fallback_terms):
                if not phase:
                    continue
                try:
                    matches = await asyncio.wait_for(searcher(*args, phase, keys), 14)
                except (
                    httpx.HTTPError,
                    TimeoutError,
                    ValueError,
                    KeyError,
                    TypeError,
                ) as exc:
                    error = exc
                    continue
                if platforms:
                    matches = [
                        item
                        for item in matches
                        if any(
                            PLATFORM_PATTERNS[index - 1].search(item.type_label)
                            for index in platforms
                        )
                    ]
                if matches:
                    return matches
            if error:
                raise error
            return []

        batches = await asyncio.gather(
            *(search_site(searcher, args) for _, searcher, args in tasks),
            return_exceptions=True,
        )
        successful = []
        failed = 0
        for (source, _, _), batch in zip(tasks, batches, strict=True):
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
                    result.title
                    if result.volumes or result.link_label == "资源页"
                    else "",
                )
                found.setdefault(key, result)
        return list(found.values()), failed

    async def expand_volumes(
        self,
        files: dict[str, str],
        list_folder: Callable[[str], Awaitable[dict[str, str]]],
    ) -> dict[str, str]:
        """Include every known matching sibling for each selected search hit.

        Args:
            files: Matching paths and displayed filenames from the search.
            list_folder: Reader for the site's public directory listing.

        Returns:
            Selected search hits and matching sibling paths.
        """
        selected = dict(list(files.items())[:20])
        wanted: dict[str, set[tuple[str, str]]] = {}
        for path, name in selected.items():
            if info := volume_info(name):
                wanted.setdefault(path.rpartition("/")[0], set()).add(
                    (info[0].casefold(), info[1])
                )
        if not wanted:
            return selected

        listings = await asyncio.gather(
            *(list_folder(folder) for folder in wanted), return_exceptions=True
        )
        available = dict(files)
        for listing in listings:
            if isinstance(listing, (httpx.HTTPError, ValueError, KeyError, TypeError)):
                self.logger.warning(
                    "Gal Link could not list a volume directory (%s)",
                    type(listing).__name__,
                )
            elif isinstance(listing, BaseException):
                raise listing
            else:
                available.update(listing)
        for path, name in available.items():
            keys = wanted.get(path.rpartition("/")[0])
            if keys is None:
                continue
            info = volume_info(name)
            key = (info[0].casefold(), info[1]) if info else (name.casefold(), "legacy")
            if key in keys:
                selected.setdefault(path, name)
        return selected

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
                    files.setdefault(urlsplit(item["href"]).path, title)

        async def list_folder(folder):
            page = await self.request("GET", SHINNKU + folder)
            return {
                path: item.get_text(" ", strip=True)
                for item in BeautifulSoup(page.text, "html.parser").select(
                    'a[href^="/files/"]'
                )
                if (path := urlsplit(item["href"]).path).rpartition("/")[0] == folder
            }

        async def resolve(path, title):
            url = SHINNKU + path
            response = await self.request("GET", url)
            page = BeautifulSoup(response.text, "html.parser")
            passwords = archive_passwords(page.get_text(" ", strip=True))
            for link in page.select("a[href]"):
                if link.get_text(" ", strip=True) == "点击此处下载":
                    target = urljoin(url, link["href"])
                    if target.startswith("https://") and not re.search(r"\s", target):
                        provider = SHARE_HOSTS.get(
                            urlsplit(target).netloc.lower(), "下载"
                        )
                        return path, Resource(
                            "真红小站",
                            title,
                            target,
                            link_label=provider,
                            archive_passwords=passwords,
                        )
            return path, Resource(
                "真红小站", title, url, link_label="资源页", archive_passwords=passwords
            )

        files = await self.expand_volumes(files, list_folder)
        return group_volumes(
            dict(
                await self.gather(resolve(path, title) for path, title in files.items())
            ),
            lambda folder: SHINNKU + folder,
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

        listed_signs = {}
        listed_passwords = {}

        async def list_folder(folder):
            files = {}
            page = 1
            while True:
                response = await self.request(
                    "POST",
                    f"{host}/api/fs/list",
                    json={"path": folder or "/", "page": page, "per_page": 100},
                )
                payload = response.json()
                if payload["code"] != 200:
                    raise ValueError("Directory listing not available")
                data = payload["data"]
                if page == 1:
                    listed_passwords[folder] = archive_passwords(
                        data.get("readme") or ""
                    ) or archive_passwords(data.get("header") or "")
                entries = data["content"] or []
                for entry in entries:
                    name = entry["name"]
                    if not entry["is_dir"] and "/" not in name:
                        path = f"{folder}/{name}"
                        files[path] = name
                        if "sign" in entry:
                            listed_signs[path] = entry["sign"]
                if not entries or page * 100 >= data["total"]:
                    return files
                page += 1

        async def resolve(path, name):
            passwords = listed_passwords.get(path.rpartition("/")[0], ())
            if path in listed_signs:
                sign = listed_signs[path]
            else:
                response = await self.request(
                    "POST", f"{host}/api/fs/get", json={"path": path}
                )
                payload = response.json()
                if payload["code"] != 200:
                    return None
                data = payload["data"]
                sign = data.get("sign")
                passwords = (
                    archive_passwords(data.get("readme") or "")
                    or archive_passwords(data.get("header") or "")
                    or passwords
                )
            # Use OpenList's public download route with the signature it supplied.
            url = f"{host}/d{quote(path, safe='/')}"
            if sign:
                url += f"?sign={quote(sign, safe='')}"
            return path, Resource(source, name, url, archive_passwords=passwords)

        paths = await self.expand_volumes(paths, list_folder)
        resolved = await self.gather(
            resolve(path, name) for path, name in paths.items()
        )
        resources = dict(item for item in resolved if item is not None)
        if (
            host == CATCAT
            and parent == "/GalGame"
            and any(
                path.startswith("/GalGame/") and not item.archive_passwords
                for path, item in resources.items()
            )
        ):
            try:
                response = await self.request(
                    "POST",
                    f"{host}/api/fs/list",
                    json={"path": "/", "page": 1, "per_page": 1},
                    timeout=3,
                )
                payload = response.json()
                if payload["code"] == 200:
                    notes = BeautifulSoup(
                        payload["data"].get("readme") or "", "html.parser"
                    )
                    for entry in notes.find_all("li"):
                        note = entry.get_text(" ", strip=True)
                        if re.match(
                            r"GalGame\s*默认解压密码\s*[:：]", note, re.IGNORECASE
                        ):
                            if passwords := archive_passwords(note):
                                resources = {
                                    path: replace(item, archive_passwords=passwords)
                                    if path.startswith("/GalGame/")
                                    and not item.archive_passwords
                                    else item
                                    for path, item in resources.items()
                                }
                            break
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.logger.debug(
                    "Gal Link could not read the CatCat archive note (%s)",
                    type(exc).__name__,
                )
        return group_volumes(
            resources,
            lambda folder: host + quote(folder or "/", safe="/"),
        )

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
                direct_files = {}
                for item in response.json()["data"]["attributes"]["downloads"] or []:
                    name = item["name"] or title
                    description = item.get("description") or ""
                    passwords = archive_passwords(description)
                    kind = " / ".join(dict.fromkeys(item.get("platforms") or []))
                    links = share_links(description)
                    if links:
                        found.extend(
                            Resource(
                                source,
                                name,
                                link,
                                kind,
                                provider,
                                archive_passwords=passwords,
                            )
                            for provider, link in links
                        )
                    elif item.get("path"):
                        link = f"{GAL_GAMES_API}/files/download?{urlencode({'path': item['path']})}"
                        direct_files[item["path"]] = Resource(
                            source, name, link, kind, archive_passwords=passwords
                        )
                return [*group_volumes(direct_files), *found]
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
                kind = " / ".join(dict.fromkeys(filter(None, labels)))
                content = entry.select_one(".content-text")
                description = content.get_text(" ", strip=True) if content else ""
                passwords = archive_passwords(
                    " ".join(
                        tag.get_text(" ", strip=True)
                        for tag in entry.select(".password-item")
                    )
                ) or archive_passwords(description)
                links = share_links(description)
                if content:
                    for link in content.select("a[href]"):
                        links.extend(share_links(link["href"]))
                if links:
                    found.extend(
                        Resource(
                            source,
                            name,
                            link,
                            kind,
                            link_label=provider,
                            archive_passwords=passwords,
                        )
                        for provider, link in dict.fromkeys(links)
                    )
                else:
                    found.append(
                        Resource(
                            source,
                            name,
                            url,
                            kind,
                            link_label="资源页",
                            archive_passwords=passwords,
                        )
                    )
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
                            link_label="资源页",
                            archive_passwords=archive_passwords(
                                item.get("description") or ""
                            ),
                        )
                    )
        return found
