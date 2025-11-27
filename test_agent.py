# -*- coding: gbk -*-
import requests

AGENT_URL = (
    "https://1930052576475971.agentrun-data.cn-hangzhou.aliyuncs.com/"
    "agent-runtimes/RA4M2/endpoints/Default/invocations/openai/v1/chat/completions"
)

def call_ra4m2_agent(user_text: str) -> str:
    payload = {
        "messages": [
            {"role": "user", "content": user_text}
        ],
        "stream": False,
    }
    resp = requests.post(
        AGENT_URL,
        headers={"content-type": "application/json"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    print("Raw resp:", data)
    return data["choices"][0]["message"]["content"]

if __name__ == "__main__":
    q = "现在室内温度是多少？"
    ans = call_ra4m2_agent(q)
    print("智能体回答：", ans)
