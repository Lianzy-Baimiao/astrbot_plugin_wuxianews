# -*- coding: utf-8 -*-
"""天刀公告插件的 Web 面板后端接口（AstrBot Plugin Pages）。

路由挂在 ``/astrbot_plugin_wuxianews/page/*`` 下，前端 ``pages/wuxianews-panel/``
通过 ``window.AstrBotPluginPage`` 的 apiGet / apiPost 调用（bridge 会自动补插件名前缀）。

    GET  /page/meta           常量与截图参数（含默认值）
    GET  /page/status         运行状态 + 推送群（带群名 + 上次推的公告）
    GET  /page/groups         会话列表（带群名），refresh=1 时现问平台要群列表
    POST /page/groups/save    保存推送群名单（写回 news_groups 配置）
    POST /page/push-now       立刻检查并推送（全部群 / 指定群，可强制重发）
    GET  /page/records        推送去重记录
    POST /page/records/reset  清空推送记录（全部或某个群）
    GET  /page/shots          公告截图缓存列表（带预览地址）
    POST /page/shots/clear    清理截图缓存
    POST /page/cache/clear    清空摘要缓存
    POST /page/config/save    保存截图参数

AstrBot 4.26+ 提供 astrbot.api.web（request/json_response/error_response），更早的版本
只有裸 quart，两套 API 形状不同，所以这里统一包一层 _query_get / _read_json。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

try:  # AstrBot >= 4.26
    from astrbot.api.web import error_response, json_response, request

    _HAS_WEB_API = True
except (ImportError, AttributeError):  # 老版本回落到 quart
    _HAS_WEB_API = False
    try:
        from quart import jsonify as _quart_jsonify
        from quart import request  # type: ignore[assignment]
    except ImportError:  # 本地单测环境两个都没有
        request = None  # type: ignore[assignment]
        _quart_jsonify = None  # type: ignore[assignment]

    def json_response(  # type: ignore[misc]
        data: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if _quart_jsonify is None:
            return {"status_code": status_code, "data": data}
        resp = _quart_jsonify(data)
        resp.status_code = status_code
        for key, value in (headers or {}).items():
            resp.headers[key] = value
        return resp

    def error_response(  # type: ignore[misc]
        message: str = "",
        *,
        status_code: int = 400,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return json_response(
            {"status": "error", "message": message, "data": data if data is not None else {}},
            status_code=status_code,
            headers=headers,
        )


from .groups import GroupNameResolver, session_label, split_umo

PLUGIN_NAME = "astrbot_plugin_wuxianews"

# 推送群名单上限，防止面板批量勾选把配置撑爆
MAX_PUSH_GROUPS = 200
# 与 _tick 里的节流保持一致（秒）
CHECK_THROTTLE = 300


def _log_warn(message: str) -> None:
    try:
        from astrbot.api import logger

        logger.warning(f"[wuxianews] {message}")
    except Exception:
        pass


def _fmt_time(ts: float) -> str:
    """时间戳 → 本地可读时间；0/负数表示从未跑过。"""
    if not ts or ts <= 0:
        return "从未"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return "未知"


class NewsPageController:
    """把天刀公告插件的能力包成 HTTP 接口。"""

    def __init__(
        self, context: Any, plugin: Any = None, data_dir: str | Path | None = None
    ) -> None:
        self.context = context
        self.plugin = plugin
        self.data_dir = Path(data_dir) if data_dir is not None else None

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_routes(self) -> None:
        routes: list[tuple[str, Callable[..., Any], list[str], str]] = [
            ("/page/meta", self.get_meta, ["GET"], "天刀公告：常量与截图参数"),
            ("/page/status", self.get_status, ["GET"], "天刀公告：运行状态与推送群"),
            ("/page/groups", self.get_groups, ["GET"], "天刀公告：会话列表（带群名）"),
            ("/page/groups/save", self.save_groups, ["POST"], "天刀公告：保存推送群名单"),
            ("/page/push-now", self.push_now, ["POST"], "天刀公告：立刻检查并推送"),
            ("/page/records", self.get_records, ["GET"], "天刀公告：推送去重记录"),
            ("/page/records/reset", self.reset_records, ["POST"], "天刀公告：清空推送记录"),
            ("/page/shots", self.get_shots, ["GET"], "天刀公告：截图缓存列表"),
            ("/page/shots/clear", self.clear_shots, ["POST"], "天刀公告：清理截图缓存"),
            ("/page/cache/clear", self.clear_cache, ["POST"], "天刀公告：清空摘要缓存"),
            ("/page/config/save", self.save_config, ["POST"], "天刀公告：保存截图参数"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}{path}", handler, methods, desc
                )
            except Exception as exc:  # 老版本 AstrBot / 单测：注册不上也不该炸插件
                _log_warn(f"注册接口 {path} 失败: {exc}")

    # ------------------------------------------------------------------
    # 请求 / 响应小工具
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(data: Any = None, message: str = "") -> Any:
        payload: dict[str, Any] = {
            "status": "ok",
            "data": data if data is not None else {},
        }
        if message:
            payload["message"] = message
        return json_response(payload)

    @staticmethod
    def _err(message: str, status_code: int = 400) -> Any:
        return error_response(message, status_code=status_code)

    @staticmethod
    def _query_get(key: str, default: str = "") -> str:
        if request is None:
            return default
        for holder in ("query", "args"):
            bag = getattr(request, holder, None)
            if bag is None:
                continue
            try:
                value = bag.get(key, default)
            except Exception:
                continue
            if value is not None:
                return str(value)
        return default

    @staticmethod
    async def _read_json() -> dict[str, Any]:
        if request is None:
            return {}
        for method_name in ("json", "get_json"):
            method = getattr(request, method_name, None)
            if not callable(method):
                continue
            try:
                data = method()
                if hasattr(data, "__await__"):
                    data = await data
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return {}

    async def _payload(self) -> dict[str, Any]:
        payload = await self._read_json()
        return payload if isinstance(payload, dict) else {}

    def _resolver(self) -> GroupNameResolver | None:
        resolver = getattr(self.plugin, "groups", None)
        return resolver if isinstance(resolver, GroupNameResolver) else None

    def _label(self, umo: str) -> str:
        resolver = self._resolver()
        return session_label(umo) if resolver is None else resolver.label(umo)

    def _shot_dir(self) -> Path | None:
        return None if self.data_dir is None else self.data_dir / "news_shots"

    def _cfg(self, key: str, default: Any = None) -> Any:
        config = getattr(self.plugin, "config", None)
        try:
            value = config.get(key, default)  # type: ignore[union-attr]
        except Exception:
            return default
        return default if value is None else value

    def _int_cfg(self, key: str, default: int) -> int:
        """取整数配置。坏值回退默认，不把 0 当成「没配」。"""
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # 数据文件（都在 plugin_data 下，纯 IO，方便单测）
    # ------------------------------------------------------------------

    def _read_records(self) -> dict[str, Any]:
        if self.data_dir is None:
            return {}
        try:
            data = json.loads(
                (self.data_dir / "push_record.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_records(self, records: dict[str, Any]) -> None:
        if self.data_dir is None:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "push_record.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _read_summary_cache(self) -> dict[str, Any]:
        if self.data_dir is None:
            return {}
        try:
            data = json.loads(
                (self.data_dir / "summary_cache.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _shot_files(self) -> list[dict[str, Any]]:
        """截图缓存文件列表（按时间倒序）。预览地址另外用 _shot_urls 补。"""
        shot_dir = self._shot_dir()
        if shot_dir is None or not shot_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(
            (p for p in shot_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                    "modified_text": _fmt_time(stat.st_mtime),
                    "url": "",
                }
            )
        return out

    def _shot_urls(self, shots: list[dict[str, Any]]) -> None:
        """把截图注册到 AstrBot 文件服务拿短期 token（拿不到就留空，只显示文件名）。"""
        shot_dir = self._shot_dir()
        if shot_dir is None:
            return
        try:
            from astrbot.core import file_token_service  # type: ignore
        except Exception:
            return
        for item in shots:
            try:
                token = file_token_service.register_file(str(shot_dir / item["name"]))
                if hasattr(token, "__await__"):
                    return  # 异步 token：整体放弃预览，别留半截数据
                item["url"] = f"/api/file/{token}" if token else ""
            except Exception:
                item["url"] = ""

    # ------------------------------------------------------------------
    # 接口：概览
    # ------------------------------------------------------------------

    async def get_meta(self) -> Any:
        return self._ok(
            {
                "plugin": PLUGIN_NAME,
                "check_throttle": CHECK_THROTTLE,
                "limits": {
                    "max_push_groups": MAX_PUSH_GROUPS,
                    "shot_max_height": [0, 20000],
                    "shot_quality": [1, 100],
                },
                "config": {
                    "shot_enabled": bool(self._cfg("shot_enabled", True)),
                    "shot_max_height": self._int_cfg("shot_max_height", 5000),
                    "shot_quality": self._int_cfg("shot_quality", 88),
                },
                "defaults": {"shot_max_height": 5000, "shot_quality": 88},
                "data_dir": str(self.data_dir) if self.data_dir else "",
            }
        )

    async def get_status(self) -> Any:
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        plugin = self.plugin
        now = time.time()
        last_check = float(getattr(plugin, "_last_check", 0) or 0)
        task = getattr(plugin, "_scheduler_task", None)
        lister = getattr(plugin, "_push_groups", None)
        umos = list(lister() or []) if callable(lister) else []
        records = self._read_records()
        groups = []
        for umo in umos:
            _, _, sid = split_umo(umo)
            groups.append(
                {
                    "umo": umo,
                    "label": self._label(umo),
                    "group_id": sid,
                    "last_pushed": records.get(f"push:{umo}") or records.get(sid) or "",
                }
            )
        shots = self._shot_files()
        return self._ok(
            {
                "running": bool(task is not None and not task.done()),
                "last_check": last_check,
                "last_check_text": _fmt_time(last_check),
                "next_check_in": max(0, int(CHECK_THROTTLE - (now - last_check)))
                if umos
                else 0,
                "throttle": CHECK_THROTTLE,
                "groups": groups,
                "record_count": len(records),
                "shot_count": len(shots),
                "shot_bytes": sum(int(s["size"]) for s in shots),
                "summary_cache_count": len(self._read_summary_cache()),
                "shot_enabled": bool(self._cfg("shot_enabled", True)),
                "data_dir": str(self.data_dir) if self.data_dir else "",
            }
        )

    # ------------------------------------------------------------------
    # 接口：推送群
    # ------------------------------------------------------------------

    def _normalize_umos(self, raw: Any) -> list[str]:
        """校验 + 去重 + 限长。面板传进来的东西一律不信任。"""
        out: list[str] = []
        for item in list(raw or []):
            text = str(item or "").strip()
            if not text or len(text) > 200:
                continue
            if ":" not in text and not text.isdigit():
                continue
            if text not in out:
                out.append(text)
            if len(out) >= MAX_PUSH_GROUPS:
                break
        return out

    async def get_groups(self) -> Any:
        """会话列表（带群名）。refresh=1 时先去平台要一遍群列表。"""
        refresh = self._query_get("refresh", "0").strip().lower() in {"1", "true", "yes"}
        refreshed = 0
        if refresh and self.plugin is not None:
            refresher = getattr(self.plugin, "refresh_group_names", None)
            if callable(refresher):
                try:
                    refreshed = int(await refresher(force=True) or 0)
                except Exception as exc:
                    _log_warn(f"刷新群列表失败: {exc}")

        lister = getattr(self.plugin, "_push_groups", None)
        pushed = list(lister() or []) if callable(lister) else []
        resolver = self._resolver()
        rows: dict[str, dict[str, Any]] = {}

        def add(umo: str, source: str) -> None:
            if not umo or umo in rows:
                return
            _, _, sid = split_umo(umo)
            rows[umo] = {
                "value": umo,
                "label": self._label(umo),
                "group_id": sid,
                "group_name": resolver.name_of(umo) if resolver else "",
                "enabled": umo in pushed,
                "source": source,
            }

        for umo in pushed:
            add(umo, "pushed")
        if resolver is not None:
            for umo in resolver.entries():
                add(umo, "platform")

        groups = list(rows.values())
        groups.sort(
            key=lambda item: (
                not item["enabled"],
                not bool(item["group_name"]),
                item["group_id"] or item["label"],
            )
        )
        return self._ok(
            {
                "groups": groups,
                "refreshed": refreshed,
                "total": len(groups),
                "pushed": len(pushed),
                "max_push_groups": MAX_PUSH_GROUPS,
            }
        )

    async def save_groups(self) -> Any:
        """保存推送群名单：写回 news_groups 配置并落盘。"""
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        payload = await self._payload()
        raw = payload.get("groups")
        if raw is None:
            return self._err("缺少 groups 字段")
        if not isinstance(raw, (list, tuple)):
            return self._err("groups 需要是数组")
        if len(list(raw)) > MAX_PUSH_GROUPS:
            return self._err(f"推送群最多 {MAX_PUSH_GROUPS} 个")
        wanted = self._normalize_umos(raw)

        lister = getattr(self.plugin, "_push_groups", None)
        before = list(lister() or []) if callable(lister) else []
        config = getattr(self.plugin, "config", None)
        if config is None:
            return self._err("插件配置不可用", status_code=500)
        try:
            config["news_groups"] = wanted
        except Exception as exc:
            return self._err(f"写入配置失败：{exc}", status_code=500)
        saver = getattr(self.plugin, "save_config_now", None)
        if callable(saver):
            try:
                await saver()
            except Exception as exc:
                _log_warn(f"保存配置失败: {exc}")
                return self._err("配置保存失败，请查看服务端日志", status_code=500)

        added = [u for u in wanted if u not in before]
        removed = [u for u in before if u not in wanted]
        return self._ok(
            {
                "groups": wanted,
                "added": [{"umo": u, "label": self._label(u)} for u in added],
                "removed": [{"umo": u, "label": self._label(u)} for u in removed],
            },
            f"已保存：新增 {len(added)} 个，移除 {len(removed)} 个",
        )

    async def push_now(self) -> Any:
        """立刻检查并推送一次（不走 5 分钟节流）。"""
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        payload = await self._payload()
        target = str(payload.get("umo", "") or "").strip()
        force = bool(payload.get("force", False))
        pusher = getattr(self.plugin, "push_now", None)
        if not callable(pusher):
            return self._err("当前版本不支持手动推送，请更新插件", status_code=501)
        try:
            result = await pusher(target or None, force=force)
        except Exception as exc:
            _log_warn(f"手动推送失败: {exc}")
            return self._err(f"推送失败：{exc}", status_code=500)
        result = result if isinstance(result, dict) else {}
        for key in ("pushed", "skipped"):
            result[key] = [
                {"umo": u, "label": self._label(u)} for u in list(result.get(key) or [])
            ]
        if target:
            result["target"] = {"umo": target, "label": self._label(target)}
        return self._ok(result, str(result.get("message") or ""))

    # ------------------------------------------------------------------
    # 接口：推送记录 / 缓存
    # ------------------------------------------------------------------

    async def get_records(self) -> Any:
        records = self._read_records()
        rows = []
        for key, title in records.items():
            umo = str(key)[5:] if str(key).startswith("push:") else ""
            rows.append(
                {
                    "key": str(key),
                    "title": str(title or ""),
                    "umo": umo,
                    "label": self._label(umo) if umo else str(key),
                }
            )
        rows.sort(key=lambda item: item["label"])
        return self._ok({"records": rows, "count": len(rows)})

    async def reset_records(self) -> Any:
        """清空推送记录。带 umo 只清那一个群，否则全清。"""
        payload = await self._payload()
        umo = str(payload.get("umo", "") or "").strip()
        records = self._read_records()
        if not umo:
            count = len(records)
            self._write_records({})
            return self._ok({"removed": count}, f"已清空 {count} 条推送记录")
        _, _, sid = split_umo(umo)
        keys = [f"push:{umo}"]
        if sid:
            keys += [sid, f"push:{sid}"]
        removed = 0
        for key in keys:
            if records.pop(key, None) is not None:
                removed += 1
        self._write_records(records)
        return self._ok(
            {"removed": removed, "umo": umo, "label": self._label(umo)},
            f"{self._label(umo)} 的推送记录已重置",
        )

    async def get_shots(self) -> Any:
        shots = self._shot_files()
        self._shot_urls(shots)
        return self._ok(
            {
                "shots": shots,
                "count": len(shots),
                "bytes": sum(int(s["size"]) for s in shots),
            }
        )

    async def clear_shots(self) -> Any:
        """删除截图缓存文件（下次推送会重新截）。"""
        shot_dir = self._shot_dir()
        removed, freed = 0, 0
        if shot_dir is not None and shot_dir.is_dir():
            for path in shot_dir.iterdir():
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    freed += size
                except OSError:
                    continue
        return self._ok({"removed": removed, "freed": freed}, f"已删除 {removed} 张截图")

    async def clear_cache(self) -> Any:
        """清空摘要缓存（下次推送重新抓详情页）。"""
        count = len(self._read_summary_cache())
        if self.data_dir is not None:
            try:
                (self.data_dir / "summary_cache.json").unlink(missing_ok=True)
            except OSError as exc:
                return self._err(f"删除缓存失败：{exc}", status_code=500)
        return self._ok({"removed": count}, f"已清空 {count} 条摘要缓存")

    # ------------------------------------------------------------------
    # 接口：截图参数
    # ------------------------------------------------------------------

    async def save_config(self) -> Any:
        if self.plugin is None:
            return self._err("插件未就绪", status_code=503)
        payload = await self._payload()
        config = getattr(self.plugin, "config", None)
        if config is None:
            return self._err("插件配置不可用", status_code=500)

        raw_height = payload.get("shot_max_height", self._cfg("shot_max_height", 5000))
        if raw_height in (None, ""):
            raw_height = 5000
        raw_quality = payload.get("shot_quality", self._cfg("shot_quality", 88))
        if raw_quality in (None, ""):
            raw_quality = 88
        try:
            max_height = int(raw_height)
            quality = int(raw_quality)
        except (TypeError, ValueError):
            return self._err("截图参数需要是整数")
        if not 0 <= max_height <= 20000:
            return self._err("截图最大高度需在 0-20000 之间（0 = 不截断）")
        if not 1 <= quality <= 100:
            return self._err("JPEG 质量需在 1-100 之间")

        try:
            config["shot_enabled"] = bool(
                payload.get("shot_enabled", self._cfg("shot_enabled", True))
            )
            config["shot_max_height"] = max_height
            config["shot_quality"] = quality
        except Exception as exc:
            return self._err(f"写入配置失败：{exc}", status_code=500)
        saver = getattr(self.plugin, "save_config_now", None)
        if callable(saver):
            try:
                await saver()
            except Exception as exc:
                _log_warn(f"保存配置失败: {exc}")
                return self._err("配置保存失败，请查看服务端日志", status_code=500)
        return self._ok(
            {
                "shot_enabled": bool(config.get("shot_enabled", True)),
                "shot_max_height": max_height,
                "shot_quality": quality,
            },
            "截图参数已保存",
        )
