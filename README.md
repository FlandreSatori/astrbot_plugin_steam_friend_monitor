# astrbot_plugin_steam_friend_monitor

Steam 好友在线监控插件（AstrBot）

修复了图片发送的问题，固定了好友渲染顺序（现在会和输入的steamid顺序相同）

[原插件地址](https://github.com/Vince-0906/astrbot_plugin_steam_friend_monitor)

## 树莓派部署排障

若日志出现 `poll error` 且头像/游戏图不显示，请优先检查：

1. 依赖版本
	- `pip show Pillow httpx`
	- 建议：`Pillow>=10`、`httpx>=0.27`
2. 网络与证书
	- 树莓派系统时间是否正确
	- CA 证书是否完整（证书异常通常会导致 HTTPS 访问失败）
3. 图片中转配置
	- `image_proxy_prefix` 留空表示直连
	- 若直连失败可尝试可用中转（默认 `https://images.weserv.nl/?url=`）
4. 重试参数
	- `steam_api_retries` 可调高到 `3-5` 提升弱网稳定性

当前版本已增强异常日志，会输出完整异常类型与堆栈，便于定位具体失败原因。



