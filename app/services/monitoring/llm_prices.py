"""
LLM list prices per model (platform_config LLM_MODEL_PRICES), for the monitoring view's cost
estimates. Seeded with the providers' published list prices; HR_ADMIN edits them per model. Costs
are always shown as estimates: they ignore free tiers, batch / cached-token discounts and taxes, and
billing is the provider's.

    {"currency": "USD", "unit": "per 1M tokens",
     "models": {"gemini-3.1-flash-lite": {"input": 0.25, "output": 1.5, "source": "..."}}}

Output tokens include thinking tokens, as the call log records them and as Google bills them.
"""
import json

LLM_MODEL_PRICES_KEY = "LLM_MODEL_PRICES"
_GEMINI_SOURCE = ("Gemini Developer API pricing, Standard tier, text input "
                  "(https://ai.google.dev/gemini-api/docs/pricing), checked 2026-10-30")
DEFAULT_LLM_MODEL_PRICES = {
    "currency": "USD",
    "unit": "per 1M tokens",
    "models": {
        "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "source": _GEMINI_SOURCE},
        "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "source": _GEMINI_SOURCE},
    },
}
MAX_PRICE = 1000.0          # per 1M tokens - anything above is a typo


class InvalidPrices(ValueError):
    pass


def validate_prices(payload: dict) -> dict:
    """The prices to store: {"currency", "unit", "models": {model: {"input", "output", "source"?}}}."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), dict):
        raise InvalidPrices("Give the prices as {\"models\": {\"<model>\": {\"input\": n, \"output\": n}}}.")
    models = {}
    for model, price in payload["models"].items():
        name = str(model).strip()
        if not name or len(name) > 120:
            raise InvalidPrices("Each model needs a name (up to 120 characters).")
        if not isinstance(price, dict):
            raise InvalidPrices(f"{name}: give input and output prices.")
        entry = {}
        for side in ("input", "output"):
            value = price.get(side)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_PRICE:
                raise InvalidPrices(f"{name}: the {side} price must be a number from 0 to {MAX_PRICE:g} "
                                    f"(USD per 1M tokens).")
            entry[side] = float(value)
        if price.get("source"):
            entry["source"] = str(price["source"])[:300]
        models[name] = entry
    return {"currency": "USD", "unit": "per 1M tokens", "models": models}


def load_prices(config_repository) -> dict:
    raw = config_repository.get_configs_by_keys([LLM_MODEL_PRICES_KEY]).get(LLM_MODEL_PRICES_KEY)
    try:
        return validate_prices(json.loads(raw)) if raw else {"currency": "USD", "unit": "per 1M tokens", "models": {}}
    except (ValueError, TypeError):
        return {"currency": "USD", "unit": "per 1M tokens", "models": {}}


def estimate_cost(prices: dict, model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """USD for these tokens at the model's list price; None when the model has no price."""
    price = (prices.get("models") or {}).get(model or "")
    if price is None:
        return None
    return ((input_tokens or 0) * price["input"] + (output_tokens or 0) * price["output"]) / 1_000_000
