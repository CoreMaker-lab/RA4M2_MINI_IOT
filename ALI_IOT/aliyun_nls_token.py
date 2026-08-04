from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

try:
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest
except ImportError:  # 由运行时给出更友好的安装提示
    AcsClient = None
    CommonRequest = None


NLS_TOKEN_REGION = "cn-shanghai"
NLS_TOKEN_DOMAIN = "nls-meta.cn-shanghai.aliyuncs.com"
NLS_TOKEN_API_VERSION = "2019-02-28"
NLS_TOKEN_ACTION = "CreateToken"


class NlsTokenError(RuntimeError):
    """获取或刷新阿里云智能语音交互Token失败。"""


@dataclass(frozen=True)
class NlsTokenInfo:
    token: str
    expire_time: int

    @property
    def expire_text(self) -> str:
        return time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(self.expire_time),
        )


class NlsTokenManager:
    """
    使用AccessKey自动获取并缓存阿里云NLS Token。

    Token即将到期时自动重新调用CreateToken。AccessKey只从环境变量读取，
    不应硬编码到源代码中。
    """

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        refresh_before_seconds: int = 600,
    ) -> None:
        access_key_id = access_key_id.strip()
        access_key_secret = access_key_secret.strip()

        if not access_key_id:
            raise NlsTokenError("AccessKey ID不能为空。")
        if not access_key_secret:
            raise NlsTokenError("AccessKey Secret不能为空。")
        if refresh_before_seconds < 0:
            raise NlsTokenError("提前刷新时间不能小于0秒。")

        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._refresh_before_seconds = refresh_before_seconds
        self._token_info: NlsTokenInfo | None = None

    @classmethod
    def from_env(
        cls,
        refresh_before_seconds: int = 600,
    ) -> "NlsTokenManager":
        access_key_id = os.getenv("ALIYUN_AK_ID", "")
        access_key_secret = os.getenv("ALIYUN_AK_SECRET", "")

        missing: list[str] = []
        if not access_key_id.strip():
            missing.append("ALIYUN_AK_ID")
        if not access_key_secret.strip():
            missing.append("ALIYUN_AK_SECRET")

        if missing:
            raise NlsTokenError(
                "缺少环境变量：" + ", ".join(missing)
            )

        return cls(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            refresh_before_seconds=refresh_before_seconds,
        )

    def invalidate(self) -> None:
        """使当前缓存失效，下次调用get_token()时强制重新获取。"""
        self._token_info = None

    def get_token(self, force_refresh: bool = False) -> str:
        now = int(time.time())

        should_refresh = (
            force_refresh
            or self._token_info is None
            or now >= (
                self._token_info.expire_time
                - self._refresh_before_seconds
            )
        )

        if should_refresh:
            self._token_info = self._create_token()

            print(
                "NLS Token updated, expires at: "
                f"{self._token_info.expire_text}"
            )

        return self._token_info.token

    @property
    def expire_time(self) -> int:
        if self._token_info is None:
            return 0
        return self._token_info.expire_time

    def _create_token(self) -> NlsTokenInfo:
        if AcsClient is None or CommonRequest is None:
            raise NlsTokenError(
                "未安装阿里云Python SDK。请执行："
                "pip install aliyun-python-sdk-core==2.15.1"
            )

        try:
            client = AcsClient(
                self._access_key_id,
                self._access_key_secret,
                NLS_TOKEN_REGION,
            )

            request = CommonRequest()
            request.set_method("POST")
            request.set_domain(NLS_TOKEN_DOMAIN)
            request.set_version(NLS_TOKEN_API_VERSION)
            request.set_action_name(NLS_TOKEN_ACTION)

            response = client.do_action_with_exception(request)

            if isinstance(response, bytes):
                response_text = response.decode("utf-8")
            else:
                response_text = str(response)

            payload = json.loads(response_text)
        except Exception as exc:
            raise NlsTokenError(
                f"调用CreateToken失败：{exc}"
            ) from exc

        try:
            token = str(payload["Token"]["Id"]).strip()
            expire_time = int(payload["Token"]["ExpireTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NlsTokenError(
                "CreateToken返回结构异常："
                + json.dumps(payload, ensure_ascii=False)[:1000]
            ) from exc

        now = int(time.time())

        if not token:
            raise NlsTokenError("CreateToken返回的Token.Id为空。")
        if expire_time <= now:
            raise NlsTokenError(
                f"CreateToken返回的ExpireTime无效：{expire_time}"
            )

        return NlsTokenInfo(
            token=token,
            expire_time=expire_time,
        )
