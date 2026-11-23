import asyncio
import importlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import httpx

from astrbot.api import AstrBotConfig
from astrbot.api.message_components import At, Plain

plugin = importlib.import_module("data.plugins.astrbot_plugin_gal_link.main")


class GalLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_acknowledgement_is_yielded_before_slow_lookup_starts(self):
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        star.replies[("test:GroupMessage:1", "42")] = plugin.ReplyState(
            monotonic() + 600, title="千恋万花", awaiting_platform=True
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def lookup(*args, **kwargs):
            started.set()
            await release.wait()
            return "查询完成", None

        star.lookup = AsyncMock(side_effect=lookup)
        event = SimpleNamespace(
            unified_msg_origin="test:GroupMessage:1",
            get_sender_id=lambda: "42",
            get_self_id=lambda: "123",
            get_platform_name=lambda: "aiocqhttp",
            get_messages=lambda: [Plain("1 2")],
            get_extra=lambda key: None,
            plain_result=lambda message: SimpleNamespace(use_t2i=lambda _: message),
        )
        stream = star.on_group_message(event)
        pending = None
        try:
            first = await asyncio.wait_for(anext(stream), 1)
            self.assertEqual(first, "收到，正在查找《千恋万花》的资源，请稍等。")
            star.lookup.assert_not_awaited()
            pending = asyncio.create_task(anext(stream))
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(pending.done())
            release.set()
            self.assertEqual(await asyncio.wait_for(pending, 1), "查询完成")
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        finally:
            release.set()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()

    def stub_resources(self, star, resources):
        star.sites.shinnku = AsyncMock(return_value=resources)
        star.sites.library = AsyncMock(return_value=[])
        star.sites.openlist = AsyncMock(return_value=[])
        star.sites.search = AsyncMock(wraps=star.sites.search)

    async def send(
        self,
        star,
        text,
        *,
        sender="42",
        group="test:GroupMessage:1",
        target="123",
        handlers=None,
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
            get_extra=lambda key: handlers if key == "handlers_parsed_params" else None,
            plain_result=lambda message: SimpleNamespace(use_t2i=lambda _: message),
        )
        return [result async for result in star.on_group_message(event)]

    async def test_platform_multiselect_filters_before_pagination_and_scopes_replies(
        self,
    ):
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        work = plugin.Work("v1", "千恋万花", ("千恋万花",))
        star.titles.find = AsyncMock(return_value=[work])
        kinds = ["APK", "PC", "iOS", "Ty", "APK", "Windows", "", "KRKR", "ONS"]
        self.stub_resources(
            star,
            [
                plugin.Resource("测试站", f"资源{i}", f"https://example.org/{i}", kind)
                for i, kind in enumerate(kinds)
            ],
        )
        prompt = await self.send(star, "查找千恋万花")
        self.assertIn("支持多选", prompt[0])
        self.assertIn("3. iOS", prompt[0])
        self.assertIn("7. 不限平台", prompt[0])
        star.sites.search.assert_not_awaited()
        self.assertEqual(await self.send(star, "1 2", sender="43", target=None), [])
        self.assertEqual(
            await self.send(star, "1 2", group="test:GroupMessage:2", target=None), []
        )
        self.assertEqual(await self.send(star, "1 2", target="999"), [])
        self.assertEqual(
            await self.send(star, "1 2", target=None, handlers={"other": {}}), []
        )
        invalid = await self.send(star, "1 8", target=None)
        self.assertIn("1～7", invalid[0])
        star.sites.search.assert_not_awaited()
        first = await self.send(star, "１，２、１", target=None)
        self.assertEqual(len(first), 2)
        self.assertIn("正在查找《千恋万花》", first[0])
        self.assertIn("共 4 条资源 · 第 1 / 2 页", first[-1])
        self.assertIn("APK / Windows", first[-1])
        self.assertEqual(await self.send(star, "下一页", sender="43"), [])
        self.assertEqual(await self.send(star, "下一页", sender="43", target=None), [])
        self.assertEqual(
            await self.send(star, "下一页", group="test:GroupMessage:2", target=None),
            [],
        )
        last = await self.send(star, "下一页", target=None)
        self.assertIn("已到最后一页", last[0])
        combined = "\n".join(first + last)
        for index in (0, 1, 4, 5):
            self.assertIn(f"https://example.org/{index}", combined)
        for index in (2, 3, 6, 7, 8):
            self.assertNotIn(f"https://example.org/{index}", combined)
        self.assertEqual(await self.send(star, "1 2", target=None), [])
        star.sites.search.assert_awaited_once()

    async def test_platform_prompt_reminds_once_and_accepts_zero_to_exit(self):
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        star.sites.search = AsyncMock()
        prompt = await self.send(star, "查找千恋万花")
        self.assertIn("0. 退出", prompt[0])
        self.assertEqual(await self.send(star, "安卓", sender="43", target=None), [])
        self.assertEqual(await self.send(star, "/其他指令", target=None), [])
        self.assertEqual(await self.send(star, "安卓", target="999"), [])
        self.assertEqual(
            await self.send(star, "安卓", target=None, handlers={"other": {}}), []
        )
        reminder = await self.send(star, "安卓", target=None)
        self.assertIn("请发送数字序号", reminder[0])
        self.assertEqual(await self.send(star, "我看看", target=None), [])
        invalid = await self.send(star, "0 1", target=None)
        self.assertIn("单独发送「0」", invalid[0])
        self.assertIn("已退出", (await self.send(star, "0", target=None))[0])
        self.assertEqual(await self.send(star, "1", target=None), [])
        self.assertEqual(await self.send(star, "1"), [])
        await self.send(star, "查找千恋万花")
        self.assertIn("已退出", (await self.send(star, "０"))[0])
        star.sites.search.assert_not_awaited()

    async def test_platform_choice_survives_work_disambiguation(self):
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        works = (
            plugin.Work("v1", "星空列车与白的旅行", ("星空列车与白的旅行",)),
            plugin.Work("v2", "星辰恋曲的白色永恒", ("星辰恋曲的白色永恒",)),
        )
        star.titles.find = AsyncMock(return_value=list(works))
        self.stub_resources(
            star,
            [
                plugin.Resource("测试站", "直装.apk", "https://example.org/apk", "APK"),
                plugin.Resource("测试站", "电脑版.zip", "https://example.org/pc", "PC"),
            ],
        )
        prompt = await self.send(star, "查找星白")
        self.assertIn("支持多选", prompt[0])
        candidates = await self.send(star, "1", target=None)
        self.assertIn("1. 星空列车与白的旅行\n2. 星辰恋曲的白色永恒", candidates[-1])
        star.sites.search.assert_not_awaited()
        self.assertIn("0. 退出", candidates[-1])
        self.assertEqual(await self.send(star, "1", target=None, sender="43"), [])
        result = await self.send(star, "1", target=None)
        self.assertIn("https://example.org/apk", result[-1])
        self.assertNotIn("https://example.org/pc", result[-1])
        star.sites.search.assert_awaited_once()

    async def test_other_platform_chinese_hits_do_not_block_foreign_fallback(self):
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        work = plugin.Work(
            "v1", "千恋万花", ("千恋万花", "Senren Banka"), ("千恋万花",)
        )
        star.titles.find = AsyncMock(return_value=[work])
        phases = []

        async def shinnku(terms, keys):
            phases.append(terms)
            if terms == ("千恋万花",):
                return [plugin.Resource("测试站", "PC", "https://example.org/pc", "PC")]
            return [plugin.Resource("测试站", "APK", "https://example.org/apk", "APK")]

        star.sites.shinnku = shinnku
        star.sites.library = AsyncMock(return_value=[])
        star.sites.openlist = AsyncMock(return_value=[])
        await self.send(star, "查找千恋万花")
        result = await self.send(star, "1", target=None)
        self.assertEqual(len(result), 2)
        self.assertIn("https://example.org/apk", result[-1])
        self.assertNotIn("https://example.org/pc", result[-1])
        self.assertEqual(phases, [("千恋万花",), ("Senren Banka",)])

    async def test_missing_platform_keeps_selection_open_and_all_platforms_can_retry(
        self,
    ):
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        work = plugin.Work("v1", "千恋万花", ("千恋万花",))
        star.titles.find = AsyncMock(return_value=[work])
        self.stub_resources(
            star,
            [
                plugin.Resource("测试站", "电脑版.zip", "https://example.org/pc", "PC"),
                plugin.Resource(
                    "测试站", "未标类型.zip", "https://example.org/unknown"
                ),
            ],
        )
        star.sites.library = AsyncMock(side_effect=httpx.ReadTimeout("test failure"))
        await self.send(star, "查找千恋万花")
        missing = await self.send(star, "1 3", target=None)
        self.assertIn("未找到《千恋万花》的 APK / iOS 资源", missing[-1])
        self.assertIn("7", missing[-1])
        self.assertIn("部分站点暂时无法查询", missing[-1])
        self.assertNotIn("https://example.org/", missing[-1])
        result = await self.send(star, "7,1", target=None)
        self.assertIn("共 2 条资源", result[-1])
        self.assertIn("https://example.org/pc", result[-1])
        self.assertIn("https://example.org/unknown", result[-1])

    async def test_ios_and_emulator_labels_are_detected_without_guessing_unmarked_types(
        self,
    ):
        sources = importlib.import_module(
            "data.plugins.astrbot_plugin_gal_link.sources"
        )
        star = plugin.GalLink(SimpleNamespace())
        self.addAsyncCleanup(star.terminate)
        work = plugin.Work("v1", "千恋万花", ("千恋万花",))
        star.titles.find = AsyncMock(return_value=[work])
        filenames = [
            "千恋万花.ipa",
            "千恋万花_iOS.zip",
            "千恋万花_Tyranor.zip",
            "千恋万花_吉里吉里2.zip",
            "千恋万花_ONScripter.zip",
            "千恋万花_安卓.zip",
            "千恋万花.zip",
        ]
        resources = [
            plugin.Resource(
                "测试站",
                name,
                f"https://example.org/{i}",
                sources.type_label(name),
                True,
            )
            for i, name in enumerate(filenames)
        ]
        self.stub_resources(star, resources)
        for choice, expected in (("3", {0, 1}), ("4", {2}), ("5", {3}), ("6", {4})):
            with self.subTest(choice=choice):
                await self.send(star, "查找千恋万花")
                result = await self.send(star, choice, target=None)
                self.assertIn("文件名标注", result[-1])
                for index in range(len(filenames)):
                    self.assertEqual(
                        f"https://example.org/{index}" in result[-1], index in expected
                    )
        await self.send(star, "查找千恋万花")
        star.replies[("test:GroupMessage:1", "42")].expires_at = monotonic() - 1
        self.assertEqual(await self.send(star, "3", target=None), [])

    async def test_manual_reminder_sends_once_and_persists_reset_before_sending(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "plugin_config.json"
            defaults = {
                "group_broadcast_whitelist": ["bot:GroupMessage:10001"],
                "send_reminder_now": False,
            }
            config = AstrBotConfig(str(config_path), default_config=defaults)
            config["send_reminder_now"] = True
            config.save_config()
            sent = []

            async def call_action(**kwargs):
                if kwargs["action"] == "get_login_info":
                    return {"user_id": 12345, "nickname": "test"}
                # Reset must reach disk before the first group receives anything.
                self.assertIs(
                    json.loads(config_path.read_text(encoding="utf-8-sig"))[
                        "send_reminder_now"
                    ],
                    False,
                )
                sent.append(kwargs)
                return {"message_id": 1}

            client = SimpleNamespace(call_action=call_action)
            platform = SimpleNamespace(
                meta=lambda: SimpleNamespace(name="aiocqhttp"),
                get_client=lambda: client,
            )
            context = SimpleNamespace(
                cron_manager=SimpleNamespace(
                    add_basic_job=AsyncMock(
                        return_value=SimpleNamespace(job_id="test-reminder")
                    ),
                    delete_job=AsyncMock(),
                ),
                get_platform_inst=Mock(return_value=platform),
            )
            star = plugin.GalLink(context, config)
            try:
                await star.initialize()
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]["group_id"], 10001)
                self.assertIs(config["send_reminder_now"], False)
                await star.initialize()
                self.assertEqual(len(sent), 1)
            finally:
                await star.terminate()

            reloaded_config = AstrBotConfig(str(config_path), default_config=defaults)
            reloaded = plugin.GalLink(context, reloaded_config)
            try:
                await reloaded.initialize()
                self.assertEqual(len(sent), 1)
            finally:
                await reloaded.terminate()

    async def test_manual_reminder_with_empty_whitelist_resets_without_sending(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "plugin_config.json"
            config = AstrBotConfig(
                str(config_path),
                default_config={
                    "group_broadcast_whitelist": [],
                    "send_reminder_now": True,
                },
            )
            context = SimpleNamespace(
                cron_manager=SimpleNamespace(
                    add_basic_job=AsyncMock(
                        return_value=SimpleNamespace(job_id="test-reminder")
                    ),
                    delete_job=AsyncMock(),
                ),
                get_platform_inst=Mock(),
            )
            star = plugin.GalLink(context, config)
            try:
                await star.initialize()
                self.assertIs(
                    json.loads(config_path.read_text(encoding="utf-8-sig"))[
                        "send_reminder_now"
                    ],
                    False,
                )
                context.get_platform_inst.assert_not_called()
            finally:
                await star.terminate()

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

    async def test_mention_only_repeats_work_selection_instructions(self):
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
        self.assertEqual(
            await send("2", target=None),
            ["收到，正在查找《作品乙》的资源，请稍等。", "已查询作品乙"],
        )
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
                    self.assertIn("支持多选", replies[0])
                    replies = await send("7")
                    self.assertEqual(len(replies), 2)
                    self.assertIn(
                        "1. 星空列车与白的旅行\n2. 星辰恋曲的白色永恒", replies[-1]
                    )
                    self.assertIn("直接回复作品序号", replies[-1])
                    self.assertIn("发起查询的群友", replies[-1])
                    self.assertNotIn("@BOT", replies[-1])
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
            self.assertIn("支持多选", replies[0])
            replies = await send("7")
            self.assertIn("2. 天使☆嚣嚣 RE-BOOT!\n3. 银河天使", replies[-1])
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
        star.titles.find = AsyncMock(
            return_value=[plugin.Work("v1", "千恋万花", ("千恋万花",))]
        )
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

        prompt = await reply(event("我想玩千恋万花"))
        self.assertIn("支持多选", prompt[0])
        star.sites.search.assert_not_awaited()
        first = await reply(event("7"))
        self.assertIn("第 1 / 2 页", first[-1])
        self.assertIn("直接发送「下一页」", first[-1])
        self.assertNotIn("@BOT", first[-1])
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

        async def search(terms, *, platforms=frozenset()):
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
                self.assertIn("支持多选", replies[0])
                replies = await self.send(star, "7", target=None)
                self.assertEqual(len(replies), 2)
                self.assertTrue(
                    replies[-1].startswith("找到《千恋＊万花》"), replies[-1]
                )
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
        self.assertIn("直接回复作品序号", message)
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
