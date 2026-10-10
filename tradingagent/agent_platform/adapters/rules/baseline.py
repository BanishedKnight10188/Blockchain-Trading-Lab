"""Threshold rule for testing the data path, not a validated trading strategy."""

from agent_platform.domain.common import positive_amount
from agent_platform.domain.decisions import AdvisoryAssessment, DecisionSnapshot


class RuleAdvisoryProvider:
    rule_version = "m0-threshold-test-v1"

    def __init__(self, threshold: str):
        self.threshold = positive_amount(threshold)

    def evaluate(self, snapshot: DecisionSnapshot) -> AdvisoryAssessment:
        market = snapshot.market
        if market.status != "ready":
            return AdvisoryAssessment(
                action="unavailable",
                source="rule",
                explanation="M0测试规则：行情不可用",
                unavailable_reasons=("market_not_ready",),
            )
        price = market.latest_trade.price if market.latest_trade else market.book.bid
        if price > self.threshold:
            action = "buy"
        elif price < self.threshold and snapshot.position.quantity > 0:
            action = "sell"
        else:
            action = "hold"
        return AdvisoryAssessment(
            action=action,
            source="rule",
            evidence_ids=snapshot.evidence_ids,
            explanation="M0阈值测试规则；未启用真实特征、JEV或资金纪律服务",
        )
