"""Typed API response contracts shared between backend routes and the TypeScript frontend."""

from typing import Any, TypedDict


class SourceRow(TypedDict, total=False):
    name: str
    checked: float | None
    status: str
    detail: str
    last_success: float | None
    stale: bool


class QuotaTrend(TypedDict, total=False):
    window_minutes: int
    available: bool
    reason: str
    start: float
    end: float
    observed_minutes: float
    start_remaining: float
    end_remaining: float
    decrease_pp: float
    per_hour: float


class QuotaRow(TypedDict, total=False):
    route: str
    bucket: str
    display_name: str
    remaining: float | None
    resets: float | None
    reset_kst: str | None
    seconds_to_reset: int | None
    checked: float | None
    source: str | None
    status: str
    stale: bool
    user_ended: bool
    retired: bool
    previous_account: bool
    omitted_latest: bool
    not_applicable: bool
    detail: str
    note: str | None
    scope: dict[str, Any]
    blocked_by: dict[str, Any] | None
    trends: list[QuotaTrend]
    decreases: list[dict[str, Any]]
    history: list[dict[str, Any]]
    history_source: str | None
    window: dict[str, Any] | None
    window_seconds: int | None
    events: dict[str, Any] | None
    account_epoch: str | None
    account_label: str | None


class LimitsResponse(TypedDict, total=False):
    limits: list[QuotaRow]
    sources: list[SourceRow]
    now: float
    history_days: int
    claude_accounts: list[dict[str, Any]]
    resources: dict[str, Any]
    quota_policies: dict[str, Any]
    stale_seconds: int
    retention_days: int
    low_percent: int
    quota_hide_days: int
    unused_percent: int
    unused_hours: int


class OverviewPeriod(TypedDict, total=False):
    start: str
    end_exclusive: str
    tokens: int
    requests: int
    cost: float | None
    cost_coverage: float | None
    prior: dict[str, Any] | None
    points: list[int | None]


class OverviewResponse(TypedDict, total=False):
    generated: float
    collected_at: float | None
    today: OverviewPeriod
    week: OverviewPeriod
    recent: dict[str, Any]
    models: list[dict[str, Any]]
    projects: list[dict[str, Any]]
    month: dict[str, Any] | None
    facts: list[dict[str, Any]]


class UsageResponse(TypedDict, total=False):
    period: str
    start: str
    end_exclusive: str
    granularity: str
    group: str
    scope: str | None
    labels: list[str]
    series: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    totals: dict[str, Any]
    lifetime: dict[str, Any]
    cost: dict[str, Any]
    prior: dict[str, Any] | None
    compare: dict[str, Any] | None
    heatmap: list[dict[str, Any]]
    efforts: list[dict[str, Any]]
    subscriptions: list[dict[str, Any]]
    reconciliation: dict[str, Any] | None
    account_gap: dict[str, Any] | None
    unavailable_routes: list[dict[str, Any]]
    sources: list[SourceRow]
    collected_at: float | None
    unpriced_models: list[str]
    project_budgets: dict[str, Any]
    project_month: dict[str, Any] | None
    residual_label: str
    claude_accounts: list[dict[str, Any]]
    insights: dict[str, Any]
    sessions: list[dict[str, Any]]
    sessions_total: int
    sessionless_requests: int
    projects: list[dict[str, Any]]
    calendar: list[dict[str, Any]]


class ReportsResponse(TypedDict):
    reports: list[dict[str, Any]]
    this_week: dict[str, Any] | None
    months: list[dict[str, Any]]


class CollectionStatusResponse(TypedDict):
    collector: dict[str, Any]
    backup: dict[str, Any]
    verify: dict[str, Any]
    telemetry: dict[str, Any]
    requested: float
    completed: float
    request_id: int
    completed_id: int


class SessionTreeNode(TypedDict):
    kind: str
    route: str
    model: str
    requests: int
    advisor_calls: int
    tokens: int
    output: int
    reasoning: int
    cache_share: float | None
    children: list['SessionTreeNode']


class SessionCacheHour(TypedDict):
    hour: int
    requests: int
    tokens: int
    uncached_input: int
    cached_input: int
    cache_creation: int
    output: int
    cache_share: float | None
    avg_context: int | None


class SessionEffortRow(TypedDict):
    route: str
    model: str
    effort: str | None
    speed: str | None
    requests: int
    tokens: int
    output: int
    reasoning: int
    cost: float | None


class SimulatorResponse(TypedDict):
    period: str
    start: str
    end_exclusive: str
    elapsed_days: float
    total_tokens: int
    priced_tokens: int
    coverage: float | None
    baseline_api_period: float | None
    simulated_api_period: float | None
    baseline_api_monthly: float | None
    simulated_api_monthly: float | None
    savings_period: float | None
    savings_monthly: float | None
    savings_pct: float | None
    subscription_monthly: float | None
    subscription_period: float | None
    baseline_vs_subscription: dict[str, Any]
    simulated_vs_subscription: dict[str, Any]
    by_route: list[dict[str, Any]]
    breakdown: list[dict[str, Any]]
    unpriced_targets: list[str]


class SystemDiagnosticsResponse(TypedDict):
    database: dict[str, Any]
    backups: dict[str, Any]
    hooks: dict[str, Any]
    refresh: dict[str, Any]
    available_targets: list[str]
