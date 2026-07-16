from types import SimpleNamespace

from app.services import api_usage


def test_extracts_openai_and_gemini_usage():
    openai = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=8,
            total_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
    )
    gemini = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=7,
            candidates_token_count=5,
            total_token_count=12,
            cached_content_token_count=1,
            thoughts_token_count=2,
        )
    )

    assert api_usage.extract_token_usage(openai) == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "cached_tokens": 3,
        "reasoning_tokens": 2,
    }
    assert api_usage.extract_token_usage(gemini)["total_tokens"] == 12


def test_store_aggregates_filters_and_does_not_persist_content(tmp_path):
    store = api_usage.ApiUsageStore(tmp_path / "usage.db")
    with api_usage.usage_context("ideas", "generate_batch_ideas"):
        store.record(
            provider="openai",
            model="gpt-test",
            prompt="PRIVATE PROMPT",
            output="PRIVATE RESPONSE",
            response={
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                }
            },
            duration_seconds=0.25,
        )
    store.record(
        provider="gemini",
        model="gemini-test",
        prompt="estimated input",
        category="tts",
        operation="gemini_tts",
    )

    report = store.report(providers=["openai"])
    assert report["totals"] == {
        "requests": 1,
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "estimated_requests": 0,
        "failed_requests": 0,
    }
    assert report["by_category"][0]["category"] == "ideas"
    assert store.report(categories=["tts"])["totals"]["estimated_requests"] == 1
    raw_database = (tmp_path / "usage.db").read_bytes()
    assert b"PRIVATE PROMPT" not in raw_database
    assert b"PRIVATE RESPONSE" not in raw_database


def test_token_estimate_handles_empty_and_unicode_text():
    assert api_usage.estimate_tokens("") == 0
    assert api_usage.estimate_tokens("hola") == 1
    assert api_usage.estimate_tokens("historia inspiradora") > 1
