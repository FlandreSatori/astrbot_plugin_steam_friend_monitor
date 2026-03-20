# astrbot_plugin_steam_friend_monitor

Steam 好友在线监控插件（AstrBot）

修复了图片发送的问题

新增：完整迁移 steam_status_monitor 的图片渲染链路

- 成就通知：优先发送成就图片（失败自动回退文本）
- 开始游戏：支持渲染并推送开始游戏图片
- 新增测试命令：
  - /steam test_achievement_render [steamid] [gameid] [数量]
  - /steam test_game_start_render [steamid] [gameid]

配置方式：

- 推荐在插件目录的 config.json 里配置渲染相关参数
- 仍可通过 _conf_schema.json 对应项在面板中调整
- 关键项：enable_achievement_monitor、enable_game_start_render、sgdb_api_key

[原插件地址](https://github.com/Vince-0906/astrbot_plugin_steam_friend_monitor)

