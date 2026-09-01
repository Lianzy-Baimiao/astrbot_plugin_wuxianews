# astrbot_plugin_wuxianews

天涯明月刀公告插件（AstrBot）。移植自 ZeroBot-Plugin 的 `plugin/wuxianews`，逻辑与原版一致，并新增定时自动推送。

作者：lianzy

## 指令（裸词即可触发，/ 前缀可省略）

| 指令 | 说明 |
|---|---|
| `公告列表` | 最近 10 条天刀公告（类型/标题/时间/链接） |
| `最新公告` | 最新一条公告 + 详情页内容摘要（含深色风格卡片图） |
| `最新公告改` | 有新公告才返回（按群去重） |
| `天刀新闻推送 开/关/状态/测试` | 本群开启后**每 5 分钟检查**，有更新自动推送（开/关/测试需管理员） |
| `重置公告推送` | 清空本群推送记录（需管理员） |

## 配置（WebUI 插件配置页）

- `news_groups`：定时推送的群（填**群号**或 unified_msg_origin，可多个）。也可以在群里发 `天刀新闻推送 开` 自动登记本群，无需手动配置。

## 数据存储

推送去重记录存于 `data/plugin_data/astrbot_plugin_wuxianews/push_record.json`。

## 说明

- 公告数据源：`wuxia.qq.com`（GBK 编码，自动转码解析）
- **卡片图**：由 AstrBot 内置 html_render 渲染深色风格公告卡片（模板 `templates/news.html`）
- 管理指令（开/关/测试/重置）需要 AstrBot 的 `admins_id`（WebUI 配置，非 QQ 群管理）

## 灵感来源

移植自 [ZeroBot-Plugin](https://github.com/FloatTech/ZeroBot-Plugin) 的 `plugin/wuxianews`。原项目采用 MIT 许可证。

## License

MIT