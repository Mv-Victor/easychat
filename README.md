# EasyChat

一个 Vue + Python 的本地聊天网页，使用 SQLite 保存单设备用户、API Key、会话和消息。

## 功能

- 首次进入选择 OpenAI 或 Claude，并填写 API Key
- 后端将 API Key 与当前设备标识绑定，下次进入自动使用
- Vue 聊天界面，支持会话管理和流式聊天
- OpenAI 默认 `gpt-5.5`
- Claude 默认 `claude-opus-4-7`
- OpenAI 生图默认 `gpt-image-2`

说明：普通网页无法直接读取真实 MAC 地址。后端会尝试通过访问 IP 查询局域网 ARP 表；如果获取不到，会用浏览器 `localStorage` 中的设备 ID 作为绑定标识。

设备标识使用后端签名 token。前端通过 `/api/device` 获取 `device_id.signature` 并保存到 `localStorage`，后端只接受签名校验通过的 `X-Device-Token`。

## 开发启动

```bash
python3 app.py
cd frontend
npm install
npm run dev
```

打开：

```text
http://127.0.0.1:3000
```

## 生产启动

```bash
mkdir -p logs
cd frontend
npm install > ../logs/npm-install.log 2>&1
npm run build > ../logs/npm-build.log 2>&1
cd ..
nohup python3 app.py > logs/app.log 2>&1 &
echo $! > logs/app.pid
```

打开：

```text
http://127.0.0.1:7860
```

数据库文件默认创建在 `easychat.sqlite3`。

查看后台日志：

```bash
tail -f logs/app.log
```

停止后台服务：

```bash
kill "$(cat logs/app.pid)"
```

## 环境变量

可选配置：

```bash
EASYCHAT_HOST=127.0.0.1
EASYCHAT_PORT=7860
EASYCHAT_DB=/path/to/easychat.sqlite3
OPENAI_MODEL=gpt-5.5
CLAUDE_MODEL=claude-opus-4-7
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_BASE_URL=https://tkcc.cloud
CLAUDE_BASE_URL=https://tkcc.cloud
EASYCHAT_DEVICE_TOKEN_SECRET=change-this-random-secret
```
