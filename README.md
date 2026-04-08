# astrbot_plugin_steam_friend_monitor

### Steam 好友在线监控插件（AstrBot）

- 修复了图片发送的问题，新增了一些实用的配置项。

- 自动记录当前游戏时间/当天游玩时间，可以配置文字/图片推送，兼容emoji姓名

- 和原插件冲突！只能安装一个（尽管原插件好像已经失效）

- [原插件地址](https://github.com/Vince-0906/astrbot_plugin_steam_friend_monitor)



感谢[steam_status_monitor](https://github.com/Maoer233/astrbot_plugin_steam_status_monitor)插件提供的优美的开始游戏和成就展示方法

![image-20260328161116923](img0)

![image-20260328160704436](img1)

![image-20260328160733510](img2)

![image-20260328160839182](img3)

## 配置项

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `steam_api_key` | 无 | Steam Web API Key（在 <https://steamcommunity.com/dev/apikey> 申请） |
| `push_targets` | `""` | 已启用插件的会话列表，建议通过命令维护 |
| `poll_interval_sec` | `30` | 基础轮询间隔（秒） |
| `cache_ttl_sec` | `120` | 网页抓取缓存有效期（秒，不低于 60） |
| `count_game_duration_online_only` | `true` | 仅在“在线”状态统计游戏时长 |
| `status_text_trigger_types` | `online,game_stop,game_switch` | 文字推送触发事件，可选：`online,offline,game_start,game_stop,game_switch` |
| `status_image_trigger_types` | `""` | 状态图推送触发事件，可选同上；留空表示不自动推图 |
| `enable_game_start_render` | `true` | 是否推送开始游戏卡片 |
| `game_start_bg_image` | `star_767x809.png` | 开始游戏卡片背景图（插件目录或数据目录） |
| `game_start_bg_opacity` | `0.15` | 开始游戏卡片背景透明度（0.0-1.0） |
| `status_image_min_interval_sec` | `1` | 所有图片最小推送间隔（秒，0 表示不限制，最大 10） |
| `presence_flap_suppress_min` | `2` | 上下线抖动抑制窗口（分钟） |
| `enable_non_steam_game_start_text_exception` | `false` | 非 Steam 游戏启动时的文字推送例外 |
| `enable_achievement_monitor` | `true` | 是否开启成就监控 |
| `achievement_poll_interval_sec` | `1200` | 成就轮询间隔（秒） |
| `achievement_final_check_delay_sec` | `300` | 游戏结束后成就补偿检查延迟（秒） |
| `achievement_fail_limit_per_day` | `10` | 同游戏单日成就接口失败阈值 |
| `max_achievement_notifications` | `5` | 单次最多推送新成就数量 |
| `steam_api_base` | `https://api.steampowered.com` | Steam API 基础地址 |
| `steam_store_base` | `https://store.steampowered.com` | Steam Store API 基础地址 |
| `sgdb_api_key` | `""` | SteamGridDB API Key |
| `sgdb_api_base` | `https://www.steamgriddb.com` | SteamGridDB API 基础地址 |
| `enable_profile_game_fallback` | `true` | API 未返回游戏名时，尝试从个人资料页读取 |
| `image_proxy_prefix` | `https://images.weserv.nl/?url=` | 图片中转前缀，留空直连 |
| `allow_dns_private_for_allow_domains` | `false` | 白名单域名是否跳过内网解析拦截 |

## 命令列表

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `/sfm` | 无 | 从缓存拉取当前群状态图 |
| `/sfm_bind` | 无 | 为当前群启用插件 |
| `/sfm_unbind` | 无 | 为当前群取消启用 |
| `/sfm_add` | `链接/好友码/SteamID64` | 为当前群添加一个视奸对象 |
| `/sfm_del` | `链接/好友码/SteamID64` | 为当前群删除一个视奸对象 |
| `/sfm_clear` | 无 | 清除当前群配置（视奸对象 + 启用状态） |
| `/sfm_test` | `[game_start \| achievement] [gameid]` | 测试卡片渲染（使用当前群第一个视奸对象） |

## 说明


建议在 AstrBot 的命令别名中自行缩短命令，例如把 `/sfm_add` 设为 `/add`，把`/sfm`改为`/视奸`
