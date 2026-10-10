"""Frozen offline observations and simulation outputs; no real execution claim."""

from typing import Literal

from pydantic import Field

from agent_platform.domain.account import Balance
from agent_platform.domain.common import RunMode
from agent_platform.domain.decisions import AdvisoryAssessment, DecisionSnapshot
from agent_platform.domain.models import DomainModel, Identifier
from agent_platform.domain.paper import PaperFill


class ReplayDecision(DomainModel):
    event_id: Identifier
    snapshot: DecisionSnapshot
    assessment: AdvisoryAssessment


class ReplayReport(DomainModel):
    run_id: Identifier
    mode: RunMode
    rule_version: Identifier
    events_processed: int = Field(strict=True, ge=0)
    duplicate_count: int = Field(strict=True, ge=0)
    decisions: tuple[ReplayDecision, ...]
    paper_fills: tuple[PaperFill, ...] = ()
    paper_initial_balances: tuple[Balance, ...] = ()
    paper_final_balances: tuple[Balance, ...] = ()
    jev_status: Literal["unspecified"] = "unspecified"
    network_calls: Literal[0] = 0
    real_orders: Literal[0] = 0
