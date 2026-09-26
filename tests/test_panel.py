# -*- coding: utf-8 -*-
"""天刀公告面板后端（wxnews/page.py）单测：桩掉 astrbot 直接驱动 handler。

    python tests/test_panel.py
全绿打印 OK。

本机没装 astrbot，所以先把 astrbot.* 塞进 sys.modules（顺带验证 page.py 的「没有
astrbot.api.web 时回落到裸 dict」分支能跑），再用假 request / 假插件驱动每个接口。
"""
import asyncio
import json
import os
import sys
import tempfile
import types

# ---------------------------------------------------------------------------
# astrbot 桩：必须在 import page 之前塞好
# ---------------------------------------------------------------------------
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PLUGIN_ROOT)


class _Logger:
    def info(self, *a, **k):
        pass

    warning = error = debug = info


def _install_astrbot_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


_install_astrbot_stub()

from wxnews import page as page_mod  # noqa: E402
from wxnews.groups import GroupNameResolver  # noqa: E402


# ---------------------------------------------------------------------------
# 假 request / 假插件
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

    async def get_json(self, default=None):
        return self._body if self._body is not None else default


def with_request(req):
    page_mod.request = req


def unwrap(resp):
    payload = resp["data"] if "data" in resp and isinstance(resp.get("data"), dict) else resp
    if "status" in payload:
        return (
            payload["status"] == "ok",
            payload.get("data") or {},
            payload.get("message", ""),
        )
    return False, {}, payload.get("message", "")


class FakeConfig(dict):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class FakeTask:
    def done(self):
        return False


class FakePlugin:
    """够 page.py 用的假插件（_push_groups 直接读 config，与真实插件一致）。"""

    def __init__(self, data_dir, groups_list=None, **cfg):
        self.data_dir = data_dir
        if groups_list is not None:
            cfg["news_groups"] = list(groups_list)
        self.config = FakeConfig(**cfg)
        self.groups = GroupNameResolver(os.path.join(data_dir, "groups.json"))
        self.groups.load()
        self._last_check = 0.0
        self._scheduler_task = None
        self.refresh_calls = []
        self.push_calls = []

    def _push_groups(self):
        return [str(x).strip() for x in (self.config.get("news_groups") or []) if str(x).strip()]

    async def refresh_group_names(self, force=False, interval=300):
        self.refresh_calls.append(force)
        return 3

    async def save_config_now(self):
        self.config.save_config()

    async def push_now(self, target=None, force=False):
        self.push_calls.append((target, force))
        pushed = [target] if target else list(self._push_groups()[:1])
        return {
            "reason": "ok",
            "title": "9月26日例行维护公告",
            "pushed": pushed,
            "skipped": [],
            "message": "已推送",
        }


def make_case(**cfg):
    """一个临时数据目录 + 假插件 + 控制器。"""
    data_dir = tempfile.mkdtemp(prefix="wxnews-panel-")
    plugin = FakePlugin(data_dir, **cfg)
    ctrl = page_mod.NewsPageController(None, plugin, data_dir=data_dir)
    return plugin, ctrl, data_dir


def run(coro):
    return asyncio.run(coro)


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, tuple(methods), desc))


# ---------------------------------------------------------------------------
# 路由与基础
# ---------------------------------------------------------------------------


def test_page_module_without_astrbot_web():
    # 桩里没给 astrbot.api.web：page.py 必须走回落分支且仍可用
    assert page_mod._HAS_WEB_API is False
    resp = page_mod.NewsPageController._ok({"a": 1}, "hi")
    assert resp["data"]["status"] == "ok" and resp["data"]["data"] == {"a": 1}
    err = page_mod.NewsPageController._err("boom", 500)
    assert err["status_code"] == 500


def test_routes_registered():
    ctx = FakeContext()
    ctrl = page_mod.NewsPageController(ctx, None)
    ctrl.register_routes()
    paths = [p for p, *_ in ctx.routes]
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
    assert all(p.startswith("/astrbot_plugin_wuxianews/") for p in paths)


def test_meta_reads_config():
    plugin, ctrl, _ = make_case(shot_enabled=False, shot_max_height=12000, shot_quality=70)
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_meta()))
    assert ok
    assert data["config"] == {
        "shot_enabled": False,
        "shot_max_height": 12000,
        "shot_quality": 70,
    }
    assert data["limits"]["max_push_groups"] == page_mod.MAX_PUSH_GROUPS
    assert data["defaults"]["shot_quality"] == 88


def test_status_counts_and_labels():
    plugin, ctrl, data_dir = make_case(news_groups=["napcat:GroupMessage:1001"])
    plugin.groups.remember("napcat:GroupMessage:1001", group_name="天刀交流群", member_count=200)
    plugin._last_check = 1000.0
    plugin._scheduler_task = FakeTask()
    # 推送记录 + 摘要缓存 + 一张截图
    with open(os.path.join(data_dir, "push_record.json"), "w", encoding="utf-8") as fh:
        json.dump({"push:napcat:GroupMessage:1001": "9月26日例行维护公告"}, fh)
    with open(os.path.join(data_dir, "summary_cache.json"), "w", encoding="utf-8") as fh:
        json.dump({"http://x": {"t": 1, "s": "摘要"}}, fh)
    shot_dir = os.path.join(data_dir, "news_shots")
    os.makedirs(shot_dir, exist_ok=True)
    with open(os.path.join(shot_dir, "abc.jpg"), "wb") as fh:
        fh.write(b"x" * 1234)

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_status()))
    assert ok
    assert data["running"] is True
    assert data["last_check"] == 1000.0 and data["last_check_text"] != "从未"
    assert data["groups"] == [
        {
            "umo": "napcat:GroupMessage:1001",
            "label": "天刀交流群（1001）",
            "group_id": "1001",
            "last_pushed": "9月26日例行维护公告",
        }
    ]
    assert data["record_count"] == 1
    assert data["summary_cache_count"] == 1
    assert data["shot_count"] == 1 and data["shot_bytes"] == 1234
    assert data["shot_enabled"] is True


def test_groups_list_and_refresh():
    plugin, ctrl, _ = make_case(news_groups=["napcat:GroupMessage:1001"])
    plugin.groups.remember("napcat:GroupMessage:1001", group_name="天刀交流群")
    plugin.groups.remember("napcat:GroupMessage:2002", group_name="活动群")
    plugin.groups.remember("napcat:GroupMessage:3003", group_name="没开推送的群")

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_groups()))
    assert ok
    assert data["pushed"] == 1 and data["total"] == 3
    assert plugin.refresh_calls == []  # 没带 refresh 不打平台接口
    by_value = {g["value"]: g for g in data["groups"]}
    assert by_value["napcat:GroupMessage:1001"]["label"] == "天刀交流群（1001）"
    assert by_value["napcat:GroupMessage:1001"]["enabled"] is True
    assert by_value["napcat:GroupMessage:3003"]["enabled"] is False
    assert data["groups"][0]["value"] == "napcat:GroupMessage:1001"  # 已勾选的排前面

    with_request(FakeRequest(query={"refresh": "1"}))
    ok, data, _ = unwrap(run(ctrl.get_groups()))
    assert ok and data["refreshed"] == 3 and plugin.refresh_calls == [True]


def test_save_groups_writes_config():
    plugin, ctrl, _ = make_case(news_groups=["napcat:GroupMessage:1001"])
    plugin.groups.remember("napcat:GroupMessage:2002", group_name="新群")
    plugin.groups.remember("napcat:GroupMessage:1001", group_name="老群")

    with_request(
        FakeRequest(
            body={
                "groups": [
                    "napcat:GroupMessage:2002",
                    "napcat:GroupMessage:1001",
                    "napcat:GroupMessage:2002",  # 重复要被去掉
                    "   ",                        # 空值跳过
                    "乱七八糟",                    # 非 umo / 非群号跳过
                ]
            },
            method="POST",
        )
    )
    ok, data, msg = unwrap(run(ctrl.save_groups()))
    assert ok
    assert data["groups"] == ["napcat:GroupMessage:2002", "napcat:GroupMessage:1001"]
    assert plugin.config["news_groups"] == data["groups"]
    assert plugin.config.save_count == 1
    assert [a["umo"] for a in data["added"]] == ["napcat:GroupMessage:2002"]
    assert data["removed"] == []
    assert "新增 1 个" in msg


def test_save_groups_validation():
    plugin, ctrl, _ = make_case()
    with_request(FakeRequest(body={}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_groups()))
    assert not ok and "groups" in msg

    with_request(FakeRequest(body={"groups": "napcat:123"}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_groups()))
    assert not ok and "数组" in msg

    with_request(
        FakeRequest(body={"groups": list(range(page_mod.MAX_PUSH_GROUPS + 1))}, method="POST")
    )
    ok, _, msg = unwrap(run(ctrl.save_groups()))
    assert not ok and "最多" in msg


def test_push_now_all_and_target():
    plugin, ctrl, _ = make_case(news_groups=["napcat:GroupMessage:1001"])
    plugin.groups.remember("napcat:GroupMessage:1001", group_name="天刀交流群")

    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.push_now()))
    assert ok and msg == "已推送"
    assert plugin.push_calls == [(None, False)]
    assert data["pushed"] == [{"umo": "napcat:GroupMessage:1001", "label": "天刀交流群（1001）"}]

    with_request(
        FakeRequest(body={"umo": "napcat:GroupMessage:1001", "force": True}, method="POST")
    )
    ok, data, _ = unwrap(run(ctrl.push_now()))
    assert ok and plugin.push_calls[-1] == ("napcat:GroupMessage:1001", True)
    assert data["target"]["label"] == "天刀交流群（1001）"


def test_push_now_without_plugin_support():
    _, ctrl, _ = make_case()
    ctrl.plugin = types.SimpleNamespace()  # 老版本插件没有 push_now
    with_request(FakeRequest(body={}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.push_now()))
    assert not ok and "不支持" in msg


def test_records_list_and_reset():
    plugin, ctrl, data_dir = make_case(news_groups=["napcat:GroupMessage:1001"])
    plugin.groups.remember("napcat:GroupMessage:1001", group_name="天刀交流群")
    with open(os.path.join(data_dir, "push_record.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "push:napcat:GroupMessage:1001": "公告A",
                "1001": "公告B",  # 老版本按群号存的老格式
            },
            fh,
        )

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_records()))
    assert ok and data["count"] == 2
    labels = {r["label"] for r in data["records"]}
    assert "天刀交流群（1001）" in labels and "1001" in labels

    # 单群重置：两种 key 都要清掉
    with_request(FakeRequest(body={"umo": "napcat:GroupMessage:1001"}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.reset_records()))
    assert ok and data["removed"] == 2 and "已重置" in msg
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_records()))
    assert data["count"] == 0

    # 全清
    with open(os.path.join(data_dir, "push_record.json"), "w", encoding="utf-8") as fh:
        json.dump({"a": "1", "b": "2"}, fh)
    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.reset_records()))
    assert ok and data["removed"] == 2 and "已清空" in msg


def test_shots_list_and_clear():
    plugin, ctrl, data_dir = make_case()
    shot_dir = os.path.join(data_dir, "news_shots")
    os.makedirs(shot_dir, exist_ok=True)
    for name, size in (("a.jpg", 10), ("b.jpg", 20)):
        with open(os.path.join(shot_dir, name), "wb") as fh:
            fh.write(b"x" * size)

    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_shots()))
    assert ok and data["count"] == 2 and data["bytes"] == 30
    assert all("url" in s and "modified_text" in s for s in data["shots"])
    assert all(s["url"] == "" for s in data["shots"])  # 单测环境没有文件服务

    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.clear_shots()))
    assert ok and data["removed"] == 2 and data["freed"] == 30 and "已删除" in msg
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(ctrl.get_shots()))
    assert data["count"] == 0


def test_clear_summary_cache():
    plugin, ctrl, data_dir = make_case()
    with open(os.path.join(data_dir, "summary_cache.json"), "w", encoding="utf-8") as fh:
        json.dump({"u1": {"t": 1, "s": "a"}, "u2": {"t": 2, "s": "b"}}, fh)
    with_request(FakeRequest(body={}, method="POST"))
    ok, data, msg = unwrap(run(ctrl.clear_cache()))
    assert ok and data["removed"] == 2 and "已清空 2 条" in msg
    assert not os.path.exists(os.path.join(data_dir, "summary_cache.json"))


def test_save_config_validation_and_ok():
    plugin, ctrl, _ = make_case(shot_enabled=True, shot_max_height=5000, shot_quality=88)

    with_request(FakeRequest(body={"shot_quality": 0}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_config()))
    assert not ok and "1-100" in msg
    with_request(FakeRequest(body={"shot_max_height": 99999}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_config()))
    assert not ok and "0-20000" in msg
    with_request(FakeRequest(body={"shot_max_height": "abc"}, method="POST"))
    ok, _, msg = unwrap(run(ctrl.save_config()))
    assert not ok and "整数" in msg

    with_request(
        FakeRequest(
            body={"shot_enabled": False, "shot_max_height": 8000, "shot_quality": 60},
            method="POST",
        )
    )
    ok, data, msg = unwrap(run(ctrl.save_config()))
    assert ok and data == {"shot_enabled": False, "shot_max_height": 8000, "shot_quality": 60}
    assert plugin.config["shot_max_height"] == 8000 and plugin.config.save_count == 1
    assert "已保存" in msg


def test_normalize_umos():
    _, ctrl, _ = make_case()
    assert ctrl._normalize_umos(None) == []
    assert ctrl._normalize_umos(["", "  ", "x" * 201]) == []
    assert ctrl._normalize_umos(["napcat:GroupMessage:1", "napcat:GroupMessage:1"]) == [
        "napcat:GroupMessage:1"
    ]
    assert ctrl._normalize_umos(["123456"]) == ["123456"]
    assert len(ctrl._normalize_umos([str(i) for i in range(500)])) == page_mod.MAX_PUSH_GROUPS


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
