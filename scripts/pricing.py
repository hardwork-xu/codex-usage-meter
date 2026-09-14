"""Pure Decimal estimates from observed tokens; never an account charge or balance.

The dated table is the public ChatGPT/Codex credit schedule, not API pricing.
Only explicitly supported model IDs are matched; aliases are not guessed.
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from typing import Any


SOURCE_URL = "https://learn.chatgpt.com/docs/pricing"
SPEED_SOURCE_URL = "https://learn.chatgpt.com/docs/agent-configuration/speed"
RATE_DATE = "2026-09-14"
_MILLION = Decimal(1_000_000)
_DISPLAY = Decimal("0.000001")
_MAX_COUNTER = 10**30
# Uncached input / cached input / output credits per million tokens.
RATES = {
    "gpt-6-astra": ("GPT-6 Astra", "250", "25", "1250"),
    "gpt-5.6-sol": ("GPT-5.6 Sol", "100", "10", "500"),
    "gpt-5.6-terra": ("GPT-5.6 Terra", "50", "5", "300"),
    "gpt-5.6-luna": ("GPT-5.6 Luna", "5", "0.5", "30"),
    "gpt-5.5": ("GPT-5.5", "125", "12.5", "750"),
    "gpt-5.4": ("GPT-5.4", "62.5", "6.25", "375"),
    "gpt-5.4-mini": ("GPT-5.4 mini", "18.75", "1.875", "113"),
}
FAST_MULTIPLIERS = {
    "gpt-6-astra": Decimal("2.5"),
    "gpt-5.6-sol": Decimal("2.5"),
    "gpt-5.6-terra": Decimal("2.5"),
    "gpt-5.6-luna": Decimal("2.5"),
    "gpt-5.5": Decimal("2.5"),
    "gpt-5.4": Decimal("2"),
}


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 60:
        raise ValueError("换算设置无效。")
    result = Decimal(str(value))
    if not result.is_finite() or not 0 <= result <= 1_000_000_000:
        raise ValueError("换算设置无效。")
    if result and result.adjusted() < -30:
        raise ValueError("换算设置精度超出支持范围。")
    return result if result else Decimal(0)


def _counter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNTER:
        raise ValueError("Token 数据无效或超出支持范围。")
    return value


def _text(value: Decimal) -> str:
    return format(value.quantize(_DISPLAY, rounding=ROUND_HALF_UP), "f")


def _base(turn: dict, official: bool) -> dict:
    model = turn.get("model") if isinstance(turn.get("model"), str) else None
    return {
        "status": "unavailable", "credits": None, "creditsMax": None,
        "usd": None, "usdMax": None, "amount": None, "amountMax": None,
        "note": "", "model": model,
        "label": "按官方费率估算" if official else "自定义金额换算",
        "sourceUrl": SOURCE_URL if official else None,
        "rateDate": RATE_DATE if official else None,
        "breakdown": None, "estimateBasis": None,
    }


def _finish(result: dict, turn: dict, notes: list[str]) -> dict:
    result["status"] = "estimated"
    if turn.get("quality") == "partial":
        result["status"] = "partial"
        notes.append("Token 记录不完整，仅估算已记录部分。")
    if turn.get("status") == "running":
        notes.append("本题仍在运行，这是截至目前的估算。")
    notes.append("不代表订阅实际扣款或剩余额度。")
    result["note"] = " ".join(notes)
    return result


def estimate_turn(turn: dict, settings: dict) -> dict:
    """Return a stable, JSON-safe estimate; unknown inputs fail closed.

    `amount` uses the selected currency; `usd` is credits × configured USD per
    credit. Public max fields are always null. Internally ambiguous speed uses
    the midpoint of unrounded credit estimates before currency conversion.
    The default follows the user's confirmed non-Fast mode; explicit auto and
    Fast remain supported for compatibility. Reasoning effort changes no rate.
    """
    turn = turn if isinstance(turn, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    mode = settings.get("pricingMode", "official")
    result = _base(turn, mode != "custom")
    try:
        if not isinstance(mode, str) or mode not in {"official", "custom"}:
            raise ValueError("计价方式无效。")
        tokens = turn.get("tokens")
        if not isinstance(tokens, dict) or turn.get("quality") == "unavailable":
            raise ValueError("暂无可用的 Token 记录。")
        total = _counter(tokens.get("total"))
        with localcontext() as context:
            context.prec = 100
            context.Emax = 1000
            context.Emin = -1000
            if mode == "custom":
                raw_rate = settings.get("ratePerMillion")
                if raw_rate is None or raw_rate == "":
                    raise ValueError("请先设置每百万 Token 的自定义换算单价。")
                rate = _number(raw_rate)
                result["amount"] = _text(Decimal(total) * rate / _MILLION)
                result["estimateBasis"] = "custom"
                result["breakdown"] = {"totalTokens": total, "ratePerMillion": str(rate)}
                return _finish(result, turn, ["按自定义单价换算，非官方价格。"])

            metadata = turn.get("pricingMetadataStatus")
            if metadata == "mixed":
                raise ValueError("本题混用了模型或服务档位，无法用单一官方费率估算。")
            if not isinstance(metadata, str) or metadata not in {"known", "unknown"}:
                raise ValueError("缺少可验证的模型与服务档位信息。")
            model = result["model"]
            if model == "gpt-5.3-codex-spark":
                raise ValueError("Spark 暂无公开数字费率，不能据此估算金额。")
            if model not in RATES:
                raise ValueError("该模型暂无本插件可核实的官方数字费率。")
            incoming = _counter(tokens.get("input"))
            cached = _counter(tokens.get("cachedInput"))
            outgoing = _counter(tokens.get("output"))
            reasoning = _counter(tokens.get("reasoningOutput"))
            cache_write = _counter(tokens.get("cacheWriteInput"))
            if cached > incoming or reasoning > outgoing or total != incoming + outgoing:
                raise ValueError("Token 明细不一致，无法可靠估算。")
            if cache_write:
                raise ValueError("记录含缓存写入 Token，当前官方费率未明确其计价方式。")

            speed = settings.get("speedMode", "standard")
            fast = FAST_MULTIPLIERS.get(model)
            multiplier, multiplier_max = Decimal(1), None
            notes: list[str] = []
            if speed == "standard":
                result["estimateBasis"] = "standard"
                notes.append("按你确认的非 Fast 模式计算。")
            elif speed == "fast":
                if fast is None:
                    raise ValueError("该模型没有已核实的 Fast 倍率。")
                multiplier = fast
                result["estimateBasis"] = "fast"
                notes.append("按用户选择的 Fast 场景估算，不据此推断实际档位。")
            elif speed == "auto":
                # The official speed page documents `fast`. It does not map
                # transcript `default` or API `priority` to Standard/Fast.
                if metadata == "known" and turn.get("serviceTier") == "fast":
                    if fast is None:
                        raise ValueError("该模型没有已核实的 Fast 倍率。")
                    multiplier = fast
                    result["estimateBasis"] = "fast"
                    notes.append("按记录中的 Fast 档位估算。")
                elif fast is not None:
                    multiplier_max = fast
                    result["estimateBasis"] = "midpoint"
                    notes.append("按可用估算的中点计算。")
                else:
                    raise ValueError("速度档位不明且没有已核实的 Fast 倍率；可选择 Standard 场景估算。")
            else:
                raise ValueError("速度计价设置无效。")

            usd_per_credit = _number(settings.get("usdPerCredit", "0.04"))
            currency_per_usd = _number(settings.get("currencyPerUsd", "1"))
            components = {}
            standard_credits = Decimal(0)
            for field, count, raw_rate in zip(
                ("uncachedInput", "cachedInput", "output"),
                (incoming - cached, cached, outgoing), RATES[model][1:],
            ):
                rate = Decimal(raw_rate)
                credits = Decimal(count) * rate / _MILLION
                standard_credits += credits
                components[field] = {"tokens": count, "creditsPerMillion": raw_rate, "standardCredits": _text(credits)}
            credits = standard_credits * multiplier
            if multiplier_max is not None:
                credits = (credits + standard_credits * multiplier_max) / 2
                multiplier = (multiplier + multiplier_max) / 2
            usd = credits * usd_per_credit
            result.update(credits=_text(credits), usd=_text(usd), amount=_text(usd * currency_per_usd))
            result["breakdown"] = {
                **components, "multiplier": str(multiplier),
                "multiplierMax": None,
                "usdPerCredit": str(usd_per_credit), "currencyPerUsd": str(currency_per_usd),
                "speedSourceUrl": SPEED_SOURCE_URL,
            }
            notes.append("金额使用设置中的每 credit 美元价值及货币汇率换算。")
            return _finish(result, turn, notes)
    except (ValueError, DecimalException) as exc:
        # None of the untrusted numeric/metadata values are interpolated into
        # output. Preserve safe diagnostics while returning no partial totals.
        result.update(status="unavailable", credits=None, creditsMax=None, usd=None, usdMax=None, amount=None, amountMax=None, breakdown=None, estimateBasis=None)
        result["note"] = str(exc) if isinstance(exc, ValueError) else "换算数值超出可支持范围。"
        return result
