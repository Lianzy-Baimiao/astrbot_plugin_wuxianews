# -*- coding: utf-8 -*-
"""天刀公告 main.py 冒烟测试：桩掉 astrbot 把插件类拉起来，测面板接线与推送逻辑。

    python tests/test_wuxianews_main.py
全绿打印 OK。

覆盖：import 健全性 / 11 个面板接口注册 / 群名记录（事件自带 + 后台问平台）/
刷新群列表 / 一次推送的去重与强制重发 / _tick 节流 / terminate 落盘。
"""
import asyncio
import json
import os
import sys
import tempfile
import types

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PLUGIN_ROOT)


class _Logger:
    def info(self, *a, **k):
        pass

    warning = error = debug = info


class _EventMessageType:
    ALL = "all"


def _passthrough(*d_args, **d_kwargs):
    def deco(func):
        return func

    return deco


class _Star:
    def __init__(self, context=None):
        self.context = context

    async def html_render(self, tmpl, data, options=None):
        return "http://t2i/card.png"


_FILTER = types.SimpleNamespace(
    regex=_passthrough,
    event_message_type=_passthrough,
    on_astrbot_loaded=_passthrough,
    EventMessageType=_EventMessageType,
)

_DATA_ROOT = tempfile.mkdtemp(prefix="wxnews-main-")


def _install_astrbot_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = _FILTER
    event_mod.AstrMessageEvent = object

    class _MessageChain:
        def message(self, text):
            return self

        def url_image(self, url):
            return self

        def file_image(self, path):
            return self

    event_mod.MessageChain = _MessageChain

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name="": _DATA_ROOT
    )

    core = types.ModuleType("astrbot.core")
    core_utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: _DATA_ROOT
    core.utils = core_utils
    core_utils.astrbot_path = path_mod

    api.star = star_mod
    api.event = event_mod
    astrbot.api = api
    astrbot.core = core

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.utils"] = core_utils
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod


_install_astrbot_stub()

from wxnews import page as page_mod  # noqa: E402
import main as wx_main  # noqa: E402


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class FakeQuery:
    def __init__(self, data):
        self._data = {k: str(v) for k, v in (data or {}).items()}

    def get(self, key, default=""):
        return self._data.get(key, default)


class FakeRequest:
    def __init__(self, query=None, body=None, method="GET"):
        self.query = FakeQuery(query)
        self.args = self.query
        self.method = method
        self._body = body

    async def json(self, default=None):
        return self._body if self._body is not None else default


class FakeContext:
    def __init__(self, platform_insts=None, send_ok=True):
        self.routes = []
        self._insts = list(platform_insts or [])
        # 真实 AstrBot 两套取法都有，这里保持一致
        self.platform_manager = types.SimpleNamespace(
            platform_insts=self._insts,
            get_insts=lambda: list(self._insts),
        )
        self.sent = []
        self.send_ok = send_ok

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, tuple(methods), desc))

    async def send_message(self, umo, chain):
        if not self.send_ok:
            raise RuntimeError("发送失败")
        self.sent.append(umo)


class FakeClient:
    def __init__(self, group_info=None, group_list=None, fail=False):
        self._info = group_info or {}
        self._list = group_list or []
        self._fail = fail
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self._fail:
            raise RuntimeError("平台炸了")
        return self._list if action == "get_group_list" else self._info


class FakePlatformInst:
    def __init__(self, platform_id, client, adapter="aiocqhttp"):
        self._pid = platform_id
        self._client = client
        self._adapter = adapter

    def meta(self):
        return types.SimpleNamespace(id=self._pid, name=self._adapter)

    def get_client(self):
        return self._client


def make_event(text="天刀公告列表", umo="napcat:GroupMessage:1001", gid="1001", group_name=None, bot=None):
    ev = types.SimpleNamespace(
        message_str=text,
        unified_msg_origin=umo,
        message_obj=types.SimpleNamespace(
            group_id=gid,
            group=types.SimpleNamespace(group_id=gid, group_name=group_name),
        ),
    )
    ev.get_platform_id = lambda: "napcat"
    if bot is not None:
        ev.bot = bot
    return ev


def make_plugin(platform_insts=None, **cfg):
    """新数据目录 + 假 context + 真插件实例；返回插件、context、数据目录。"""
    data_dir = tempfile.mkdtemp(prefix="wxnews-case-")
    wx_main.DATA_DIR = None
    wx_main.get_astrbot_data_path = lambda: data_dir
    ctx = FakeContext(platform_insts=platform_insts)
    plugin = wx_main.WuxiaNewsPlugin(ctx, cfg)
    return plugin, ctx, plugin.data_dir


def run(coro):
    return asyncio.run(coro)


NEWS_ITEM = {
    "tag": "公告",
    "title": "9月26日例行维护公告",
    "time": "2026-09-26",
    "url": "http://wuxia.qq.com/news/1.shtml",
}


def patch_pipeline(plugin):
    """把网络相关的几个模块函数换成本地桩，只测推送流程本身。"""
    calls = {"send_text": [], "send_img": []}
    wx_main.fetch_news_list = _async([NEWS_ITEM])
    wx_main.pick_latest = lambda items: dict(NEWS_ITEM)
    wx_main.fetch_summary_cached = _async("这是摘要")

    async def _news_image(it, summary):
        return None, ""

    async def _send_text_to(umo, text):
        calls["send_text"].append((umo, text))

    async def _send_img_to(umo, url):
        calls["send_img"].append((umo, url))

    plugin._news_image = _news_image
    plugin._send_text_to = _send_text_to
    plugin._send_img_to = _send_img_to
    return calls


def _async(value):
    async def _inner(*a, **k):
        return value

    return _inner


def route_paths(ctx):
    return [path for path, *_ in ctx.routes]


# ---------------------------------------------------------------------------
# import 与面板接线
# ---------------------------------------------------------------------------


def test_import_and_routes():
    plugin, ctx, _ = make_plugin()
    assert wx_main.WuxiaNewsPlugin is not None
    assert page_mod.PLUGIN_NAME == "astrbot_plugin_wuxianews"
    paths = route_paths(ctx)
    for suffix in (
        "/page/meta",
        "/page/status",
        "/page/groups",
        "/page/groups/save",
        "/page/push-now",
        "/page/records",
        "/page/records/reset",
        "/page/shots",
        "/page/shots/clear",
        "/page/cache/clear",
        "/page/config/save",
    ):
        assert f"/astrbot_plugin_wuxianews{suffix}" in paths, suffix
    assert plugin.page.plugin is plugin
    assert plugin.page.data_dir == plugin.data_dir
    assert plugin.groups.path.name == "groups.json"


def test_push_groups_normalizes_bare_ids():
    plugin, _, _ = make_plugin(news_groups=["1001", "napcat:GroupMessage:1002", "1001"])
    assert plugin._push_groups() == [
        "aiocqhttp:GroupMessage:1001",
        "napcat:GroupMessage:1002",
    ]


def test_bare_group_number_gets_routable_instance_id():
    """裸群号要补成**实例 id**：写 aiocqhttp 这种适配器类型名的话 send_message 匹配不到。"""
    insts = [
        FakePlatformInst("webchat", None, adapter="webchat"),
        FakePlatformInst("napcat", None, adapter="aiocqhttp"),
    ]
    plugin, _, _ = make_plugin(platform_insts=insts, news_groups=["1001"])
    assert plugin._bare_id_platform() == "napcat"
    assert plugin._push_groups() == ["napcat:GroupMessage:1001"]

    # 只有 webchat 时也不能写死 aiocqhttp
    only_web = [FakePlatformInst("webchat", None, adapter="webchat")]
    plugin2, _, _ = make_plugin(platform_insts=only_web, news_groups=["1001"])
    assert plugin2._bare_id_platform() == "webchat"

    # 探不到平台：保持旧行为（回落 aiocqhttp）
    plugin3, _, _ = make_plugin(news_groups=["1001"])
    assert plugin3._bare_id_platform() == "aiocqhttp"


def test_plugin_status_route_works_end_to_end():
    plugin, _, _ = make_plugin(news_groups=["1001"])
    run(plugin.on_any_message(make_event(group_name="天刀交流群")))
    page_mod.request = FakeRequest()
    resp = run(plugin.page.get_status())
    page_mod.request = None
    payload = resp["data"]
    assert payload["status"] == "ok"
    assert payload["data"]["groups"][0]["label"] == "天刀交流群（1001）"


# ---------------------------------------------------------------------------
# 群名记录
# ---------------------------------------------------------------------------


def test_remember_group_from_event():
    plugin, _, _ = make_plugin()
    run(plugin.on_any_message(make_event(group_name="天刀交流群")))
    assert plugin.groups.name_of("napcat:GroupMessage:1001") == "天刀交流群"
    # 落盘了（面板不用等新消息）
    assert plugin.groups.path.is_file()
    # 事件不带群名时保持「群 群号」，不写空名
    run(plugin.on_any_message(make_event(umo="napcat:GroupMessage:2002", gid="2002")))
    assert plugin.groups.name_of("napcat:GroupMessage:2002") == ""
    assert plugin.groups.label("napcat:GroupMessage:2002") == "群 2002"


def test_learn_group_name_from_platform():
    plugin, _, _ = make_plugin()
    client = FakeClient(group_info={"group_id": 1001, "group_name": "天刀交流群", "member_count": 300})
    run(plugin._learn_group_name(client, "napcat:GroupMessage:1001", "1001", "napcat"))
    assert client.calls[0] == ("get_group_info", {"group_id": 1001})
    assert plugin.groups.name_of("napcat:GroupMessage:1001") == "天刀交流群"
    # 平台失败：静默
    bad = FakeClient(fail=True)
    run(plugin._learn_group_name(bad, "napcat:GroupMessage:1003", "1003", "napcat"))
    assert plugin.groups.name_of("napcat:GroupMessage:1003") == ""


def test_refresh_group_names_from_platform():
    client = FakeClient(
        group_list=[
            {"group_id": 1001, "group_name": "天刀交流群", "member_count": 300},
            {"group_id": 2002, "group_name": "活动群"},
        ]
    )
    plugin, _, _ = make_plugin(platform_insts=[FakePlatformInst("napcat", client)])
    changed = run(plugin.refresh_group_names(force=True))
    assert changed == 2
    assert plugin.groups.name_of("napcat:GroupMessage:2002") == "活动群"
    # 节流：刚刷过再刷不带 force 就不打接口了
    assert run(plugin.refresh_group_names()) == 0
    assert len(client.calls) == 1
    # 平台接口炸了也不抛
    bad, _, _ = make_plugin(platform_insts=[FakePlatformInst("napcat", FakeClient(fail=True))])
    assert run(bad.refresh_group_names(force=True)) == 0
    # 没有平台管理器（老版本 AstrBot）也不报错
    plain, _, _ = make_plugin()
    plain.context.platform_manager = types.SimpleNamespace()
    assert run(plain.refresh_group_names(force=True)) == 0


def test_save_config_now_prefers_async():
    plugin, _, _ = make_plugin()

    class Cfg(dict):
        def __init__(self):
            super().__init__()
            self.sync = 0
            self.async_calls = 0

        def save_config(self):
            self.sync += 1

        async def save_config_async(self):
            self.async_calls += 1

    cfg = Cfg()
    plugin.config = cfg
    run(plugin.save_config_now())
    assert cfg.async_calls == 1 and cfg.sync == 0

    cfg2 = Cfg()
    del Cfg.save_config_async
    plugin.config = cfg2
    run(plugin.save_config_now())
    assert cfg2.sync == 1


# ---------------------------------------------------------------------------
# 推送流程
# ---------------------------------------------------------------------------


def test_push_round_no_groups():
    plugin, _, _ = make_plugin()
    result = run(plugin._push_round())
    assert result["reason"] == "no_groups" and result["pushed"] == []
    assert "还没有设置推送群" in result["message"]


def test_push_round_dedup_and_force():
    plugin, _, data_dir = make_plugin(news_groups=["1001", "napcat:GroupMessage:1002"])
    calls = patch_pipeline(plugin)

    first = run(plugin._push_round())
    assert first["reason"] == "ok" and first["title"] == NEWS_ITEM["title"]
    assert sorted(first["pushed"]) == [
        "aiocqhttp:GroupMessage:1001",
        "napcat:GroupMessage:1002",
    ]
    assert len(calls["send_text"]) == 2
    assert "最新公告" in calls["send_text"][0][1] and "这是摘要" in calls["send_text"][0][1]
    # 记录写盘了：每群一条 push:umo
    records = json.loads((data_dir / "push_record.json").read_text(encoding="utf-8"))
    assert set(records) == {
        "push:aiocqhttp:GroupMessage:1001",
        "push:napcat:GroupMessage:1002",
    }
    assert records["push:aiocqhttp:GroupMessage:1001"] == NEWS_ITEM["title"]

    # 再跑一轮：已推过，全部跳过
    second = run(plugin._push_round())
    assert second["reason"] == "up_to_date" and second["pushed"] == []
    assert sorted(second["skipped"]) == sorted(first["pushed"])
    assert len(calls["send_text"]) == 2

    # force：忽略记录重发
    third = run(plugin._push_round(force=True))
    assert third["reason"] == "ok" and len(third["pushed"]) == 2
    assert len(calls["send_text"]) == 4


def test_push_round_target_only():
    plugin, _, _ = make_plugin(news_groups=["1001", "1002"])
    calls = patch_pipeline(plugin)
    result = run(plugin._push_round("1002"))
    assert result["pushed"] == ["aiocqhttp:GroupMessage:1002"]
    assert [umo for umo, _ in calls["send_text"]] == ["aiocqhttp:GroupMessage:1002"]


def test_push_round_fetch_failure():
    plugin, _, _ = make_plugin(news_groups=["1001"])
    patch_pipeline(plugin)

    async def boom(*a, **k):
        raise RuntimeError("网络断了")

    wx_main.fetch_news_list = boom
    result = run(plugin._push_round())
    assert result["reason"] == "fetch_failed" and "网络断了" in result["message"]

    wx_main.fetch_news_list = _async([])
    assert run(plugin._push_round())["reason"] == "no_list"


def test_push_now_bypasses_throttle():
    plugin, _, _ = make_plugin(news_groups=["1001"])
    patch_pipeline(plugin)
    plugin._last_check = 0
    run(plugin.push_now())
    assert plugin._last_check > 0  # 手动推送会刷新节流时间戳
    result = run(plugin.push_now("1001", force=True))
    assert result["pushed"] == ["aiocqhttp:GroupMessage:1001"]


def test_tick_throttle():
    plugin, _, _ = make_plugin(news_groups=["1001"])
    calls = patch_pipeline(plugin)

    run(plugin._tick())
    assert len(calls["send_text"]) == 1  # 第一次会推
    plugin._last_check = 9999999999.0  # 假装刚跑过
    run(plugin._tick())
    assert len(calls["send_text"]) == 1  # 节流窗口内不再跑

    # 没有推送群时直接返回，连时间戳都不动
    empty, _, _ = make_plugin()
    patch_pipeline(empty)
    empty._last_check = 0
    run(empty._tick())
    assert empty._last_check == 0


def test_terminate_flushes_group_names():
    plugin, _, data_dir = make_plugin()
    run(plugin.on_any_message(make_event(group_name="天刀交流群")))
    plugin._scheduler_task = None
    run(plugin.terminate())
    saved = json.loads((data_dir / "groups.json").read_text(encoding="utf-8"))
    assert saved["groups"]["napcat:GroupMessage:1001"]["group_name"] == "天刀交流群"


def test_norm_umo_rewrites_adapter_type_to_instance_id():
    """推送目标按平台实例 id 路由：写成适配器类型名要换成实例 id，否则静默丢消息。"""
    insts = [
        FakePlatformInst("napcat", FakeClient(), adapter="aiocqhttp"),
        FakePlatformInst("qqguan", FakeClient(), adapter="qq_official"),
    ]
    plugin, _, _ = make_plugin(platform_insts=insts)
    # 适配器类型名 → 实例 id
    assert plugin._norm_umo("aiocqhttp:GroupMessage:1001") == "napcat:GroupMessage:1001"
    assert plugin._norm_umo("qq_official:GroupMessage:OPENID") == "qqguan:GroupMessage:OPENID"
    # 已是实例 id / 认不出来的：原样，不误改
    assert plugin._norm_umo("napcat:GroupMessage:1001") == "napcat:GroupMessage:1001"
    assert plugin._norm_umo("telegram:GroupMessage:9") == "telegram:GroupMessage:9"
    # 裸群号：补到 aiocqhttp 类型的实例 id
    assert plugin._norm_umo("1001") == "napcat:GroupMessage:1001"


def main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for name, fn in tests:
        fn()
        print(f"  {name} ok")
    print(f"OK ({len(tests)} tests)")


if __name__ == "__main__":
    main()
