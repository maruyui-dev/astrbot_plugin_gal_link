import asyncio
import importlib
import json
import unittest
from datetime import datetime
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import httpx

from astrbot.api.message_components import At, Plain

plugin = importlib.import_module("data.plugins.astrbot_plugin_gal_link.main")


class GalLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_reminder_schedule_includes_midnight_and_is_removed_on_unload(self):
        from apscheduler.triggers.cron import CronTrigger

        manager = SimpleNamespace(
            add_basic_job=AsyncMock(
                return_value=SimpleNamespace(job_id="test-reminder")
            ),
            delete_job=AsyncMock(),
        )
        star = plugin.GalLink(SimpleNamespace(cron_manager=manager))
        self.addAsyncCleanup(star.terminate)
        # AstrBot only invokes initialize when the plugin implements it.
        if hasattr(star, "initialize"):
            await star.initialize()
        manager.add_basic_job.assert_awaited_once()
        await star.initialize()
        manager.add_basic_job.assert_awaited_once()
        settings = manager.add_basic_job.await_args.kwargs
        self.assertEqual(settings["timezone"], "Asia/Shanghai")
        self.assertIs(settings["persistent"], False)
        trigger = CronTrigger.from_crontab(
            settings["cron_expression"], timezone=settings["timezone"]
        )
        now = datetime(2026, 11, 20, 6, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        fire = trigger.get_next_fire_time(None, now)
        hours = []
        for _ in range(19):
            hours.append((fire.day, fire.hour, fire.minute))
            fire = trigger.get_next_fire_time(fire, fire)
        self.assertEqual(
            hours, [(20, hour, 0) for hour in range(7, 24)] + [(21, 0, 0), (21, 7, 0)]
        )
        await star.terminate()
        manager.delete_job.assert_awaited_once_with("test-reminder")
        self.assertTrue(star.client.is_closed)

    async def test_reminders_use_whitelisted_groups_and_a_real_self_mention_without_blank_lines(
        self,
    ):
        clients = {}
        platforms = {}
        for platform_id, self_id in (("bot-a", 12345), ("bot-b", 67890)):
            client = SimpleNamespace(
                call_action=AsyncMock(
                    return_value={"user_id": self_id, "nickname": platform_id}
                )
            )
            clients[platform_id] = client
            platforms[platform_id] = SimpleNamespace(
                meta=lambda: SimpleNamespace(name="aiocqhttp"),
                get_client=lambda client=client: client,
            )
        context = SimpleNamespace(get_platform_inst=Mock(side_effect=platforms.get))
        star = plugin.GalLink(
            context,
            {
                "group_broadcast_whitelist": [
                    "bot-a:GroupMessage:10001",
                    "bot-a:GroupMessage:10001",
                    "bot-a:FriendMessage:10002",
                    "bot-b:GroupMessage:20001",
                ]
            },
        )
        self.addAsyncCleanup(star.terminate)
        await star.send_reminders()
        for platform_id, self_id, group_id in (
            ("bot-a", 12345, 10001),
            ("bot-b", 67890, 20001),
        ):
            calls = clients[platform_id].call_action.await_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].kwargs["action"], "get_login_info")
            send = calls[1].kwargs
            self.assertEqual(
                (send["action"], send["group_id"], send["self_id"]),
                ("send_group_msg", group_id, self_id),
            )
            self.assertEqual(
                send["message"][1], {"type": "at", "data": {"qq": str(self_id)}}
            )
            text = "".join(
                part["data"]["text"] if part["type"] == "text" else "@" + platform_id
                for part in send["message"]
            )
            self.assertEqual(len(text.splitlines()), 3)
            self.assertIn(f"例如 @{platform_id} 查找千恋万花", text)
            self.assertNotIn("\n\n", text)
            self.assertIn(
                datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:"), text
            )

    async def test_empty_whitelist_skips_sending_and_one_failure_keeps_other_groups(
        self,
    ):
        client = SimpleNamespace(call_action=AsyncMock())
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: client
        )
        context = SimpleNamespace(get_platform_inst=Mock(return_value=platform))
        config = {"group_broadcast_whitelist": []}
        star = plugin.GalLink(context, config)
        self.addAsyncCleanup(star.terminate)
        await star.send_reminders()
        context.get_platform_inst.assert_not_called()
        client.call_action.side_effect = [
            {"user_id": 12345, "nickname": "test"},
            RuntimeError("test send failure"),
            {"user_id": 12345, "nickname": "test"},
            {"message_id": 1},
        ]
        config["group_broadcast_whitelist"] = [
            "bot:GroupMessage:10001",
            "bot:GroupMessage:10002",
        ]
        await star.send_reminders()
        self.assertEqual(client.call_action.await_count, 4)
        self.assertEqual(client.call_action.await_args.kwargs["group_id"], 10002)

    async def test_sites_run_concurrently_and_fall_back_only_after_chinese_misses(self):
        sources = importlib.import_module(
            "data.plugins.astrbot_plugin_gal_link.sources"
        )
        chinese = ("甜蜜女友",)
        foreign = ("アマカノ", "Amakano")
        calls = {}
        started = asyncio.Event()

        async def query(source, terms, keys):
            calls.setdefault(source, []).append(terms)
            self.assertEqual(keys, {"甜蜜女友", "アマカノ", "amakano"})
            if terms == chinese:
                if len(calls) == 5:
                    started.set()
                await asyncio.wait_for(started.wait(), 1)
                if source not in ("真红小站", "喵喵盘"):
                    return []
            else:
                self.assertEqual(terms, foreign)
            return [sources.Resource(source, source, f"https://example.org/{source}")]

        async def shinnku(terms, keys):
            return await query("真红小站", terms, keys)

        async def library(host, source, terms, keys):
            return await query(source, terms, keys)

        async def openlist(host, source, parent, terms, keys):
            return await query(source, terms, keys)

        async with httpx.AsyncClient() as client:
            sites = sources.Sites(client, "", plugin.logger)
            sites.shinnku = shinnku
            sites.library = library
            sites.openlist = openlist
            found, failed = await sites.search(chinese, fallback_terms=foreign)

        self.assertEqual(failed, 0)
        self.assertEqual(len(found), 5)
        for source, phases in calls.items():
            self.assertEqual(
                phases,
                [chinese] if source in ("真红小站", "喵喵盘") else [chinese, foreign],
            )

    async def test_chinese_alias_is_the_display_name_and_first_search_term(self):
        async def catalog(request):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "v15679",
                            "title": "Amakano",
                            "alttitle": "アマカノ",
                            "titles": [{"lang": "ja", "title": "アマカノ"}],
                            "aliases": ["甜蜜女友"],
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(catalog)) as client:
            work = (await plugin.Titles(client).find("甜蜜女友"))[0]
        self.assertEqual(work.title, "甜蜜女友")
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        star.titles.find = AsyncMock(return_value=[work])
        star.sites.search = AsyncMock(
            return_value=(
                [
                    plugin.Resource(
                        "测试站", "甜蜜女友.zip", "https://example.org/amakano"
                    ),
                ],
                0,
            )
        )
        message, _ = await star.lookup("test", "甜蜜女友", None)
        self.assertTrue(message.startswith("找到《甜蜜女友》"), message)
        self.assertEqual(star.sites.search.await_args.args[0], ("甜蜜女友",))
        self.assertCountEqual(
            star.sites.search.await_args.kwargs["fallback_terms"],
            ("アマカノ", "Amakano"),
        )

    async def test_mention_only_arms_number_selection_for_the_original_user(self):
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        works = (
            plugin.Work("v1", "作品甲", ("作品甲",)),
            plugin.Work("v2", "作品乙", ("作品乙",)),
        )
        key = ("test:GroupMessage:1", "42")
        star.replies[key] = plugin.ReplyState(monotonic() + 600, candidates=works)
        star.lookup = AsyncMock(return_value=("已查询作品乙", None))

        async def send(
            text, sender="42", group="test:GroupMessage:1", target="123", handlers=None
        ):
            components = [Plain(text)]
            if target is not None:
                components.insert(0, At(qq=target))
            event = SimpleNamespace(
                unified_msg_origin=group,
                get_sender_id=lambda: sender,
                get_self_id=lambda: "123",
                get_platform_name=lambda: "aiocqhttp",
                get_messages=lambda: components,
                get_extra=lambda key: (
                    handlers if key == "handlers_parsed_params" else None
                ),
                plain_result=lambda message: SimpleNamespace(use_t2i=lambda _: message),
            )
            if not plugin.MentionedGalRequest().filter(event, {}):
                return []
            return [result async for result in star.on_group_message(event)]

        self.assertEqual(await send("2", target=None), [])
        self.assertEqual(await send(" ", sender="43"), [])
        prompt = await send(" ")
        self.assertEqual(len(prompt), 1)
        self.assertIn("请直接发送作品序号", prompt[0])
        self.assertEqual(await send("2", sender="43", target=None), [])
        self.assertEqual(await send("2", group="test:GroupMessage:2", target=None), [])
        self.assertEqual(await send("2", target="999"), [])
        self.assertEqual(await send("你好", target=None), [])
        self.assertEqual(await send("2", target=None, handlers={"other": {}}), [])
        self.assertIn("1～2", (await send("9", target=None))[0])
        self.assertEqual(await send("2", target=None), ["已查询作品乙"])
        self.assertEqual(star.lookup.await_args.args[2], works[1])
        self.assertEqual(await send("2", target=None), [])
        star.lookup.assert_awaited_once()

        star.replies[key] = plugin.ReplyState(monotonic() + 600, candidates=works)
        await send("")
        star.replies[key].expires_at = monotonic() - 1
        self.assertEqual(await send("2", target=None), [])
        self.assertEqual(await send(""), [])

    async def test_short_queries_offer_dynamic_candidates_before_searching_resources(
        self,
    ):
        def entry(id_, title, *other_titles):
            return {
                "id": id_,
                "title": title,
                "alttitle": None,
                "aliases": [],
                "titles": [
                    {"lang": lang, "title": name}
                    for lang, name in zip(
                        ("zh-Hans", "ja", "en"), (title, *other_titles)
                    )
                ],
            }

        train = entry(
            "v28297",
            "星空列车与白的旅行",
            "星空鉄道とシロの旅",
            "The Starry Sky Train and Shiro's Journey",
        )
        queries = []

        async def catalog(request):
            query = json.loads(request.content)["filters"][2]
            queries.append(query)
            results = {
                "星 白": [train, entry("v12984", "星辰恋曲的白色永恒")],
                "天使": [
                    entry("v61094", "天使"),
                    entry("v40520", "天使☆嚣嚣 RE-BOOT!"),
                    entry("v860", "银河天使"),
                ],
            }
            return httpx.Response(200, json={"results": results.get(query, [])})

        async with httpx.AsyncClient(transport=httpx.MockTransport(catalog)) as client:
            star = plugin.GalLink(SimpleNamespace())
            self.addAsyncCleanup(star.terminate)
            star.titles = plugin.Titles(client)
            star.sites.search = AsyncMock(return_value=([], 1))

            async def send(text, sender="42"):
                event = SimpleNamespace(
                    unified_msg_origin="test:GroupMessage:1",
                    get_sender_id=lambda: sender,
                    get_self_id=lambda: "123",
                    get_platform_name=lambda: "aiocqhttp",
                    get_messages=lambda: [At(qq="123"), Plain(text)],
                    get_extra=lambda key: None,
                    plain_result=lambda message: SimpleNamespace(
                        use_t2i=lambda _: message
                    ),
                )
                if not plugin.MentionedGalRequest().filter(event, {}):
                    return []
                return [result async for result in star.on_group_message(event)]

            for text in (
                "星白",
                "找一下星白",
                "能帮我找一下星白吗",
                "给我一个星白的安装包",
            ):
                with self.subTest(text=text):
                    replies = await send(text)
                    self.assertEqual(len(replies), 1)
                    self.assertIn(
                        "1. 星空列车与白的旅行\n2. 星辰恋曲的白色永恒", replies[0]
                    )
                    self.assertIn("请用 QQ 的 @ 功能选中本机器人", replies[0])
                    self.assertIn("发起查询的群友", replies[0])
                    self.assertNotIn("@BOT", replies[0])
            self.assertIn("星 白", queries)
            self.assertEqual(await send("1", sender="43"), [])
            star.sites.search.assert_not_awaited()
            await send("1")
            self.assertEqual(
                star.sites.search.await_args.args[0],
                ("星空列车与白的旅行",),
            )
            self.assertEqual(
                star.sites.search.await_args.kwargs["fallback_terms"],
                (
                    "星空鉄道とシロの旅",
                    "The Starry Sky Train and Shiro's Journey",
                ),
            )
            star.sites.search.reset_mock()
            replies = await send("天使")
            self.assertIn("2. 天使☆嚣嚣 RE-BOOT!\n3. 银河天使", replies[0])
            star.sites.search.assert_not_awaited()

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
        self.assertIn("再输入「下一页」并发送", first[0])
        self.assertNotIn("@BOT", first[0])
        self.assertEqual(await reply(event("下一页", sender="43")), [])
        second = await reply(event("下一页"))
        self.assertIn("已到最后一页", second[0])
        self.assertEqual(await reply(event("下一页")), [])
        star.sites.search.assert_awaited_once()

    async def test_group_search_uses_the_full_title_without_confirmation(self):
        work = plugin.Work(
            "v19073",
            "千恋＊万花",
            ("千恋＊万花", "Senren Banka"),
        )
        context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="test"),
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(completion_text='["千恋万花"]')
            ),
        )
        star = plugin.GalLink(context)
        self.addAsyncCleanup(star.terminate)
        star.titles.find = AsyncMock(
            side_effect=lambda title: [work] if work.matches(title) else []
        )

        async def search(terms):
            if "Senren Banka" in terms:
                return [
                    plugin.Resource("测试站", work.title, "https://example.org/file")
                ], 0
            return [], 0

        star.sites.search = AsyncMock(side_effect=search)
        for text in ("查找一下千恋万花", "我想玩千元万花", "给我一个千元万华的安装包"):
            with self.subTest(text=text):
                star.sites.search.reset_mock()
                context.llm_generate.reset_mock()
                event = SimpleNamespace(
                    unified_msg_origin="test:GroupMessage:1",
                    get_sender_id=lambda: "42",
                    get_self_id=lambda: "123",
                    get_platform_name=lambda: "aiocqhttp",
                    get_messages=lambda: [At(qq="123"), Plain(text)],
                    get_extra=lambda key: None,
                    plain_result=lambda message: SimpleNamespace(
                        use_t2i=lambda _: message
                    ),
                )
                replies = [result async for result in star.on_group_message(event)]
                self.assertEqual(len(replies), 1)
                self.assertTrue(replies[0].startswith("找到《千恋＊万花》"), replies[0])
                star.sites.search.assert_awaited_once()
                if text.startswith("查找一下"):
                    context.llm_generate.assert_not_awaited()

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
        self.assertIn("对应的作品编号", message)
        self.assertNotIn("@BOT", message)
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
