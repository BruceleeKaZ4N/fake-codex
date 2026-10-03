import json
import os
import re
import asyncio
import requests
from pathlib import Path
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sandbox import safe_path, is_safe_command, WORKSPACE, set_sandbox_mode
from memory import MemoryStore

# ========== 从 secret.h 宏加载配置（模拟 C #define 宏替换） ==========
def load_secret_h(path: str = "secret.h") -> Dict[str, str]:
    """解析 secret.h 中 #define KEY "VALUE" 的形式，返回 dict。"""
    cfg: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到 {path}。请复制 secret.h 模板并填入你的 API Key。"
        )
    pattern = re.compile(r'^[ \t]*#[ \t]*define[ \t]+([A-Z_][A-Z0-9_]*)[ \t]+("(?:[^"\\]|\\.)*"|\S+)', re.MULTILINE)
    for m in pattern.finditer(p.read_text(encoding="utf-8")):
        key, raw_val = m.group(1), m.group(2)
        if raw_val.startswith('"') and raw_val.endswith('"'):
            cfg[key] = raw_val[1:-1]
        else:
            cfg[key] = raw_val
    return cfg

def mask_key(key: str) -> str:
    """脱敏日志：只保留前 6 位和后 4 位。"""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"

_cfg = load_secret_h()
LLM_API_URL = _cfg.get("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions")
LLM_API_KEY = _cfg.get("LLM_API_KEY", "")
MODEL_NAME = _cfg.get("MODEL_NAME", "deepseek-chat")
SANDBOX_MODE = int(_cfg.get("SANDBOX_MODE", "1"))
APPROVAL_REQUIRED = int(_cfg.get("APPROVAL_REQUIRED", "1"))

if not LLM_API_KEY or LLM_API_KEY.startswith("sk-PUT-YOUR"):
    print("[启动警告] secret.h 中的 LLM_API_KEY 还是占位符，请先填入真实 Key。")

set_sandbox_mode(SANDBOX_MODE)
print(f"[配置] 模型={MODEL_NAME}  接口={LLM_API_URL}  Key={mask_key(LLM_API_KEY)}  沙箱档={SANDBOX_MODE}")
# ======================================================================

app = FastAPI(title="FullMiniCodex")
mem_store = MemoryStore()
SESSIONS: Dict[str, Dict] = {}

HTML_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FullMiniCodex</title>
    <style>
        * {box-sizing: border-box; margin:0; padding:0; font-family: system-ui, sans-serif;}
        body {display: grid; grid-template-columns: 260px 1fr 320px; height:100vh; overflow:hidden;}
        .sidebar-left {background:#1f2937; color:#fff; padding:16px;}
        .chat-area {display:grid; grid-template-rows:1fr 130px;}
        .msg-container {padding:16px; overflow-y:auto;}
        .msg {margin:8px 0; padding:10px; border-radius:8px; white-space:pre-wrap;}
        .user {background:#dbeafe; margin-left:auto; max-width:85%;}
        .agent {background:#eef2ff; margin-right:auto; max-width:85%;}
        .tool {background:#fef3c7; max-width:85%; margin:4px auto; font-size:13px;}
        .input-box {padding:12px; border-top:1px solid #ddd; display:flex; gap:8px;}
        textarea {flex:1; padding:8px; font-size:14px;}
        button {padding:8px 14px; cursor:pointer;}
        .sidebar-right {background:#f9fafb; border-left:1px solid #ddd; padding:12px; overflow-y:auto; font-size:13px;}
        .modal {display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); align-items:center; justify-content:center;}
        .modal-box {background:white; padding:20px; border-radius:10px; max-width:420px;}
    </style>
</head>
<body>
    <div class="sidebar-left">
        <h2>FullMiniCodex</h2>
        <p id="sessionId"></p>
        <hr style="margin:10px 0; border-color:#4b5563;">
        <p>可用工具：</p>
        <ul style="padding-left:18px; margin-top:6px; font-size:13px;">
            <li>read file</li>
            <li>write file</li>
            <li>ls list</li>
            <li>shell cmd</li>
            <li>memory</li>
        </ul>
    </div>
    <div class="chat-area">
        <div class="msg-container" id="chatBox"></div>
        <div class="input-box">
            <textarea id="userInput" placeholder="输入任务，例如：列出目录文件，读取文件，写代码"></textarea>
            <button onclick="sendMessage()">发送</button>
        </div>
    </div>
    <div class="sidebar-right">
        <h3>工具输出日志</h3>
        <div id="toolLog"></div>
    </div>

    <div class="modal" id="approveModal">
        <div class="modal-box">
            <h3>权限确认</h3>
            <p id="modalText"></p>
            <div style="margin-top:12px; display:flex; gap:10px;">
                <button onclick="approve(true)">允许</button>
                <button onclick="approve(false)">拒绝</button>
            </div>
        </div>
    </div>

<script>
const ws = new WebSocket(`ws://${location.host}/ws`);
const chatBox = document.getElementById("chatBox");
const toolLog = document.getElementById("toolLog");
const userInput = document.getElementById("userInput");
const sessionIdSpan = document.getElementById("sessionId");
const modal = document.getElementById("approveModal");
const modalText = document.getElementById("modalText");
let pendingCallback = null;

ws.onopen = () => {
    const sid = Date.now().toString();
    sessionIdSpan.textContent = `SID:${sid}`;
};
ws.onmessage = async (event) => {
    const data = JSON.parse(event.data);
    switch(data.type){
        case "text":
            addMsg(data.content, "agent");
            break;
        case "tool":
            addToolLog(data.content);
            break;
        case "ask_approve":
            modalText.innerText = data.msg;
            modal.style.display = "flex";
            pendingCallback = data.callback_id;
            break;
    }
};
ws.onclose = () => addMsg("连接断开", "agent");

function addMsg(text, cls){
    const div = document.createElement("div");
    div.className = "msg "+cls;
    div.innerText = text;
    chatBox.appendChild(div);
    chatBox.scrollTop = chatBox.scrollHeight;
}
function addToolLog(text){
    const div = document.createElement("div");
    div.style.padding="4px 0";
    div.innerText = text;
    toolLog.appendChild(div);
    toolLog.scrollTop = toolLog.scrollHeight;
}
function sendMessage(){
    const txt = userInput.value.trim();
    if(!txt) return;
    addMsg(txt, "user");
    ws.send(JSON.stringify({prompt: txt}));
    userInput.value = "";
}
function approve(ok){
    modal.style.display = "none";
    ws.send(JSON.stringify({type:"approve", callback_id:pendingCallback, allow:ok}));
}
</script>
</body>
</html>
"""

# 工具调度
from sandbox import is_read_only, is_full_auto

async def call_tool(tool_name: str, args: dict, ws: WebSocket, callback_id: str) -> str:
    # read-only 档：任何写/执行操作一律拒绝
    if is_read_only() and tool_name in ("write", "shell"):
        return f"拒绝：当前为 read-only 沙箱档，{tool_name} 被禁止"

    if tool_name == "shell":
        cmd = args["cmd"]
        if not is_safe_command(cmd):
            return "拒绝：命令存在高危字符"
        if APPROVAL_REQUIRED and not is_full_auto():
            await ws.send_text(json.dumps({"type":"ask_approve", "msg":f"执行shell：{cmd}", "callback_id":callback_id}))
            return await wait_approval(ws, callback_id, lambda: asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return out.decode("utf-8", errors="ignore") + err.decode("utf-8", errors="ignore")

    elif tool_name == "read":
        path_raw = args["path"]
        fp = safe_path(path_raw)
        if not fp or not fp.exists():
            return "文件不存在或者越权"
        return fp.read_text(encoding="utf-8", errors="ignore")

    elif tool_name == "write":
        path_raw = args["path"]
        content = args["content"]
        fp = safe_path(path_raw)
        if not fp:
            return "路径非法"
        if APPROVAL_REQUIRED and not is_full_auto():
            await ws.send_text(json.dumps({"type":"ask_approve", "msg":f"写入文件：{path_raw}", "callback_id":callback_id}))
            return await wait_approval(ws, callback_id, lambda: (fp.write_text(content, encoding="utf-8"), "写入成功"))
        fp.write_text(content, encoding="utf-8")
        return "写入成功"

    elif tool_name == "ls":
        items = list(WORKSPACE.iterdir())
        return "\n".join([i.name for i in items])

    elif tool_name == "memory":
        return json.dumps(mem_store.get_all(), ensure_ascii=False, indent=2)
    return "未知工具"

async def wait_approval(ws: WebSocket, target_id: str, exec_func):
    while True:
        msg = await ws.receive_text()
        data = json.loads(msg)
        if data.get("type") == "approve" and data["callback_id"] == target_id:
            if not data["allow"]:
                return "用户拒绝本次操作"
            res = exec_func()
            if isinstance(res, tuple):
                return res[1]
            proc = await res
            out, err = await proc.communicate()
            return out.decode("utf-8", errors="ignore") + err.decode("utf-8", errors="ignore")

async def llm_invoke(messages: List[dict]) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature":0.2
    }
    headers = {"Authorization":f"Bearer {LLM_API_KEY}", "Content-Type":"application/json"}
    resp = requests.post(LLM_API_URL, json=payload, headers=headers, timeout=90)
    j = resp.json()
    if "choices" not in j:
        return f"LLM调用失败: {j}"
    return j["choices"][0]["message"]["content"]

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sid = str(id(ws))
    SESSIONS[sid] = {"history":[{"role":"system", "content":"你是Agent，可以调用工具：read,write,ls,shell,memory。当需要操作文件/执行命令时调用工具，需要向用户申请权限。工具返回结果之后继续完成任务。"}], "callback_counter":0}
    session = SESSIONS[sid]
    try:
        while True:
            raw = await ws.receive_text()
            payload = json.loads(raw)
            if payload.get("type") == "approve":
                continue
            prompt = payload["prompt"]
            session["history"].append({"role":"user", "content": prompt})
            # Agent循环
            for _ in range(8):
                llm_resp = await llm_invoke(session["history"])
                session["history"].append({"role":"assistant", "content": llm_resp})
                # 简易工具解析标记：【TOOL:工具名|json参数】
                if "【TOOL:" in llm_resp:
                    start = llm_resp.find("【TOOL:") + len("【TOOL:")
                    end = llm_resp.find("】", start)
                    tool_block = llm_resp[start:end]
                    tn, arg_str = tool_block.split("|",1)
                    args = json.loads(arg_str)
                    cid = str(session["callback_counter"])
                    session["callback_counter"] +=1
                    await ws.send_text(json.dumps({"type":"tool", "content":f"调用工具 {tn} 参数 {args}"}))
                    tool_result = await call_tool(tn, args, ws, cid)
                    session["history"].append({"role":"tool", "content":tool_result})
                    await ws.send_text(json.dumps({"type":"tool", "content":f"工具返回：{tool_result}"}))
                    continue
                await ws.send_text(json.dumps({"type":"text", "content": llm_resp}))
                break
    except WebSocketDisconnect:
        del SESSIONS[sid]
    except Exception as e:
        await ws.send_text(json.dumps({"type":"text", "content":f"服务异常 {str(e)}"}))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
