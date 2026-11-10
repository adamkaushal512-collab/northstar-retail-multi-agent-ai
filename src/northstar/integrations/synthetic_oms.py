"""Synthetic, in-memory Order Management System (OMS) records (Step 2.6B).

[SYNTHETIC] Every record is invented for a fictional company. No external
API calls, no network, no credentials. Read-only [Decision D11]: there is no
create, update or delete path.

Reads are scoped to the caller's tenant. An order in another tenant is
indistinguishable from an unknown order, so lookups do not reveal whether
another tenant's order exists (FR-AUTH-03).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class OmsOrderRecord:
    order_id: str
    tenant_id: str
    customer_id: str  # internal only; never returned by the tool
    order_status: str
    promised_delivery_date: str  # ISO 8601 date
    carrier: str
    # Step 2.6C: internal shipment link used to resolve carrier tracking.
    # Not returned by ``get_order_details``. None means no tracking number.
    tracking_number: str | None = None


_RECORDS = (
    OmsOrderRecord("SYN-ORD-0001", "SYN-TENANT-A", "SYN-CUST-0001", "SHIPPED", "2026-11-05", "SYN-CARRIER-1", "SYN-TRK-0001"),
    OmsOrderRecord("SYN-ORD-0002", "SYN-TENANT-A", "SYN-CUST-0002", "PROCESSING", "2026-11-12", "SYN-CARRIER-2"),
    OmsOrderRecord("SYN-ORD-0003", "SYN-TENANT-B", "SYN-CUST-0101", "SHIPPED", "2026-11-07", "SYN-CARRIER-1", "SYN-TRK-0003"),
    # Step 2.6C carrier scenarios (see synthetic_carrier.py).
    OmsOrderRecord("SYN-ORD-0004", "SYN-TENANT-A", "SYN-CUST-0004", "SHIPPED", "2026-11-11", "SYN-CARRIER-2", "SYN-TRK-0004"),
    OmsOrderRecord("SYN-ORD-0005", "SYN-TENANT-A", "SYN-CUST-0005", "SHIPPED", "2026-11-06", "SYN-CARRIER-1", "SYN-TRK-0005"),
    OmsOrderRecord("SYN-ORD-0006", "SYN-TENANT-A", "SYN-CUST-0006", "SHIPPED", "2026-11-08", "SYN-CARRIER-2", "SYN-TRK-0006"),
    OmsOrderRecord("SYN-ORD-0007", "SYN-TENANT-A", "SYN-CUST-0007", "SHIPPED", "2026-11-09", "SYN-CARRIER-1", "SYN-TRK-0007"),
)

SYNTHETIC_ORDERS: Mapping[str, OmsOrderRecord] = MappingProxyType({r.order_id: r for r in _RECORDS})


def find_order(order_id: str, tenant_id: str) -> OmsOrderRecord | None:
    """Return the order only if it exists in ``tenant_id``; otherwise None."""
    record = SYNTHETIC_ORDERS.get(order_id)
    if record is None or record.tenant_id != tenant_id:
        return None
    return record
