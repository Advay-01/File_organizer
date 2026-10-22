"""Provider-agnostic LLM caller built on litellm.

Model strings pick the provider, e.g.:
    gemini/gemini-2.5-flash     (GEMINI_API_KEY)
    openai/gpt-5.6-luna         (OPENAI_API_KEY)
    deepseek/deepseek-chat      (DEEPSEEK_API_KEY)
Pass several models to get automatic fallback in order.
"""
from __future__ import annotations
import time
import random
import json
import re
from typing import Callable, List, Optional

from pydantic import ValidationError

from app.models.schemas import OrganizerResponse

# A completion function: (model, messages) -> raw text. Injectable for tests.
CompletionFn = Callable[[str, List[dict]], str]


def _litellm_completion(model: str, messages: List[dict]) -> str:
    import litellm

    resp = litellm.completion(
        model=model,
        messages=messages,
        temperature=0.1,
        response_format={"type": "json_object"},
        timeout=120,
    )
    return resp.choices[0].message.content or ""


def extract_json(text: str) -> dict:
    """Parse JSON even if the model wrapped it in markdown fences or prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("No JSON object found in model output")


class LLMClassifier:
    def __init__(
        self,
        models: List[str],
        completion_fn: Optional[CompletionFn] = None,
        retries_per_model: int = 1,
    ):
        if not models:
            raise ValueError("At least one model is required")
        self.models = models
        self.completion_fn = completion_fn or _litellm_completion
        self.retries_per_model = retries_per_model

    def classify(self, system_prompt: str, user_prompt: str) -> OrganizerResponse:
        """Try models in order, retrying temporary provider failures."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        errors: List[str] = []

        for model in self.models:
            for attempt in range(self.retries_per_model + 1):
                try:
                    raw = self.completion_fn(model, messages)
                    return OrganizerResponse.model_validate(extract_json(raw))

                except (ValueError, ValidationError) as e:
                    errors.append(
                        f"{model} (bad output, attempt {attempt + 1}): {e}"
                    )

                except Exception as e:
                    errors.append(
                        f"{model} (attempt {attempt + 1}): "
                        f"{type(e).__name__}: {e}"
                    )

                    # Retry temporary provider/network failures.
                    message = str(e).lower()
                    temporary = any(term in message for term in [
                        "503", "unavailable", "overloaded",
                        "429", "rate limit", "timeout",
                        "connection", "500", "502", "504",
                    ])

                    if not temporary:
                        break

                if attempt < self.retries_per_model:
                    delay = min(2 ** (attempt + 1), 16)
                    time.sleep(delay + random.uniform(0, 1))

        raise RuntimeError("All models failed:\n  " + "\n  ".join(errors))

