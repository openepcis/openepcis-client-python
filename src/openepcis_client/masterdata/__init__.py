# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Product and organization records: payloads, upsert, reading back, GS1 import,
bulk onboarding, GPC.

- :mod:`~openepcis_client.masterdata.payload` builds the nested catalog document
  from dotted GS1 term paths and shapes the awkward value kinds;
- :mod:`~openepcis_client.masterdata.vocabulary` is the pinned manifest of terms
  the platform speaks, including the divergent bulk CSV column set;
- :class:`Masterdata` makes the calls.
"""

from .service import (
    BulkReport,
    BulkRowError,
    GpcNode,
    Gs1Record,
    InvalidKey,
    Masterdata,
)

__all__ = ["BulkReport", "BulkRowError", "GpcNode", "Gs1Record", "InvalidKey", "Masterdata"]
