"""Pure token-to-budget estimation shared by HTTP Workers and the Control Plane."""

from decimal import ROUND_CEILING, Decimal, localcontext

from .contracts import PricingManifest


def price_call(manifest, call):
    rates = PricingManifest.model_validate(manifest)
    with localcontext() as context:
        context.prec = 80
        value = (
            Decimal(call.input_tokens - call.cached_input_tokens)
            * Decimal(rates.input_usd_per_million)
            + Decimal(call.cached_input_tokens) * Decimal(rates.cached_input_usd_per_million)
            + Decimal(call.output_tokens) * Decimal(rates.output_usd_per_million)
        )
        return int(value.to_integral_value(rounding=ROUND_CEILING))
