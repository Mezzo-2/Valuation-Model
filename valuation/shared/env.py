"""项目模型配置：Comein NewAPI。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "https://new-api.comein.cn/v1"
DEFAULT_MODEL = "gpt-6-luna"
_LOADED = False


def _env(*keys: str) -> str:
    for key in keys:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def load_env(path: Path | None = None) -> Path | None:
    """把项目根目录 .env 写入 os.environ。已有环境变量不覆盖。"""
    global _LOADED
    env_path = path or ROOT / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
    os.environ.setdefault("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    _LOADED = True
    return env_path if env_path.is_file() else None


def api_key() -> str:
    load_env()
    key = _env("OPENAI_API_KEY", "VALUATION_LLM_API_KEY", "SPIKE_LLM_API_KEY")
    if not key:
        raise RuntimeError(
            "缺少 OPENAI_API_KEY。复制 .env.example 为 .env，填入 Comein 网关 key。"
        )
    return key


def base_url() -> str:
    load_env()
    url = (os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def model_id() -> str:
    load_env()
    raw = _env("VALUATION_SPLIT_MODEL", "VALUATION_MODEL", "SPIKE_SPLIT_MODEL", "SPIKE_MODEL") or DEFAULT_MODEL
    if ":" in raw:
        prefix, name = raw.split(":", 1)
        if prefix != "openai":
            raise RuntimeError(f"当前网关只走 OpenAI 兼容接口，不支持 {prefix}:{name}")
        raw = name
    return raw or DEFAULT_MODEL


def _use_responses_api(name: str) -> bool:
    del name
    forced = _env("VALUATION_LLM_API", "SPIKE_LLM_API").lower()
    if forced in {"chat", "completions"}:
        return False
    return True


def _chat_tools_unavailable(name: str) -> bool:
    """gpt-6.1-sol 的 chat completions 不能带工具：关推理会被拒，开着推理也不能调用工具。"""
    return "sol" in name.lower()


def _chat_profile(name: str) -> OpenAIModelProfile | None:
    """Comein 上的 DeepSeek 不接受 tool_choice=required，也不接受点名强制某个工具。"""
    if "deepseek" in name.lower():
        return OpenAIModelProfile(
            openai_supports_tool_choice_required=False,
            openai_supports_strict_tool_definition=False,
        )
    return None


class _ConfiguredChatModel(OpenAIChatModel):
    """按模型补上推理强度。luna 不能带默认推理，DeepSeek Flash 用 max。"""

    def __init__(
        self,
        model_name: str,
        *,
        provider: OpenAIProvider,
        effort: str | None,
        profile: OpenAIModelProfile | None = None,
    ):
        super().__init__(model_name, provider=provider, profile=profile)
        self._effort = effort

    async def request(self, messages, model_settings, model_request_parameters):
        settings = dict(model_settings or {})
        if self._effort:
            settings.setdefault("openai_reasoning_effort", self._effort)
        return await super().request(messages, settings, model_request_parameters)


def _chat_tools_need_reasoning_off(name: str) -> bool:
    return "luna" in name.lower()


def reasoning_effort(name: str | None = None) -> str | None:
    """DeepSeek Flash 的 max 是推理强度，不是另一个模型名。"""
    load_env()
    forced = _env("VALUATION_REASONING_EFFORT", "SPIKE_REASONING_EFFORT").lower()
    if forced in {"none", "off"}:
        return None
    if forced:
        return forced
    model_name = (name or model_id()).lower()
    if "luna" in model_name:
        return "high"
    if model_name in {"deepseek-flash", "deepseek-v4-flash"}:
        return "max"
    return None


def build_chat_model(name: str | None = None, *, api: str | None = None):
    """判断 Agent 共用：key + Comein /v1。默认 Responses。"""
    import httpx2

    model_name = name or model_id()
    http_client = httpx2.AsyncClient(
        trust_env=False,
        timeout=httpx2.Timeout(600.0, connect=20.0),
    )
    provider = OpenAIProvider(
        base_url=base_url(),
        api_key=api_key(),
        http_client=http_client,
    )
    effort = reasoning_effort(model_name)
    profile = _chat_profile(model_name)
    settings = {"openai_reasoning_effort": effort} if effort else None
    forced = (api or "").strip().lower()
    use_chat = forced in {"chat", "completions"} or (
        forced not in {"responses", "response"} and not _use_responses_api(model_name)
    )
    if use_chat and not _chat_tools_unavailable(model_name):
        chat_effort = "none" if _chat_tools_need_reasoning_off(model_name) else effort
        return _ConfiguredChatModel(model_name, provider=provider, effort=chat_effort, profile=profile)
    return OpenAIResponsesModel(
        model_name,
        provider=provider,
        profile=profile,
        settings=settings,
    )


def describe() -> str:
    key = api_key()
    shown = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "(set)"
    kind = "responses" if _use_responses_api(model_id()) else "chat"
    effort = reasoning_effort() or "-"
    return f"model={model_id()}  api={kind}  reasoning={effort}  base={base_url()}  key={shown}"


if __name__ == "__main__":
    print(describe())
