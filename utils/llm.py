import os
import litellm

litellm.telemetry = False

# LiteLLM model string examples:
#   ollama_chat/gpt-oss:20b  (Ollama Cloud — set OLLAMA_API_BASE + OLLAMA_API_KEY)
#   groq/llama-3.1-8b-instant  (set GROQ_API_KEY)
#   openai/gpt-4o-mini         (set OPENAI_API_KEY)
LLM_MODEL   = os.environ.get("LLM_MODEL",   "ollama_chat/gpt-oss:20b")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "90") or "90")

_api_base = os.environ.get("OLLAMA_API_BASE") or "https://api.ollama.com"
_api_key  = os.environ.get("OLLAMA_API_KEY")  or os.environ.get("OPENAI_API_KEY") or None


def chat(system_prompt: str, user_message: str, max_tokens: int = 2048, temperature: float = 0.3) -> str:
    """Send a chat request via LiteLLM. Returns the assistant reply as a string."""
    kwargs = dict(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=LLM_TIMEOUT,
    )
    if _api_base:
        kwargs["api_base"] = _api_base
    if _api_key:
        kwargs["api_key"] = _api_key

    response = litellm.completion(**kwargs)
    msg = response.choices[0].message
    # Reasoning models (e.g. gpt-oss:20b) put output in content; thinking goes to
    # reasoning_content. If content is empty, fall back to reasoning_content.
    content = (msg.content or "").strip()
    if not content:
        content = (getattr(msg, "reasoning_content", None) or "").strip()
    return content
