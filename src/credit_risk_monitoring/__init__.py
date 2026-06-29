"""credit-risk-monitoring — branch-correctness evaluation surface (C2).

Scores how well a credit-deterioration investigation walks a chain of primary
sources (SEC EDGAR -> exhibits -> UK Companies House) against verified fixtures:
the right sources in the right dependency order, to the right depth, with a
grounded answer. Built on the shared ``agent-evals`` (Layer 1 deterministic +
Layer 2 judge) and ``llm-provider`` packages.

This package ships the eval surface and a deliberately weak single-shot baseline
(``credit_risk_monitoring.baseline``); the Arm A agent loop (C3) is out of scope
and emits the same trace format defined in ``credit_risk_monitoring.trace``.
"""

__version__ = "0.1.0"
