"""Pydantic schemas shared by agents, rules, the orchestrator, and the CLI.

Schemas an LLM fills (Invoice, ItemMatching, ApprovalDraft, Critique) stay plain:
floats, strings, and enums only. Money is compared in integer cents in code.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RiskSignal(BaseModel):
    description: str = Field(description="What looks suspicious and why, in one sentence")
    quote: str = Field(description="The exact text from the document that raised the concern")


class LineItem(BaseModel):
    description: str = Field(
        description="Item name exactly as written, including any parenthetical such as '(rush order)'"
    )
    quantity: float | None = Field(description="Quantity as written (may be negative); null if absent")
    unit_price: float | None = Field(description="Unit price as written; null if absent")
    stated_amount: float | None = Field(
        default=None, description="Line total as written on the document; null if not written"
    )
    note: str | None = Field(default=None, description="Per-line note as written, e.g. 'Volume discount'")


class Invoice(BaseModel):
    """What the document states. Nothing here is computed."""

    invoice_number: str | None = Field(description="Invoice number as written, e.g. 'INV 1012' or '1002'")
    vendor: str | None = Field(
        description="Issuing vendor's name as written, including a parenthetical that is part of it. "
        "Not the bill-to party (Acme). Null if blank."
    )
    invoice_date: str | None = Field(description="Invoice date copied exactly as written")
    due_date: str | None = Field(description="Due date copied exactly as written, even if it is not a date")
    currency: str | None = Field(
        description="ISO currency code if the document states one (e.g. 'EUR'); 'USD' if amounts use '$'; "
        "null if there is no indication"
    )
    line_items: list[LineItem] = Field(description="One entry per invoice line, in document order. Never merge lines.")
    stated_subtotal: float | None = Field(description="Subtotal as written; null if not written")
    stated_tax_rate: float | None = Field(
        description="Tax rate as a fraction (8% -> 0.08), only if a rate is written; null otherwise"
    )
    stated_tax_amount: float | None = Field(description="Tax amount as written; null if not written")
    stated_shipping: float | None = Field(description="Shipping charge as written; null if not written")
    stated_total: float | None = Field(
        description="Total amount due as written (may be labeled Total, Grand Total, Amt); null if not written"
    )
    payment_terms: str | None = Field(default=None, description="Payment terms as written")
    revision: str | None = Field(default=None, description="Revision marker if the document has one, e.g. 'R1'")
    risk_signals: list[RiskSignal] = Field(
        default_factory=list,
        description="Signs of a potentially fraudulent or suspicious invoice. Empty if nothing is suspicious.",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class LineMatch(BaseModel):
    line: int = Field(description="1-based line number")
    sku: str | None = Field(description="Catalog item this line names, or null if unknown or unsure")
    note: str = Field(description="One short sentence: what you checked and found")


class ItemMatching(BaseModel):
    """Validation agent output."""

    lines: list[LineMatch]
    summary: str = Field(description="Two sentences at most on catalog matches and stock")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class FindingCode(StrEnum):
    MISSING_FIELD = "missing_field"
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_PRICE = "invalid_price"
    UNKNOWN_ITEM = "unknown_item"
    OUT_OF_STOCK = "out_of_stock"
    INSUFFICIENT_STOCK = "insufficient_stock"
    ARITHMETIC = "arithmetic_mismatch"
    PRICE_ABOVE_CATALOG = "price_above_catalog"
    NON_USD_CURRENCY = "non_usd_currency"
    RISK_SIGNAL = "risk_signal"
    REVISED_INVOICE = "revised_invoice"
    VENDOR_CONFLICT = "vendor_conflict"


class Finding(BaseModel):
    code: FindingCode
    severity: Severity
    message: str
    line: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class LineCheck(BaseModel):
    line: int
    description: str
    sku: str | None
    note: str | None = None  # stripped parenthetical, e.g. "rush order"


class ComputedTotals(BaseModel):
    line_amounts_cents: list[int | None]
    subtotal_cents: int | None
    tax_cents: int | None
    total_cents: int | None
    payable_cents: int | None  # stated total, else computed total


class ValidationReport(BaseModel):
    lines: list[LineCheck]
    findings: list[Finding]
    computed: ComputedTotals
    currency: str
    agent_summary: str = ""
    overrides: list[str] = Field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]


class InvoiceIdentity(BaseModel):
    number: str  # normalized, e.g. INV-1012
    vendor: str  # normalized, e.g. quickship distributers
    key: str  # number/vendor
    fingerprint: str


class DuplicateVerdict(BaseModel):
    kind: Literal["new", "identical", "revised", "vendor_conflict", "no_number"]
    earlier_run_id: int | None = None
    earlier_status: str | None = None
    message: str = ""
    findings: list[Finding] = Field(default_factory=list)


class Outcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


class PolicyMode(StrEnum):
    REJECT = "reject"
    HUMAN_REVIEW = "human_review"
    STRICT = "strict"  # VP + Critic decide; human review if they don't agree
    STANDARD = "standard"


class PolicyResult(BaseModel):
    mode: PolicyMode
    reasons: list[str]
    checklist: list[str] = Field(default_factory=list)

    @property
    def fixed_outcome(self) -> Outcome | None:
        return {PolicyMode.REJECT: Outcome.REJECT, PolicyMode.HUMAN_REVIEW: Outcome.NEEDS_REVIEW}.get(self.mode)


class ApprovalDraft(BaseModel):
    """VP output."""

    decision: Outcome
    rationale: str = Field(description="Why, citing specific findings and amounts. At most 120 words.")
    checklist_responses: list[str] = Field(
        default_factory=list, description="One entry per checklist item: how you addressed it"
    )

    @field_validator("rationale")
    @classmethod
    def _unquote(cls, value: str) -> str:
        return value.strip().strip('"').strip()


class Critique(BaseModel):
    """Critic output."""

    agrees: bool = Field(description="True only if the draft has no material issue")
    issues: list[str] = Field(default_factory=list, description="Concrete problems with the draft; empty if none")
    summary: str = Field(description="One sentence verdict")


class HumanReview(BaseModel):
    outcome: Outcome
    comment: str
    reviewer: str
    reviewed_at: str


class Decision(BaseModel):
    outcome: Outcome
    decided_by: Literal["policy", "vp_critic", "human"]
    policy_mode: PolicyMode
    rationale: str
    reasons: list[str] = Field(default_factory=list)
    rounds: int = 0
    critic_issues: list[str] = Field(default_factory=list)
    review: HumanReview | None = None


class PaymentResult(BaseModel):
    status: Literal["paid", "already_paid", "nothing_due"]
    amount_cents: int
    currency: str
    vendor: str
    approved_total_cents: int
    previously_paid_cents: int
    idempotency_key: str
    payment_id: int | None = None


class RunStatus(StrEnum):
    PROCESSING = "processing"
    PAID = "paid"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class Usage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0

    def add(self, other: "Usage") -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.tool_calls += other.tool_calls
