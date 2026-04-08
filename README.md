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

状态图片和开始游戏卡片共用同一套图片节流与抖动抑制规则，同一群聊内的图片会按顺序排队发送。下面按功能分组，便于快速查找。

### 基础与监控目标

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `steam_api_key` | 无 | Steam Web API Key（在 <https://steamcommunity.com/dev/apikey> 申请） |
| `steam_ids` | `""` | 全局监控 SteamID64，不推荐手动填写 |
| `push_targets` | `""` | 哪些群启用了监控，不推荐手动填写 |

### 轮询与时长

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `poll_interval_sec` | 50 | 基础轮询间隔（秒） |
| `cache_ttl_sec` | 300 | 网页抓取缓存有效期（秒） |
| `count_game_duration_online_only` | `false` | 如果是，则“离开”状态不统计游戏时长 |

### 状态推送与抖动抑制

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `status_text_trigger_types` | `online,game_stop,game_switch` | 哪些事件会触发文字推送；可选：`online` `offline` `game_start` `game_stop` `game_switch` |
| `status_image_trigger_types` |  | 哪些事件会触发状态图推送；可选：`online` `offline` `game_start` `game_stop` `game_switch` |
| `status_image_min_interval_sec` | `3` | 状态图片和开始游戏卡片共用的最小推送间隔（秒），`0` 表示不限制；用于规避高频发送，一般可弃用。 |
| `presence_flap_suppress_min` | `2` | 上下线抖动抑制窗口（分钟），`X`分钟内反复上下线会被视为始终在线，也会抑制同一款游戏的短暂关闭/重启 |
| `enable_non_steam_game_start_text_exception` | `false` | 为非 Steam 游戏启动添加文字推送例外 |

### 成就监控

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_achievement_monitor` | `true` | 是否开启成就监控 |
| `achievement_poll_interval_sec` | `1200` | 成就轮询间隔（秒） |
| `achievement_final_check_delay_sec` | `300` | 游戏结束后多久再检查一次成就（秒） |
| `achievement_fail_limit_per_day` | `10` | 同一游戏单日最多查询几次成就 |
| `max_achievement_notifications` | `5` | 单次最多推送几个新成就 |

### 开始游戏卡片

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_game_start_render` | `true` | 是否启用开始游戏的图片消息；启用后同样受上述图片推送间隔与抖动抑制约束 |
| `game_start_bg_image` | `star_767x809.png` | 开始游戏页面背景图文件名或路径 |
| `game_start_bg_opacity` | `0.15` | 开始游戏页面背景图不透明度（`0.0`-`1.0`） |

### 网络与图片来源

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `steam_api_base` | `https://api.steampowered.com` | Steam API 基础地址 |
| `steam_store_base` | `https://store.steampowered.com` | Steam Store API 基础地址 |
| `sgdb_api_key` | `""` | SteamGridDB API Key（查询竖版封面） |
| `sgdb_api_base` | `https://www.steamgriddb.com` | SteamGridDB API 基础地址 |
| `enable_profile_game_fallback` | `true` | API 未返回游戏名时，尝试从个人资料页读取 |
| `image_proxy_prefix` | `https://images.weserv.nl/?url=` | 图片中转前缀，留空为直连 |
| `allow_dns_private_for_allow_domains` | `false` | 是否允许对白名单域名跳过内网解析拦截；开启后可缓解部分 DNS 污染场景 |

## 命令列表

### 目标会话管理

| 命令 | 参数 | 说明 | 示例 |
| --- | --- | --- | --- |
| `/sfm_bind` | 无 | 绑定当前会话为推送目标 | `/sfm_bind` |
| `/sfm_unbind` | 无 | 取消当前会话绑定 | `/sfm_unbind` |
| `/sfm_targets` | 无 | 查看当前推送目标列表 | `/sfm_targets` |

### 全局视奸 SteamID 

| 命令 | 参数 | 说明 | 示例 |
| --- | --- | --- | --- |
| `/sfm_add_id` | `steam_id64` | 添加一个全局 SteamID64 | `/sfm_add_id 7656119xxxxxxxxxx` |
| `/sfm_del_id` | `steam_id64` | 删除一个全局 SteamID64 | `/sfm_del_id 7656119xxxxxxxxxx` |
| `/sfm_set_ids` | `ids` | 批量设置全局 SteamID64（逗号或换行分隔） | `/sfm_set_ids 7656...,7656...` |

### 当前群独立视奸 SteamID 

| 命令 | 参数 | 说明 | 示例 |
| --- | --- | --- | --- |
| `/sfm_set_group_ids` | `ids` | 为当前群设置独立 SteamID64 列表 | `/sfm_set_group_ids 7656...,7656...` |
| `/sfm_add_group_id` | `ids` | 为当前群追加一个或多个 SteamID64 | `/sfm_add_group_id 7656...,7656...` |
| `/sfm_del_group_id` | `steam_id64` | 为当前群删除一个 SteamID64 | `/sfm_del_group_id 7656119xxxxxxxxxx` |
| `/sfm_group_ids` | 无 | 查看当前群 SteamID 配置 | `/sfm_group_ids` |
| `/sfm_clear_group_ids` | 无 | 清除当前群独立配置并使用全局配置 | `/sfm_clear_group_ids` |

### 手动拉取和测试

| 命令 | 参数 | 说明 | 示例 |
| --- | --- | --- | --- |
| `/sfm_status` | 无 | 立即拉取并推送当前群聊的状态图 | `/sfm_status` |
| `/sfm_test` | `action`（可选） | 监控链路测试。可选：`all` `cfg` `config` `status` `pull` `push` `emoji`| `/sfm_test all` |
| `/steam test_achievement_render` | `steamid gameid [count]` | 成就图片渲染测试（`count` 默认 `3`） | `/steam test_achievement_render 7656... 730 3` |
| `/steam test_game_start_render` | `steamid gameid` | 开始游戏卡片渲染测试 | `/steam test_game_start_render 7656... 730` |

### 成就监控开关

| 命令 | 参数 | 说明 | 示例 |
| --- | --- | --- | --- |
| `/sfm_achievement_on` | 无 | 开启成就监控 | `/sfm_achievement_on` |
| `/sfm_achievement_off` | 无 | 关闭成就监控并停止当前任务 | `/sfm_achievement_off` |
| `/sfm_achievement_status` | 无 | 查看成就监控状态和任务数量 | `/sfm_achievement_status` |



## 说明

1. 原命令较为繁琐，但考虑到可以在 AstrBot插件 - 管理行为 - 重命名 处添加别名，因此推荐安装后手动添加多个别名。

   例如`/sfm_status` -> `/视奸`, `/sfm_add_group_id`-> `/add`

   添加的命令可以给予所有人权限，删除只给管理员权限。

2. /sfm_test emoji 可以查看当前系统是否已经支持emoji渲染。
