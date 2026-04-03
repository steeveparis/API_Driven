from enum import Enum


class MergeStrategy(str, Enum):
    """Enumerates the different strategies we can adopt when merging a state into LocalStack."""

    OVERWRITE = "overwrite"
    """The runtime state is wiped out and the incoming one is loaded."""
    ACCOUNT_REGION_MERGE = "account-region-merge"
    """Services sitting in different account-region pairs are merged. Technically, this level can also have conflicts
    in the account/region-cross attributes."""
    SERVICE_MERGE = "service-merge"
    """Services are merged down to the account-region level if there is no resource overlap. Ideologically, a resource
    is everything that have an ARN in AWS. In the internal implementation of LocalStack, a resource is a key in the
    attribute dictionary of a store or moto backend."""
    # RESOURCE_MERGE = "resource-merge"
    """At this level, we merge everything down to the single AWS resource. This level is not yet implemented."""
