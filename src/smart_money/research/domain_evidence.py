"""One field-driven evidence collector; no broad prefetch or fallback source lists."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_money.infrastructure.sources.tools import SourceToolRegistry, build_default_source_tool_registry
from smart_money.research import domain_contract_activity, domain_registered_crypto, domain_rule_sources
from smart_money.research.domain_source_parsers import (
    DomainSourceParserRegistry,
    build_default_domain_source_parser_registry,
)
from smart_money.research.research_gateway import UniversalEvidenceResearchGateway


class DomainEvidenceRouter:
    """Execute only registered activities requested by the evidence contract."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        source_tool_registry: SourceToolRegistry | None = None,
        domain_source_parser_registry: DomainSourceParserRegistry | None = None,
        source_config_path: str | Path | None = None,
    ) -> None:
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.source_tool_registry = source_tool_registry or build_default_source_tool_registry(
            now=self.now,
            source_config_path=source_config_path,
        )
        self.domain_source_parser_registry = (
            domain_source_parser_registry or build_default_domain_source_parser_registry()
        )
        self.research_gateway = UniversalEvidenceResearchGateway(
            self.source_tool_registry,
            self.domain_source_parser_registry,
            now=self.now,
        )
        self.runtime: dict[str, Any] = {}

    execute_contract_activity = domain_contract_activity.execute_contract_activity
    _registered_crypto_price_rows = domain_registered_crypto._registered_crypto_price_rows
    _crypto_spot_artifacts = domain_registered_crypto._crypto_spot_artifacts
    _crypto_candle_path = domain_registered_crypto._crypto_candle_path
    _crypto_artifact_row = domain_registered_crypto._crypto_artifact_row
    _parsed_field_names = staticmethod(domain_rule_sources._parsed_field_names)
    _contract_activity_result = staticmethod(domain_rule_sources._contract_activity_result)
