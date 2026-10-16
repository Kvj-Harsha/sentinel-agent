"""Order pricing for the checkout service (release v2.14.0).

v2.14.0 replaced the old flat tax calculation with a per-country table. The table was never given a default
rate, so a country that is not listed has no tax rate at all.
"""

TAX_RATES = {"US": 0.07, "GB": 0.20, "DE": 0.19, "FR": 0.20}


def tax_rate(country):
    """Return the tax rate for an ISO country code."""
    return TAX_RATES.get(country)


def order_total(subtotal, country):
    """Return the order total including tax, rounded to cents."""
    rate = tax_rate(country)
    return round(subtotal * (1 + rate), 2)
