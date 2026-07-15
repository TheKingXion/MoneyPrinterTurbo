from dataclasses import dataclass


DEFAULT_LLM_PROVIDER_ID = "moonshot"


@dataclass(frozen=True, slots=True)
class LLMProviderField:
    config_suffix: str
    label_key: str
    required: bool = False
    secret: bool = False
    default_value: str = ""


@dataclass(frozen=True, slots=True)
class LLMProviderSpec:
    """Stable metadata shared by provider configuration and service adapters."""

    provider_id: str
    default_label: str
    adapter: str = "openai_compatible"
    api_key_url: str = ""
    default_model: str = ""
    default_base_url: str = ""
    requires_api_key: bool = True
    requires_model_name: bool = True
    requires_base_url: bool = True
    show_api_key: bool = True
    show_base_url: bool = True
    deprecated_models: tuple[str, ...] = ()
    deprecated_base_urls: tuple[str, ...] = ()
    extra_fields: tuple[LLMProviderField, ...] = ()

    @property
    def label_key(self) -> str:
        return f"llm_provider_label.{self.provider_id}"

    @property
    def tips_key(self) -> str:
        return f"llm_provider_tips.{self.provider_id}"

    def config_key(self, suffix: str) -> str:
        return f"{self.provider_id}_{suffix}"

    def resolve_model_name(self, configured_model: str | None) -> str:
        model_name = (configured_model or "").strip()
        if not model_name or model_name in self.deprecated_models:
            return self.default_model
        return model_name

    def resolve_base_url(self, configured_base_url: str | None) -> str:
        base_url = (configured_base_url or "").strip()
        deprecated_urls = {url.rstrip("/") for url in self.deprecated_base_urls}
        if not base_url or base_url.rstrip("/") in deprecated_urls:
            return self.default_base_url
        return base_url


LLM_PROVIDER_REGISTRY = (
    LLMProviderSpec(
        "moonshot", "Kimi / Moonshot AI",
        api_key_url="https://platform.kimi.com/console/api-keys?aff=MoneyPrinterTurbo",
        default_model="kimi-k2.7-code", default_base_url="https://api.moonshot.cn/v1",
    ),
    LLMProviderSpec(
        "openai", "OpenAI", api_key_url="https://platform.openai.com/api-keys",
        default_model="gpt-5.5", default_base_url="https://api.openai.com/v1",
    ),
    LLMProviderSpec(
        "gemini", "Google Gemini", adapter="gemini",
        api_key_url="https://aistudio.google.com/app/apikey",
        default_model="gemini-3.1-pro-preview", requires_base_url=False,
        show_base_url=False, deprecated_models=("gemini-pro", "gemini-1.0-pro"),
    ),
    LLMProviderSpec(
        "deepseek", "DeepSeek", api_key_url="https://platform.deepseek.com/api_keys",
        default_model="deepseek-v4-pro", default_base_url="https://api.deepseek.com",
    ),
    LLMProviderSpec(
        "qwen", "Alibaba Cloud Qwen", adapter="qwen",
        api_key_url="https://dashscope.console.aliyun.com/apiKey",
        default_model="qwen-max", requires_base_url=False, show_base_url=False,
    ),
    LLMProviderSpec(
        "azure", "Microsoft Azure OpenAI", adapter="azure",
        api_key_url=("https://portal.azure.com/#view/"
                     "Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI"),
        default_model="gpt-35-turbo",
    ),
    LLMProviderSpec(
        "volcengine", "ByteDance VolcEngine Ark",
        api_key_url=("https://www.volcengine.com/activity/ai618?utm_campaign=hw&"
                     "utm_content=hw&utm_medium=devrel_tool_web&utm_source=OWO&"
                     "utm_term=MoneyPrinterTurbo"),
        default_model="doubao-seed-2-1-turbo-260628",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
    LLMProviderSpec(
        "grok", "xAI Grok", api_key_url="https://console.x.ai/",
        default_model="grok-4.3", default_base_url="https://api.x.ai/v1",
    ),
    LLMProviderSpec(
        "minimax", "MiniMax", api_key_url="https://platform.minimax.io/",
        default_model="MiniMax-M3", default_base_url="https://api.minimax.io/v1",
    ),
    LLMProviderSpec(
        "mimo", "Xiaomi MiMo",
        api_key_url="https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call",
        default_model="mimo-v2.5-pro", default_base_url="https://api.xiaomimimo.com/v1",
    ),
    LLMProviderSpec(
        "cloudflare", "Cloudflare AI Gateway", adapter="cloudflare_ai_gateway",
        api_key_url="https://dash.cloudflare.com/", default_model="openai/gpt-4.1-mini",
        requires_base_url=False, show_base_url=False,
        deprecated_models=("@cf/meta/llama-3.1-8b-instruct",),
        extra_fields=(
            LLMProviderField("account_id", "Account ID", required=True),
            LLMProviderField("gateway_id", "Gateway ID", default_value="default"),
        ),
    ),
    LLMProviderSpec(
        "modelscope", "Alibaba ModelScope", adapter="modelscope",
        api_key_url="https://modelscope.cn/docs/model-service/API-Inference/intro",
        default_model="ZhipuAI/GLM-5.2",
        default_base_url="https://api-inference.modelscope.cn/v1/",
    ),
    LLMProviderSpec(
        "aihubmix", "AIHubMix", api_key_url="https://aihubmix.com/",
        default_model="gpt-5.4-mini", default_base_url="https://aihubmix.com/v1",
    ),
    LLMProviderSpec(
        "aimlapi", "AIML API", api_key_url="https://aimlapi.com/app/keys",
        default_model="openai/gpt-5-5", default_base_url="https://api.aimlapi.com/v1",
    ),
    LLMProviderSpec(
        "evolink", "EvoLink", api_key_url="https://evolink.ai/dashboard/keys",
        default_model="gpt-5.5", default_base_url="https://direct.evolink.ai/v1",
    ),
    LLMProviderSpec(
        "ollama", "Ollama", requires_api_key=False, show_api_key=False,
    ),
    LLMProviderSpec(
        "oneapi", "OneAPI", api_key_url="https://github.com/songquanpeng/one-api",
    ),
    LLMProviderSpec(
        "litellm", "LiteLLM", adapter="litellm", default_model="openai/gpt-4o-mini",
        requires_api_key=False, requires_base_url=False,
        show_api_key=False, show_base_url=False,
    ),
    LLMProviderSpec(
        "g4f", "G4f", adapter="g4f", default_model="gpt-3.5-turbo-16k-0613",
        requires_api_key=False, requires_base_url=False,
        show_api_key=False, show_base_url=False,
    ),
    LLMProviderSpec(
        "ernie", "Baidu ERNIE", adapter="ernie",
        api_key_url="https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
        default_model="ernie", requires_base_url=True,
        extra_fields=(
            LLMProviderField("secret_key", "Secret Key", required=True, secret=True),
        ),
    ),
    LLMProviderSpec(
        "groq", "Groq", api_key_url="https://console.groq.com/keys",
        default_model="llama-3.3-70b-versatile",
        default_base_url="https://api.groq.com/openai/v1",
    ),
    LLMProviderSpec(
        "pollinations", "Pollinations AI", api_key_url="https://enter.pollinations.ai/",
        default_model="openai-fast", default_base_url="https://gen.pollinations.ai/v1",
        deprecated_models=("default",),
        deprecated_base_urls=("https://text.pollinations.ai/openai",),
    ),
)

LLM_PROVIDERS = {provider.provider_id: provider for provider in LLM_PROVIDER_REGISTRY}

if len(LLM_PROVIDERS) != len(LLM_PROVIDER_REGISTRY):
    raise RuntimeError("duplicate LLM provider id in registry")


def get_llm_provider(provider_id: str) -> LLMProviderSpec | None:
    return LLM_PROVIDERS.get((provider_id or "").lower())


def normalize_provider_override(value: str | None, default_value: str | None) -> str:
    normalized_value = (value or "").strip()
    normalized_default = (default_value or "").strip()
    if normalized_value == normalized_default:
        return ""
    return normalized_value
