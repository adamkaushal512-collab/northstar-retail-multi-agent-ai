"""Integrations: read-only data access layer.

Placeholder only. Will be the only path to OMS, Payment, Inventory and
Shipping/Carrier APIs for current status [Decision D4], and to Snowflake for
historical context only [Decision D5]. Read-only [Decision D11]. An
unavailable or stale source means unknown or unverified status, with no
Snowflake or cache substitution [Decision D15]. No credentials in code.
"""
