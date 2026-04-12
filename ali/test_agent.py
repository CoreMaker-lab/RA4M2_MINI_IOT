# -*- coding: gbk -*-
import json
import requests

BASE_URL = "https://1930052576475971.agentrun-data.cn-hangzhou.aliyuncs.com/agent-runtimes/RA4M2_1/endpoints/Default/invocations"

CANDIDATE_URLS = [
    f"{BASE_URL}/openai/v1/chat/completions",
    f"{BASE_URL}/v1/chat/completions",
    BASE_URL,
]

PAYLOAD = {
    "messages": [
        {
            "role": "user",
            "content": "现在室内温度是多少？"
        }
    ],
    "stream": False
}


def try_request(url: str, payload: dict):
    print("=" * 80)
    print("Trying URL:")
    print(url)
    print("-" * 80)

    try:
        resp = requests.post(
            url,
            headers={"content-type": "application/json"},
            json=payload,
            timeout=60
        )
    except requests.exceptions.RequestException as e:
        print("Request error:", e)
        return False, None

    print("status_code:", resp.status_code)
    print("raw response:")
    print(resp.text)

    try:
        data = resp.json()
        print("\njson pretty:")
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        data = None

    # 只要不是 404，就说明这个路径更接近正确
    if resp.status_code != 404:
        return True, resp

    return False, resp


def main():
    success = False
    final_resp = None

    for url in CANDIDATE_URLS:
        ok, resp = try_request(url, PAYLOAD)
        if ok:
            success = True
            final_resp = resp
            break

    print("\n" + "=" * 80)
    if success:
        print("Found a usable endpoint.")
    else:
        print("All candidate URLs returned 404 or failed.")
        print("Please check:")
        print("1. AgentRun endpoint path")
        print("2. Whether OpenAI protocol is enabled")
        print("3. Whether the Agent service is running normally")

    if final_resp is not None:
        print("\nFinal URL used:")
        print(final_resp.request.url)


if __name__ == "__main__":
    main()