"""Find out WHERE the time goes on your machine.

    python -m app.diag

Prints timings (ms) for each model in ORGANIZER_MODELS: a cold request, a warm request,
and a 4-file classification, plus the same request with the provider's default
"thinking" so you can see how much it costs. Paste the output if something is slow.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from dotenv import load_dotenv

from app.core import llm_http
from app.core.fast_planner import _classify_system

SAMPLE = [(0, "notes1.txt", "Neural networks, backpropagation and transformer attention"),
          (1, "bst.py", "binary search tree insert delete traversal"),
          (2, "invoice_oct.pdf", "Invoice total amount due tax payment"),
          (3, "random.txt", "hello")]
MSGS = [{"role": "system", "content": _classify_system(["AI", "DSA", "Finance"])},
        {"role": "user", "content": "\n".join(f"{i}|{n}|{s}" for i, n, s in SAMPLE)}]


async def timed(label, model, params=None):
    t = time.perf_counter()
    try:
        out = await llm_http.direct_call(model, MSGS, params, timeout=120)
        print(f"  {label:<34}{(time.perf_counter() - t) * 1000:8.0f} ms   {out.strip()[:70]!r}")
    except Exception as e:
        print(f"  {label:<34}{(time.perf_counter() - t) * 1000:8.0f} ms   FAILED: {str(e)[:150]}")


async def main() -> int:
    load_dotenv()
    t = time.perf_counter()
    try:
        import litellm  # noqa: F401
        print(f"import litellm: {time.perf_counter() - t:.1f}s  (fast mode skips this for known providers)")
    except ImportError:
        print("litellm not installed (fine: fast mode does not need it for known providers)")

    models = [m.strip() for m in os.getenv("ORGANIZER_MODELS", "").split(",") if m.strip()] \
        or ["deepseek/deepseek-flash", "gemini/gemini-3.8-flash"]
    for m in models:
        prov = llm_http.split_model(m)[0]
        print(f"\n{m}")
        if not llm_http.is_supported(m):
            print("  not a direct provider (would use litellm); skipped")
            continue
        await timed("cold, fast settings", m)
        await timed("warm, fast settings", m)
        if prov == "deepseek":
            await timed("DEFAULT thinking (slow baseline)", m, {"deepseek": {"thinking": {"type": "enabled"}}})
        elif prov == "gemini":
            await timed("DEFAULT thinking (slow baseline)", m, {"gemini": {"reasoning_effort": None}})
    print("\nIf the 'fast settings' rows are still several seconds, that is the provider's own "
          "latency/rate limit; list a second model in ORGANIZER_MODELS so small runs race both.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

