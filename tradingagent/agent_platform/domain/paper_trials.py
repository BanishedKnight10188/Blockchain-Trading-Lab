"""An immutable first-trial budget, shared for one Shanghai budget day."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import Field, StrictBool, model_serializer, model_validator

from .models import DomainModel, Identifier, Revision, UtcDateTime
from .paper_trading import PaperAmount
from .routing import ModelPrice


class PaperTrialPolicy(DomainModel):
    grant_id: Identifier | None = None  # Explicit append-only continuation; never a new budget.
    trial_total_usd: PaperAmount = Field(gt=0)
    single_call_usd: PaperAmount = Field(gt=0, le=Decimal("0.02"))
    issued_at: UtcDateTime
    expires_at: UtcDateTime | None = None
    price: ModelPrice
    supersedes_budget_key: Identifier | None = None
    budget_change_confirmed: StrictBool = False

    @model_validator(mode="after")
    def bounded_trial(self):
        if (self.supersedes_budget_key is not None) != self.budget_change_confirmed or (
            self.budget_change_confirmed and (self.grant_id is None or self.expires_at is not None)
        ):
            raise ValueError("a budget change needs explicit confirmation and a continuous parent")
        local = self.issued_at.astimezone(ZoneInfo("Asia/Shanghai"))
        midnight = datetime.combine(local.date() + timedelta(days=1), time(), tzinfo=local.tzinfo)
        if self.single_call_usd > self.trial_total_usd or (
            self.expires_at is not None and not self.issued_at < self.expires_at <= midnight
        ):
            raise ValueError("trial must fit its confirmed total and one Shanghai budget day")
        if (
            not self.price.available_at(self.issued_at)
            or (self.expires_at is None and self.price.valid_until is not None)
            or (
                self.expires_at is not None
                and self.price.valid_until is not None
                and self.expires_at > self.price.valid_until
            )
        ):
            raise ValueError("trial must fit verified price validity")
        if self.price.input_usd_per_million <= 0:
            raise ValueError("real trial needs a positive verified input price")
        return self

    @model_serializer(mode="wrap")
    def compatible_serialization(self, handler):
        value = handler(self)
        if not self.budget_change_confirmed:
            value.pop("supersedes_budget_key", None)
            value.pop("budget_change_confirmed", None)
        return value

    @property
    def budget_key(self):
        return self.initial_budget_key + (":" + self.grant_id if self.grant_id else "")

    @property
    def initial_budget_key(self):
        return (
            "paper-trial:" + self.issued_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        )

    def validate_active(self, now):
        if now < self.issued_at or (self.expires_at is not None and now >= self.expires_at):
            raise ValueError("paper model trial expired or has not started")


def effective_continuous_policies(policies):
    """Only explicit, non-forking authorizations supersede prior immutable caps."""
    by_key = {policy.budget_key: policy for policy in policies}
    if len(by_key) != len(policies):
        raise ValueError("duplicate budget authorization")
    replaced = set()
    for policy in policies:
        key = policy.supersedes_budget_key
        if key is None:
            continue
        parent = by_key.get(key)
        if (
            parent is None
            or key == policy.budget_key
            or key in replaced
            or parent.expires_at is not None
            or policy.issued_at < parent.issued_at
            or policy.single_call_usd > parent.single_call_usd
        ):
            raise ValueError("invalid budget authorization lineage")
        replaced.add(key)
    for policy in policies:
        visited, current = set(), policy
        while current.supersedes_budget_key is not None:
            if current.budget_key in visited:
                raise ValueError("budget authorization cycle")
            visited.add(current.budget_key)
            current = by_key[current.supersedes_budget_key]
    return tuple(p for p in policies if p.expires_at is None and p.budget_key not in replaced)


class PaperTrialState(DomainModel):
    aggregate_id: Identifier
    revision: Revision = 1
    policy: PaperTrialPolicy
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def immutable_identity(self):
        if (
            self.aggregate_id != self.policy.budget_key
            or self.revision != 1
            or self.updated_at != self.created_at
        ):
            raise ValueError("first trial policy is immutable")
        self.policy.validate_active(self.created_at)
        return self
