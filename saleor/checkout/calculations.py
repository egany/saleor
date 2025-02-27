from decimal import Decimal
from typing import TYPE_CHECKING, Iterable, Optional, Tuple

from django.conf import settings
from django.utils import timezone
from prices import Money, TaxedMoney

from ..checkout import base_calculations
from ..core.prices import quantize_price
from ..core.taxes import TaxData, zero_taxed_money
from ..discount import DiscountInfo
from ..tax import TaxCalculationStrategy
from ..tax.calculations.checkout import update_checkout_prices_with_flat_rates
from ..tax.utils import (
    calculate_tax_rate,
    get_charge_taxes_for_checkout,
    get_tax_calculation_strategy_for_checkout,
    normalize_tax_rate_for_db,
)
from .models import Checkout

if TYPE_CHECKING:
    from ..account.models import Address
    from ..plugins.manager import PluginsManager
    from .fetch import CheckoutInfo, CheckoutLineInfo


def checkout_shipping_price(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    address: Optional["Address"],
    discounts: Optional[Iterable[DiscountInfo]] = None,
) -> "TaxedMoney":
    """Return checkout shipping price.

    It takes in account all plugins.
    """
    currency = checkout_info.checkout.currency
    checkout_info, _ = fetch_checkout_prices_if_expired(
        checkout_info,
        manager=manager,
        lines=lines,
        address=address,
        discounts=discounts,
    )
    return quantize_price(checkout_info.checkout.shipping_price, currency)


def checkout_shipping_tax_rate(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    address: Optional["Address"],
    discounts: Optional[Iterable[DiscountInfo]] = None,
) -> Decimal:
    """Return checkout shipping tax rate.

    It takes in account all plugins.
    """
    checkout_info, _ = fetch_checkout_prices_if_expired(
        checkout_info,
        manager=manager,
        lines=lines,
        address=address,
        discounts=discounts,
    )
    return checkout_info.checkout.shipping_tax_rate


def checkout_subtotal(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    address: Optional["Address"],
    discounts: Optional[Iterable[DiscountInfo]] = None,
) -> "TaxedMoney":
    """Return the total cost of all the checkout lines, taxes included.

    It takes in account all plugins.
    """
    currency = checkout_info.checkout.currency
    checkout_info, _ = fetch_checkout_prices_if_expired(
        checkout_info,
        manager=manager,
        lines=lines,
        address=address,
        discounts=discounts,
    )
    return quantize_price(checkout_info.checkout.subtotal, currency)


def calculate_checkout_total_with_gift_cards(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    address: Optional["Address"],
    discounts: Optional[Iterable[DiscountInfo]] = None,
) -> "TaxedMoney":
    total = (
        checkout_total(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            address=address,
            discounts=discounts,
        )
        - checkout_info.checkout.get_total_gift_cards_balance()
    )

    return max(total, zero_taxed_money(total.currency))


def checkout_total(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    address: Optional["Address"],
    discounts: Optional[Iterable[DiscountInfo]] = None,
) -> "TaxedMoney":
    """Return the total cost of the checkout.

    Total is a cost of all lines and shipping fees, minus checkout discounts,
    taxes included.

    It takes in account all plugins.
    """
    currency = checkout_info.checkout.currency
    checkout_info, _ = fetch_checkout_prices_if_expired(
        checkout_info,
        manager=manager,
        lines=lines,
        address=address,
        discounts=discounts,
    )
    return quantize_price(checkout_info.checkout.total, currency)


def _find_checkout_line_info(
    lines: Iterable["CheckoutLineInfo"],
    checkout_line_info: "CheckoutLineInfo",
) -> "CheckoutLineInfo":
    """Return checkout line info from lines parameter.

    The return value represents the updated version of checkout_line_info parameter.
    """
    return next(
        line_info
        for line_info in lines
        if line_info.line.pk == checkout_line_info.line.pk
    )


def checkout_line_total(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    checkout_line_info: "CheckoutLineInfo",
    discounts: Iterable[DiscountInfo] = [],
) -> TaxedMoney:
    """Return the total price of provided line, taxes included.

    It takes in account all plugins.
    """
    currency = checkout_info.checkout.currency
    address = checkout_info.shipping_address or checkout_info.billing_address
    _, lines = fetch_checkout_prices_if_expired(
        checkout_info,
        manager=manager,
        lines=lines,
        address=address,
        discounts=discounts,
    )
    checkout_line = _find_checkout_line_info(lines, checkout_line_info).line
    return quantize_price(checkout_line.total_price, currency)


def checkout_line_unit_price(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    checkout_line_info: "CheckoutLineInfo",
    discounts: Iterable[DiscountInfo],
) -> TaxedMoney:
    """Return the unit price of provided line, taxes included.

    It takes in account all plugins.
    """
    currency = checkout_info.checkout.currency
    address = checkout_info.shipping_address or checkout_info.billing_address
    _, lines = fetch_checkout_prices_if_expired(
        checkout_info,
        manager=manager,
        lines=lines,
        address=address,
        discounts=discounts,
    )
    checkout_line = _find_checkout_line_info(lines, checkout_line_info).line
    unit_price = checkout_line.total_price / checkout_line.quantity
    return quantize_price(unit_price, currency)


def checkout_line_tax_rate(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    checkout_line_info: "CheckoutLineInfo",
    discounts: Iterable[DiscountInfo],
) -> Decimal:
    """Return the tax rate of provided line.

    It takes in account all plugins.
    """
    address = checkout_info.shipping_address or checkout_info.billing_address
    _, lines = fetch_checkout_prices_if_expired(
        checkout_info,
        manager=manager,
        lines=lines,
        address=address,
        discounts=discounts,
    )
    checkout_line_info = _find_checkout_line_info(lines, checkout_line_info)
    return checkout_line_info.line.tax_rate


def fetch_checkout_prices_if_expired(
    checkout_info: "CheckoutInfo",
    manager: "PluginsManager",
    lines: Iterable["CheckoutLineInfo"],
    address: Optional["Address"] = None,
    discounts: Optional[Iterable["DiscountInfo"]] = None,
    force_update: bool = False,
) -> Tuple["CheckoutInfo", Iterable["CheckoutLineInfo"]]:
    """Fetch checkout prices with taxes.

    First calculate and apply all checkout prices with taxes separately,
    then apply tax data as well if we receive one.

    Prices can be updated only if force_update == True, or if time elapsed from the
    last price update is greater than settings.CHECKOUT_PRICES_TTL.
    """

    checkout = checkout_info.checkout

    if not force_update and checkout.price_expiration > timezone.now():
        return checkout_info, lines

    tax_configuration = checkout_info.tax_configuration
    tax_calculation_strategy = get_tax_calculation_strategy_for_checkout(
        checkout_info, lines
    )
    prices_entered_with_tax = tax_configuration.prices_entered_with_tax
    charge_taxes = get_charge_taxes_for_checkout(checkout_info, lines)
    should_charge_tax = charge_taxes and not checkout.tax_exemption

    if prices_entered_with_tax:
        # If prices are entered with tax, we need to always calculate it anyway, to
        # display the tax rate to the user.
        _calculate_and_add_tax(
            tax_calculation_strategy,
            checkout,
            manager,
            checkout_info,
            lines,
            prices_entered_with_tax,
            address,
            discounts,
        )

        if not should_charge_tax:
            # If charge_taxes is disabled or checkout is exempt from taxes, remove the
            # tax from the original gross prices.
            _remove_tax(checkout, lines)

    else:
        # Prices are entered without taxes.
        if should_charge_tax:
            # Calculate taxes if charge_taxes is enabled and checkout is not exempt
            # from taxes.
            _calculate_and_add_tax(
                tax_calculation_strategy,
                checkout,
                manager,
                checkout_info,
                lines,
                prices_entered_with_tax,
                address,
                discounts,
            )
        else:
            # Calculate net prices without taxes.
            _get_checkout_base_prices(checkout, checkout_info, lines, discounts)

    checkout.price_expiration = (
        timezone.now() + settings.CHECKOUT_PRICES_TTL  # type: ignore
    )
    checkout.save(
        update_fields=[
            "voucher_code",
            "total_net_amount",
            "total_gross_amount",
            "subtotal_net_amount",
            "subtotal_gross_amount",
            "shipping_price_net_amount",
            "shipping_price_gross_amount",
            "shipping_tax_rate",
            "price_expiration",
            "translated_discount_name",
            "discount_amount",
            "discount_name",
            "currency",
        ],
        using=settings.DATABASE_CONNECTION_DEFAULT_NAME,
    )
    checkout.lines.bulk_update(
        [line_info.line for line_info in lines],
        [
            "total_price_net_amount",
            "total_price_gross_amount",
            "tax_rate",
        ],
    )
    return checkout_info, lines


def _calculate_and_add_tax(
    tax_calculation_strategy: str,
    checkout: "Checkout",
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    prices_entered_with_tax: bool,
    address: Optional["Address"] = None,
    discounts: Optional[Iterable["DiscountInfo"]] = None,
):
    if tax_calculation_strategy == TaxCalculationStrategy.TAX_APP:
        # Call the tax plugins.
        _apply_tax_data_from_plugins(
            checkout, manager, checkout_info, lines, address, discounts
        )
        # Get the taxes calculated with apps and apply to checkout.
        tax_data = manager.get_taxes_for_checkout(checkout_info, lines)
        _apply_tax_data(checkout, lines, tax_data)
    elif tax_calculation_strategy == TaxCalculationStrategy.FLAT_RATES:
        # Get taxes calculated with flat rates and apply to checkout.
        update_checkout_prices_with_flat_rates(
            checkout, checkout_info, lines, prices_entered_with_tax, address, discounts
        )


def _remove_tax(checkout, lines_info):
    checkout.total_gross_amount = checkout.total_net_amount
    checkout.subtotal_gross_amount = checkout.subtotal_net_amount
    checkout.shipping_price_gross_amount = checkout.shipping_price_net_amount
    checkout.shipping_tax_rate = Decimal("0.00")

    for line_info in lines_info:
        total_price_net_amount = line_info.line.total_price_net_amount
        line_info.line.total_price_gross_amount = total_price_net_amount
        line_info.line.tax_rate = Decimal("0.00")


def _calculate_checkout_total(checkout, currency):
    total = checkout.subtotal + checkout.shipping_price
    return quantize_price(
        total,
        currency,
    )


def _calculate_checkout_subtotal(lines, currency):
    line_totals = [line_info.line.total_price for line_info in lines]
    total = sum(line_totals, zero_taxed_money(currency))
    return quantize_price(
        total,
        currency,
    )


def _apply_tax_data(
    checkout: "Checkout",
    lines: Iterable["CheckoutLineInfo"],
    tax_data: Optional[TaxData],
) -> None:
    if not tax_data:
        return

    currency = checkout.currency
    for (line_info, tax_line_data) in zip(lines, tax_data.lines):
        line = line_info.line

        line.total_price = quantize_price(
            TaxedMoney(
                net=Money(tax_line_data.total_net_amount, currency),
                gross=Money(tax_line_data.total_gross_amount, currency),
            ),
            currency,
        )
        line.tax_rate = normalize_tax_rate_for_db(tax_line_data.tax_rate)

    checkout.shipping_tax_rate = normalize_tax_rate_for_db(tax_data.shipping_tax_rate)
    checkout.shipping_price = quantize_price(
        TaxedMoney(
            net=Money(tax_data.shipping_price_net_amount, currency),
            gross=Money(tax_data.shipping_price_gross_amount, currency),
        ),
        currency,
    )
    checkout.subtotal = _calculate_checkout_subtotal(lines, currency)
    checkout.total = _calculate_checkout_total(checkout, currency)


def _apply_tax_data_from_plugins(
    checkout: "Checkout",
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    address: Optional["Address"],
    discounts: Optional[Iterable[DiscountInfo]] = None,
) -> None:
    if not discounts:
        discounts = []

    for line_info in lines:
        line = line_info.line

        total_price = manager.calculate_checkout_line_total(
            checkout_info,
            lines,
            line_info,
            address,
            discounts,
        )
        line.total_price = total_price

        unit_price = manager.calculate_checkout_line_unit_price(
            checkout_info,
            lines,
            line_info,
            address,
            discounts,
        )

        line.tax_rate = manager.get_checkout_line_tax_rate(
            checkout_info,
            lines,
            line_info,
            address,
            discounts,
            unit_price,
        )

    checkout.shipping_price = manager.calculate_checkout_shipping(
        checkout_info, lines, address, discounts
    )
    checkout.shipping_tax_rate = manager.get_checkout_shipping_tax_rate(
        checkout_info, lines, address, discounts, checkout.shipping_price
    )
    checkout.subtotal = manager.calculate_checkout_subtotal(
        checkout_info, lines, address, discounts
    )
    checkout.total = manager.calculate_checkout_total(
        checkout_info, lines, address, discounts
    )


def _get_checkout_base_prices(
    checkout: "Checkout",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    discounts: Optional[Iterable[DiscountInfo]] = None,
) -> None:
    if not discounts:
        discounts = []

    currency = checkout_info.checkout.currency

    for line_info in lines:
        line = line_info.line

        total_price_default = base_calculations.calculate_base_line_total_price(
            line_info,
            checkout_info.channel,
            discounts,
        )
        line.total_price = quantize_price(
            TaxedMoney(net=total_price_default, gross=total_price_default), currency
        )

        unit_price_default = base_calculations.calculate_base_line_unit_price(
            line_info, checkout_info.channel, discounts
        )
        unit_price = quantize_price(
            TaxedMoney(net=unit_price_default, gross=unit_price_default), currency
        )
        line.tax_rate = calculate_tax_rate(unit_price)

    shipping_price_default = base_calculations.base_checkout_delivery_price(
        checkout_info, lines
    )
    checkout.shipping_price = quantize_price(
        TaxedMoney(shipping_price_default, shipping_price_default), currency
    )
    checkout.shipping_tax_rate = calculate_tax_rate(checkout.shipping_price)

    subtotal_default = sum(
        [line_info.line.total_price for line_info in lines], zero_taxed_money(currency)
    )
    checkout.subtotal = subtotal_default

    total_default = base_calculations.base_checkout_total(
        checkout_info, discounts, lines
    )
    checkout.total = quantize_price(
        TaxedMoney(net=total_default, gross=total_default), currency
    )
