# XJ-GPT 协议注册机

> 基于 HTTP 协议层(非浏览器自动化)的 ChatGPT 账号批量注册工具,自带 Web GUI + 实时日志 + Outlook 自动取件。

---

## ✨ 特性

- **纯协议层**:`curl_cffi` 复刻 Chrome TLS / HTTP2 指纹,绕过 Cloudflare 检测,无需启动浏览器
- **Sentinel Token**:本地 Node 子进程跑 OpenAI 反爬 SDK,自动计算 `openai-sentinel-token`
- **OTP 自动取件**:对接 Outlook 邮箱池(mail.chatai.codes 协议),无需手动输入验证码
- **Web GUI**:Flask 单页,实时 SSE 日志推送,支持手动/池模式切换
- **CLI 批量**:多线程并发,失败可继续,自动错峰
- **失败语义清晰**:区分"创建前失败(可重试)"、"创建后失败(已废弃)"、"邮箱已注册(永久 failed)",自动维护邮箱池状态
- **2FA 可选**:支持自动 enroll TOTP 并保存 secret
- **后置 Flow**:注册成功后可自动触发指定的 flow 接口

## 📦 项目结构

```
.
├── main.py                    # CLI 入口,批量调度(串行/线程池)
├── web_app.py                 # Web GUI 入口(Flask + SSE)
├── requirements.txt           # 依赖
├── config/                    # 分模块配置
│   ├── browser.py             # UA / curl_cffi impersonate / 超时
│   ├── openai_protocol.py     # OAuth 固定参数 / Sentinel 版本
│   ├── proxy.py               # 代理池
│   ├── register.py            # 注册默认信息
│   ├── email.py               # Outlook 邮箱池 + OTP 轮询
│   ├── twofa.py               # 2FA 开关
│   └── flow_trigger.py        # 后置 flow 配置
├── core/                      # 核心业务
│   ├── session.py             # curl_cffi BrowserSession
│   ├── chatgpt_auth.py        # providers / csrf / signin
│   ├── openai_auth.py         # authorize / OTP / create_account
│   ├── sentinel.py            # Sentinel Token 请求/构造
│   ├── sentinel_runner.py     # 调用 Node 计算 token
│   ├── account_export.py      # OAuth 回调 / fetch session / 2FA / 归档
│   ├── email_provider.py      # acquire_email / wait_for_otp
│   ├── outlook_client.py      # Outlook 邮箱池协议层
│   ├── flow_trigger.py        # 后置 flow 触发
│   ├── otp_utils.py / db.py
│   └── registration_service.py
├── sentinel/                  # Node 端 SDK
│   ├── sdk.js
│   └── sentinel-runner.js
├── templates/
│   └── index.html             # Web GUI 单页
├── accounts/                  # 归档目录(按批次)— ⚠️ 含敏感数据,已 gitignore
└── 注册日志/                  # 每次注册的详细日志
```

## 🚀 快速开始

### 环境要求

- **Python** ≥ 3.10(用了 `tuple[X, Y]` 等新语法)
- **Node.js** ≥ 16(算 Sentinel Token 用)
- **代理**(可选,但推荐):需要能稳定访问 `chatgpt.com` / `auth.openai.com`

### 安装

```bash
git clone https://github.com/hmexy/XJ-GPT-Registrar.git
cd XJ-GPT-Registrar
pip install -r requirements.txt
```

### 配置

1. 在 [config/proxy.py](config/proxy.py) 里填上你的代理(默认 `http://127.0.0.1:7890`,可改成代理池)
2. 如果用 Outlook 自动取件:把邮箱按下面格式存到 `用于注册的邮箱.json`,程序会按 status=available 自动抽取
3. (可选)在 [config/twofa.py](config/twofa.py) 里设置 `ENABLE_2FA=True` 启用自动 TOTP 设置

### 运行

#### Web GUI(推荐)

```bash
python web_app.py                 # http://127.0.0.1:5000
python web_app.py --port 8000
python web_app.py --host 0.0.0.0  # 暴露局域网,自担风险
```

界面功能:
- **模式切换**:Outlook 自动取件 / 手动粘贴邮箱
- **手动模式**:textarea 一行一个,自动识别两种格式(见下)
- **参数**:数量、并发、单个失败后是否继续
- **统计**:目标/已完成/成功/失败 实时更新
- **日志窗**:SSE 实时推送,支持自动滚动 / 暂停 / 清空

#### CLI(命令行批量)

```bash
python main.py -n 1                                   # 单个测试
python main.py -n 10 --workers 3 --continue-on-fail   # 批量 10 个,3 并发
python main.py -n 5 --delay 30                        # 串行,每个间隔 30s
python main.py -n 1 --verbose                         # 详细日志
```

更多命令见 [启动命令.txt](启动命令.txt)。

## 📧 邮箱格式

GUI 手动模式 / `用于注册的邮箱.json` 都需要邮箱包含 4 个字段:**email / password / client_id / refresh_token**。

GUI 的 textarea 自动识别两种粘贴格式:

**格式一**(标准,`----` 分隔):
```
foo@hotmail.com----pwd123----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C527_BAY.0.U.xxx
```

**格式二**(冒号变体,自动转换):
```
foo@hotmail.com:pwd123::M.C527_BAY.0.U.xxx:9e5f94bc-e8a4-4e73-b8be-63364c29d753::M.C527_BAY.0.U.xxx
```

冒号变体的字段映射:`[0]=email`,`[1]=password`,`[3]=refresh_token`,`[4]=client_id`,`[2]/[5]` 为空,`[6]` 是 `[3]` 的重复。

## 🔄 注册流程(13 步)

```
阶段 1 · ChatGPT 认证
  ├─ 步骤 1   GET  /api/auth/providers
  ├─ 步骤 2   GET  /api/auth/csrf
  └─ 步骤 3   POST /api/auth/signin/login-web (带 login_hint + screen_hint=login_or_signup)

阶段 2 · OpenAI Auth
  └─ 步骤 4   跟随 authorize URL → 自动落到 /email-verification → OTP 已发送

阶段 3 · OTP 验证
  ├─ 步骤 9   Sentinel Token (authorize_continue)
  ├─ 等待 OTP (Outlook 自动 / 手动输入)
  └─ 步骤 10  POST /api/accounts/email-otp/validate

阶段 4 · 完成注册
  ├─ 步骤 11  Sentinel Token (oauth_create_account)
  └─ 步骤 12  POST /api/accounts/create_account

阶段 5 · 登录态建立
  ├─ 步骤 12.5  follow_oauth_callback → 写入 chatgpt.com 的 session-token cookie
  └─ 步骤 13    GET /api/auth/session → 提取 accessToken (5 次指数退避重试)

阶段 6 · 2FA(可选)
  └─ 步骤 14-20 重认证 → enroll TOTP → activate

阶段 7 · 持久化
  └─ save_account_data → 写入 accounts/{batch_dir}/ + 注册成功的*.txt + DB

阶段 8 · 后置 Flow
  └─ trigger_flow(access_token) → 自定义 flow 接口
```

## ❗ 错误处理

| 错误码 / 异常 | 含义 | 邮箱池处理 |
|---|---|---|
| `invalid_auth_step` | 邮箱已在 OpenAI 注册过 | **failed**(永久,不重试) |
| `email_otp_invalid` | 验证码错误 | available(可重试) |
| `email_otp_expired` | 验证码过期 | available(可重试) |
| `email_otp_attempts_exceeded` | 尝试过多 | available(稍后再试) |
| `name_invalid_chars` | 显示名含非法字符 | available |
| 网络层错误(SSL/Timeout/Proxy) | 临时性 | 自动重试 8 次,退避 2^n 秒 |
| 创建接口已通过,后续失败 | 远端已消耗邮箱 | **failed**(永久) |

## 📜 许可

仅供学习与个人使用。使用本工具产生的一切后果(账号封禁 / 法律风险 / 第三方服务条款冲突)由使用者自行承担。

## 🙏 致谢

- [curl_cffi](https://github.com/lexiforest/curl_cffi) — TLS / HTTP2 指纹伪装
- [pyotp](https://github.com/pyauth/pyotp) — TOTP
- [Flask](https://flask.palletsprojects.com/) — Web 框架
