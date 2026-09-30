"""One place for the condition identifiers that non-pinned code accepts.

The scoring tables themselves stay in the hash-pinned modules
(``config/borders.py``, ``config/calculation_config.py``, ``config/score_weight.py``,
``NullPolicy.create`` and ``services/guide_prompts.py``). Those modules keep their
own literals and are not edited here; ``tests/test_guideline_registry.py`` checks
that every pinned table agrees with this registry.

- ``SUPPORTED_GUIDELINES``: guidelines with a reference calculation table.
- ``ARC_GUIDELINES``: the ARC family (minimum-count policy, ARC submission scope).
- ``GUIDELINE_BASIS``: ARC2025 uses the ARC2020 calculation settings and coaching.
- ``TARGETS`` / ``TRAINING_TYPES``: the condition.target and
  condition.training_type values the input validator and log sanitizers accept.
  ``TARGET_ORDER`` / ``TRAINING_TYPE_ORDER`` are the same values in the order
  the reference CLI lists them (argparse choices keep that order).

Consumers keep the container type they always had (``set``, ``frozenset`` or
``list``); convert at the import site instead of changing these values.
``scripts/verify_reference_parity.py`` keeps its own literal tuples because its
workers also run inside the reference checkout, where this module does not exist.
"""

from types import MappingProxyType


SUPPORTED_GUIDELINES = frozenset({"AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015"})
ARC_GUIDELINES = frozenset({"ARC2020", "ARC2025"})
GUIDELINE_BASIS = MappingProxyType({"ARC2025": "ARC2020"})

TARGET_ORDER = ("adult", "child", "infant")
TRAINING_TYPE_ORDER = ("cpr", "compression_only", "ventilation_only")
TARGETS = frozenset(TARGET_ORDER)
TRAINING_TYPES = frozenset(TRAINING_TYPE_ORDER)
