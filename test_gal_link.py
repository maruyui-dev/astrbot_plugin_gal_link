import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from astrbot.api.message_components import At, Plain

plugin = importlib.import_module("data.plugins.astrbot_plugin_gal_link.main")


class GalLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_message_gate_and_reply_state(self):
        handler = next(
            (obj for obj in vars(plugin).values() if hasattr(obj, "on_group_message")),
            None,
        )
        self.assertIsNotNone(handler, "The template has no Gal Link message handler")
        sources = importlib.import_module(
            "data.plugins.astrbot_plugin_gal_link.sources"
        )
        config = {"touchgal_api_token": ""}
        star = handler(SimpleNamespace(), config)
        self.addAsyncCleanup(star.terminate)
        star.titles.find = AsyncMock(return_value=[])
        star.sites.search = AsyncMock(
            return_value=(
                [
                    sources.Resource("测试站", f"文件 {i}", f"https://example.org/{i}")
                    for i in range(4)
                ],
                0,
            )
        )

        def event(text, sender="42", target="123", handlers=()):
            return SimpleNamespace(
                unified_msg_origin="test:GroupMessage:1",
                get_sender_id=lambda: sender,
                get_self_id=lambda: "123",
                get_platform_name=lambda: "aiocqhttp",
                get_messages=lambda: [At(qq=target), Plain(text)],
                get_extra=lambda key, default=None: (
                    handlers if key == "handlers_parsed_params" else default
                ),
                plain_result=lambda message: SimpleNamespace(use_t2i=lambda _: message),
            )

        async def reply(message):
            return [result async for result in star.on_group_message(message)]

        for text in (
            "你好",
            "我想玩桌游，你有什么推荐吗？",
            "/查找 千恋万花",
            "1",
            "下一页",
        ):
            self.assertEqual(await reply(event(text)), [])
        self.assertEqual(await reply(event("我想玩千恋万花", target="999")), [])
        self.assertEqual(await reply(event("查找千恋万花", handlers={"other": {}})), [])
        star.sites.search.assert_not_awaited()

        first = await reply(event("我想玩千恋万花"))
        self.assertIn("第 1 / 2 页", first[0])
        self.assertEqual(await reply(event("下一页", sender="43")), [])
        second = await reply(event("下一页"))
        self.assertIn("已到最后一页", second[0])
        self.assertEqual(await reply(event("下一页")), [])
        star.sites.search.assert_awaited_once()

    async def test_correction_candidates_are_validated_and_selection_searches_three_languages(
        self,
    ):
        titles = importlib.import_module("data.plugins.astrbot_plugin_gal_link.titles")
        first = titles.Work(
            "v1",
            "星空列车与白的旅行",
            (
                "星空列车与白的旅行",
                "星空鉄道とシロの旅",
                "The Starry Sky Train and Shiro's Journey",
            ),
        )
        second = titles.Work("v2", "星空的记忆", ("星空的记忆", "星空のメモリア"))
        context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="test"),
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text='["星空列车与白的旅行", "星空的记忆", "杜撰作品"]',
                )
            ),
        )
        star = plugin.GalLink(context)
        self.addAsyncCleanup(star.terminate)

        async def find(query):
            return [item for item in (first, second) if item.matches(query)]

        star.titles.find = AsyncMock(side_effect=find)
        star.sites.search = AsyncMock(return_value=([], 0))

        message, state = await star.lookup("test", "星空与白", None, implicit=True)
        self.assertIn("1. 星空列车与白的旅行\n2. 星空的记忆", message)
        self.assertIn("@BOT 2", message)
        self.assertNotIn("杜撰作品", message)
        chosen = state.candidates[0]
        message, _ = await star.lookup("test", chosen.title, chosen)
        self.assertIn("暂未找到《星空列车与白的旅行》", message)
        self.assertCountEqual(star.sites.search.await_args.args[0], chosen.names)

    def test_public_netdisk_links_keep_provider_and_query(self):
        sources = importlib.import_module(
            "data.plugins.astrbot_plugin_gal_link.sources"
        )
        text = "百度 https://pan.baidu.com/s/share?pwd=1234。 夸克 https://pan.quark.cn/s/abc。"
        self.assertEqual(
            sources.share_links(text),
            [
                ("百度网盘", "https://pan.baidu.com/s/share?pwd=1234"),
                ("夸克网盘", "https://pan.quark.cn/s/abc"),
            ],
        )

    async def test_gal_games_distinguishes_direct_and_quark_links(self):
        sources = importlib.import_module(
            "data.plugins.astrbot_plugin_gal_link.sources"
        )

        async def transport(request):
            if request.url.host == "gal.games":
                return httpx.Response(
                    200, text='<h2><a href="/games/11101?q=test">千恋＊万花</a></h2>'
                )
            self.assertEqual(request.url.path, "/v1/games/11101")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "downloads": [
                                {
                                    "name": "千恋万花_PC直装.7z",
                                    "path": "作品/千恋万花_PC直装.7z",
                                    "platforms": [],
                                },
                                {
                                    "name": "千恋万花 Ty",
                                    "description": "Ty 版 https://pan.quark.cn/s/abc",
                                },
                            ]
                        }
                    }
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            results = await sources.Sites(client, "", plugin.logger).library(
                sources.GAL_GAMES,
                "梓澪の妙妙屋",
                ("千恋万花",),
                {"千恋万花"},
            )
        self.assertEqual([item.link_label for item in results], ["下载", "夸克网盘"])
        self.assertEqual(results[0].type_label, "PC直装")
        self.assertTrue(results[0].from_filename)
        self.assertEqual(results[1].type_label, "Ty")

    async def test_partial_failure_keeps_other_hits(self):
        sources = importlib.import_module(
            "data.plugins.astrbot_plugin_gal_link.sources"
        )

        async def transport(request):
            if request.url.host == "www.shinnku.com":
                return httpx.Response(503)
            if (
                request.url.path == "/api/fs/search"
                and request.url.host == "pan.mmpan.top"
            ):
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "data": {
                            "content": [
                                {
                                    "is_dir": False,
                                    "parent": "/游戏/千恋＊万花",
                                    "name": "【PC直装】千恋万花.7z",
                                }
                            ],
                            "total": 1,
                        },
                    },
                )
            if request.url.path == "/api/fs/get":
                return httpx.Response(
                    200, json={"code": 200, "data": {"sign": "test-sign"}}
                )
            if request.url.path == "/api/fs/search":
                return httpx.Response(
                    200, json={"code": 200, "data": {"content": [], "total": 0}}
                )
            return httpx.Response(200, text="<html></html>")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            results, failed = await sources.Sites(client, "", plugin.logger).search(
                ("千恋万花",)
            )
        self.assertEqual(failed, 1)
        self.assertEqual([item.source for item in results], ["喵喵盘"])
        self.assertEqual(results[0].type_label, "PC直装")
        self.assertTrue(results[0].from_filename)
