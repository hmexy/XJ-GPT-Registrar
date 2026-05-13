# -*- coding: utf-8 -*-
"""
注册成功后的"自动触发 flow"配置 · 模板。

⚠️ 真正的凭据请填到本地 config/flow_trigger.py(已被 .gitignore 排除),
不要直接修改本文件然后提交。

部署/复刻流程:
    cp config/flow_trigger.example.py config/flow_trigger.py
    # 编辑 config/flow_trigger.py 填入实际值
"""

# 总开关:False 则跳过整个 flow 调用
ENABLE_FLOW_TRIGGER = False

# 目标接口
FLOW_TRIGGER_URL = "https://your-flow-host.example.com/api/flows/from-token"

# Bearer 令牌(写到 Authorization 头)
FLOW_TRIGGER_BEARER = "YOUR_BEARER_TOKEN_HERE"

# Cookie(保留与抓包一致即可;服务端校验 plus_admin_session)
FLOW_TRIGGER_COOKIE = (
    "http_Path=/your/path; "
    "plus_admin_session=YOUR_SESSION_HERE"
)

# 请求体模板。运行时会把 access_token 字段替换成本次注册成功的 token,
# 其他字段保持原样发送。
FLOW_TRIGGER_PAYLOAD = {
    "agent_id": "",
    "unlink_after_success": True,
    "plan_name": "chatgptplusplan",
    "ui_mode": "hosted",
    "region": "ID",
    "workspace_name": "MyTeam",
    "seat_quantity": 5,
    "access_token": "",  # 由 trigger_flow() 在调用时填入
}

# 单次请求超时(秒)。设短一点,反正 fire-and-forget。
FLOW_TRIGGER_TIMEOUT = 10
