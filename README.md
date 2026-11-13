<p align="center">
  <img src="./logo.png" width="144" height="144" alt="Gal Link 的 Q 版角色图标">
</p>

# Gal Link · 资源搜索

在 QQ 群里 @ AstrBot，用自然语言查找 Galgame 资源。插件根据中文、日文和英文标题并发查询站点，汇总下载入口、网盘分享和资源页。

## 安装

需要 **AstrBot `>=4.29.4,<5`**，并已接入 **QQ OneBot（`aiocqhttp`）**。

1. 打开 AstrBot WebUI 的「插件 → 安装插件 → 从链接安装」。
2. 粘贴下面的仓库地址，安装后在「已安装」页确认 **Gal Link · 资源搜索** 已启用。

   ```text
   https://github.com/maruyui-dev/astrbot_plugin_gal_link
   ```

本地开发时，可将仓库克隆到 `AstrBot/data/plugins/astrbot_plugin_gal_link`，在 AstrBot 的 Python 环境中安装 [依赖](./requirements.txt)，然后在 WebUI 重载插件。更多安装方式见 [AstrBot 插件文档](https://docs.astrbot.app/use/plugin.html#从仓库或文件安装)。

## 群聊用法

在 QQ 消息里实际 @ 机器人，接着输入游戏名或查找请求。下文用 `@BOT` 表示这一步。

| 发送 | 用途 |
| --- | --- |
| `@BOT 我想玩千恋万花` | 按作品名搜索资源 |
| `@BOT 给我一个千恋万花的安装包` | 用自然语言提出资源请求 |
| `@BOT 《千恋万花》` | 直接用书名号指定作品 |
| `@BOT 星白`、`@BOT 找一下星白` | 列出与「星白」相关的作品供选择 |
| `@BOT 天使` | 列出名称与「天使」相关的作品 |
| `@BOT 1` | 选择纠错回复里的第 1 个候选 |
| `@BOT 下一页` | 查看后续结果 |

普通聊天由 AstrBot 处理，已匹配的其他插件指令优先。编号和分页只对同一群里的原请求人有效，交互状态在 10 分钟后过期，翻页会刷新有效期。

### 命中示例

```text
找到《千恋万花》
共 5 条资源 · 第 1 / 2 页

1. 真红小站
名称  千恋万花.7z
下载  xxx

2. 喵喵盘
名称  【PC】千恋万花.7z
类型  PC（文件名标注）
下载  xxx

3. 稻荷ACG
名称  千恋万花安卓直装
类型  APK
夸克网盘  xxx

还有 2 条资源，发送「@BOT 下一页」查看。
```

这是格式示例，链接用 `xxx` 代替。类型按资源页面标注显示。页面缺少类型时，从文件名读取明确的标签，仍无标签就省略。公开的百度、夸克、迅雷等网盘分享会注明网盘名称，链接自带的提取参数会保留。

### 纠错示例

```text
「星空与白」可能对应以下作品。

1. 星空列车与白的旅行
2. 星空的记忆

发送 「@BOT 1」 或 「@BOT 2」 查询对应作品的资源。
```

插件用 VNDB 核对标题和别名，也可借助当前会话的聊天模型识别错字。能唯一匹配的完整作品名和高度可信的错字会直接检索。简短关键词、差异较大的简称和相近作品会先列出候选，回复编号后才查询资源站。已确认作品且各站无命中时，会统一提示暂无可用资源。

## 站点与配置

| 站点 | 回复中的入口 |
| --- | --- |
| [真红小站](https://www.shinnku.com/) | 公开下载链接 |
| [梓澪の妙妙屋](https://gal.games/) | 公开下载链接或公开网盘分享 |
| [喵盘](https://catcat.cloud/GalGame) | 公开下载链接 |
| [喵喵盘](https://pan.mmpan.top/) | 公开下载链接 |
| [稻荷 ACG](https://inarigal.com/) | 公开网盘分享或资源页 |
| [TouchGal](https://www.touchgal.ink/) | 官方 API 返回的资源页，需配置 Token |

单个站点没有命中时会从回复中省略。下载入口和网盘链接由原站提供，使用时以站点当前状态为准。

前五个站点开箱即用。要启用 TouchGal，请在 [开发者门户](https://developer.touchgal.com/) 申请访问，获批后在 AstrBot WebUI 的 Gal Link 插件配置里填写「TouchGal 官方 API Token」。该字段默认为空，官方 API 提供资源页入口。详见 [TouchGal API 文档](https://developer.touchgal.com/docs/api/game-resources)。

## 开发与反馈

群消息处理和分页位于 [`main.py`](./main.py)，作品标题匹配位于 [`titles.py`](./titles.py)，站点适配位于 [`sources.py`](./sources.py)。插件放在 `data/plugins/astrbot_plugin_gal_link` 下时，可从 AstrBot 根目录运行测试。

```bash
python -m unittest data.plugins.astrbot_plugin_gal_link.test_gal_link
```

欢迎在 [GitHub Issues](https://github.com/maruyui-dev/astrbot_plugin_gal_link/issues) 反馈问题。请附上 AstrBot 版本、插件版本、输入示例、预期结果和已脱敏的相关日志。

本项目采用 [GNU AGPL v3](./LICENSE) 许可证。
