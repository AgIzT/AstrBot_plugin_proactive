# astrbot_plugin_maibot_proactive

将 MaiBot 的主动回复核心提炼为 AstrBot 插件的 MVP。

## 功能

- 保守型群聊主动接话
- 提及优先触发
- 概率观察与 `no_reply` 反向降频
- 私聊轻量脑流，支持 `reply / wait / complete_talk`
- 独立 SQLite 状态存储，保存最近消息、动作记录和会话节奏
- 可选地将主动回复补写回已有的 AstrBot 对话历史
- 不拦截原有 AstrBot 命令、插件、Agent 和任务流程

## 安装

1. 将本目录放入 AstrBot 工作区的 `data/plugins/` 下。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 在 AstrBot 中加载或重载插件。

## 说明

- 当前版本是聚焦主动回复行为的 MVP。
- 还没有迁移 MaiBot 的长期记忆、黑话学习、表达学习和复杂动作编排。
- `quote` 当前只作为回复风格提示使用，不会发送平台原生引用消息。
