import fnmatch
from collections.abc import Iterable, Iterator
from typing import Any, TypedDict

from localstack_cli.utils.numbers import to_number


class ProductInfo(TypedDict):
    """
    A description of the product being licensed. Does not necessarily map to the stripe products.
    """

    name: str
    version: str
    metadata: dict[str, Any] | None


class ProductEntitlements:
    """
    Helper for feature gating with the product entitlement information embedded in a license document.

    Currently, two types of features/entitlements are supported:
    * Boolean features (`has_entitlement`)
    * Numerical limits (`get_entitlement_limit`)

    To be extended in the future to support other types of features/entitlements.
    """

    def __init__(
        self,
        products: Iterable[ProductInfo],
        allow_all: bool = False,
    ):
        self._products = list(products)
        self._allow_all = allow_all

    def __contains__(self, entitlement: str) -> bool:
        return self.has_entitlement(entitlement)

    def __iter__(self) -> Iterator[ProductInfo]:
        return iter(self._products)

    def has_entitlement(self, entitlement: str) -> bool:
        """
        Check if an entitlement is part of the currently active license.

        The comparison uses fnmatch to compare, so the license could contain a ProductInfo record
        ``localstack.extensions/*`` which would give access to all restricted extensions.

        :param entitlement: The entitlement to check, for example ``localstack.extensions/foo``.
        :return: True if the entitlement is part of the active license.
        """
        if self._allow_all:
            return True

        return self._get_product_info(entitlement, pattern_matching=True) is not None

    def get_entitlement_limit(
        self,
        entitlement: str,
        default: int | float | None = None,
    ) -> int | float | None:
        """
        Returns the numerical limit configured for the given feature.

        :param entitlement: The entitlement to check, for example ``localstack.extensions/foo``.
        :param default: The default value to return if the entitlement is not part of the active license or the limit
                        cannot be determined.
        :return: The limit configured for the given entitlement, or the default value if the entitlement is not part of
                 the active license or the limit cannot be determined.
        """
        product = self._get_product_info(entitlement)
        if not product:
            return default

        metadata = self._get_metadata(product)
        if metadata is None:
            return default

        raw_limit = metadata.get("limit")
        if raw_limit is None:
            return default

        try:
            limit = to_number(raw_limit)
        except Exception:
            return default

        if limit is None or isinstance(limit, bool):
            return default

        return limit

    def _get_product_info(
        self, entitlement: str, pattern_matching: bool = False
    ) -> ProductInfo | None:
        for product in self._products:
            pattern = product.get("name")
            if not pattern:
                continue

            pattern = str(pattern)

            if "*" in pattern and pattern_matching:
                if fnmatch.fnmatch(entitlement, pattern):
                    return product
            elif pattern == entitlement:
                return product

        return None

    def _get_metadata(self, product: ProductInfo) -> dict[str, Any] | None:
        metadata = product.get("metadata")
        if metadata is None or not isinstance(metadata, dict):
            return None

        return metadata
