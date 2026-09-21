"""项目模型配置：Comein NewAPI。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "https://new-api.comein.cn/v1"
DEFAULT_MODEL = "gpt-5.6-luna"
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
    forced = _env("VALUATION_LLM_API", "SPIKE_LLM_API").lower()
    if forced in {"responses", "response"}:
        return True
    if forced in {"chat", "completions"}:
        return False
    lowered = name.lower()
    return lowered.startswith("gpt-5") or "luna" in lowered


def build_chat_model(name: str | None = None, *, api: str | None = None):
    """判断 Agent 共用：key + Comein /v1。gpt-5 / luna 默认 Responses；多轮工具用 api='chat'。"""
    import httpx2

    model_name = name or model_id()
    http_client = httpx2.AsyncClient(
        trust_env=False,
        timeout=httpx2.Timeout(120.0, connect=20.0),
    )
    provider = OpenAIProvider(
        base_url=base_url(),
        api_key=api_key(),
        http_client=http_client,
    )
    forced = (api or "").strip().lower()
    if forced in {"chat", "completions"}:
        return OpenAIChatModel(model_name, provider=provider)
    if forced in {"responses", "response"} or _use_responses_api(model_name):
        return OpenAIResponsesModel(model_name, provider=provider)
    return OpenAIChatModel(model_name, provider=provider)


def describe() -> str:
    key = api_key()
    shown = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "(set)"
    kind = "responses" if _use_responses_api(model_id()) else "chat"
    return f"model={model_id()}  api={kind}  base={base_url()}  key={shown}"


if __name__ == "__main__":
    print(describe())
