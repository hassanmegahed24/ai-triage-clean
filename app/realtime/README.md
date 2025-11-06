# Realtime 语音助手与 REST 工具

本目录提供两种集成方式：
- CMD 语音助手（本机麦克风/扬声器，PyAudio）
- FastAPI REST + WebSocket（浏览器/第三方客户端）

核心目标：使用 OpenAI Realtime（语音对语音），并可把“患者快照”安全注入到会话里用于问诊与推理（默认不主动复述）。

## 快速开始

### 1) 环境
- Python 3.8+
- 有效的 OPENAI_API_KEY（写到仓库根目录的 .env）
- 若要注入患者上下文：配置 E_HOSPITAL_BASE_URL 指向你的 EHR mock/服务

### 2) 安装依赖
`
pip install -r requirements.txt
`

### 3) 启动 FastAPI 并测试
`
uvicorn app.main:app --reload
`
- Swagger: http://127.0.0.1:8000/docs
- WebSocket 测试页: GET /realtime/test

常用 REST 端点（均在 ealtime 分组）：
- POST /realtime/say → 返回 JSON（text + audio_base64）
- POST /realtime/say_wav → 直接返回 WAV（可下载/播放）
- POST /realtime/say_play → 返回一个 HTML 页面，内嵌文本与播放器
- POST /realtime/instructions_preview → 预览最终会话指令（含患者上下文）
- POST /realtime/echo_fields → 无模型地回显患者快照字段（用于校验）

## 注入“患者快照”

构建：pp/services/snapshot_builder.py 会根据 E_HOSPITAL_BASE_URL 拉取多张表（过敏、既往、用药、检验、诊断…），pp/realtime/context.py 会将其裁剪为“紧凑快照”，并与系统级 prompt 合成会话指令。

两种注入方式：
- REST 每次调用时注入
  - 在请求体传 patient_id（可选 system_file 指向 pp/prompts 下文件，默认 system_global.txt）。服务端会在建连前自动注入上下文。
- CMD 语音助手启动时注入
  - 在 .env 写入：
    - REALTIME_SYSTEM_FILE=system_global.txt
    - REALTIME_PATIENT_ID=5
  - 运行：python app/main.py --mode voice

默认行为：不主动复述患者信息（只在用户明确要求时复述），已在会话指令中加入“只用已知信息、不猜测”的规则。温度遵循 Realtime 限制（≥0.6）。

## “逐字复述”测试（可选）

用于校验“上下文是否准确注入”。
- 在 REST 端点中传 erbatim=true + patient_id，服务器会把患者的紧凑快照原文内嵌到本轮用户指令中，并要求模型只输出原文（不加前后缀）。
- 示例（/realtime/say_play）：
`
{
  "text": "逐字复述 Known patient context(JSON) 中的 allergies 与 medical_history；仅输出字段原文，不要添加任何前后缀。",
  "system_file": "system_global.txt",
  "patient_id": 5,
  "verbatim": true
}
`
- 若只想查看原文，不经模型：用 POST /realtime/echo_fields（传 patient_id 和需要的键名），或 POST /realtime/instructions_preview 查看最终会话指令。

## WebSocket（浏览器）
- GET /realtime/test 页面：
  - Connect WS → ws://<host>/realtime/ws
  - Start Mic → 采集麦克风并推流 24k PCM16
  - Stop + Ask → 提交缓冲并请求响应
  - Say 你好 → 直接指令式让模型发声
- 说明：WS 测试页默认不注入 patient_id。如要带患者上下文，请使用上面的 REST 端点触发响应，或在服务端扩展 WS 的 session.update 消息体。

## CMD 语音助手
- .env：
  - OPENAI_API_KEY=...
  - REALTIME_SYSTEM_FILE=system_global.txt
  - （可选）REALTIME_PATIENT_ID=5（启动时注入患者上下文）
- 运行：
`
python app/main.py --mode voice
`
- 说明：助手读取 .env 并在建连前注入系统指令；若设置了 REALTIME_PATIENT_ID 则同时注入患者快照。默认不会主动复述患者信息；请直接陈述症状，agent 将结合上下文做推理与问诊。

## 故障排除
- 422 JSON decode error：Swagger 里 	ext 必须是合法 JSON 字符串。若要粘贴原始 JSON，请使用 erbatim=true 让服务端自动内嵌，而非手工转义。
- Cancellation failed: no active response found：竞态造成的冗余取消，已在客户端降级处理（忽略）。
- 温度限制：Realtime 预览模型要求温度 ≥ 0.6；代码已设置为 0.6 以减少发散同时满足限制。
- E_HOSPITAL_BASE_URL：若抓取失败，患者上下文不会注入。请在 .env 配置该地址并确保可访问。
- 音频卡顿/噪音：项目已内置预热缓冲与播放修复逻辑；若仍异常，请检查系统声卡设置（关闭“侦听此设备”）或更换音频设备。

## 目录索引
- pp/realtime/assistant.py 语音助手（CMD）
- pp/realtime/ws.py REST + WebSocket 端点
- pp/realtime/context.py 会话指令构造（系统 prompt + 紧凑快照 + 规则）
- pp/realtime/audio_handler.py 录音/播放（PyAudio）

---
使用本功能会产生 OpenAI API 费用，请留意用量与账单。
