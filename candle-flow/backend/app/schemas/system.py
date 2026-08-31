from decimal import Decimal

from pydantic import Field

from app.schemas.common import BaseSchema


class MembershipOut(BaseSchema):
    plan: str = "free"
    plan_label: str = "免费"
    is_member: bool = False
    expires_at: str | None = None
    watchlist_limit: int = 8


class MembershipOfferOut(BaseSchema):
    price_month: str
    price_year: str
    price_lifetime: str
    wechat: str = ""
    alipay_hint: str = ""
    wechat_qr: str = ""
    alipay_qr: str = ""
    note: str = ""
    free_watchlist: int = 8
    member_watchlist: int = 50
    online_wechat: bool = False
    online_alipay: bool = False


class ConfigOut(BaseSchema):
    risk_per_trade: Decimal
    default_symbol: str
    preferred_period: str
    default_capital: Decimal
    has_password: bool = False
    username: str | None = None
    watchlist: list[str] = Field(default_factory=list)
    membership: MembershipOut = Field(default_factory=MembershipOut)


class WatchlistGroupOut(BaseSchema):
    id: str
    name: str
    symbols: list[str] = Field(default_factory=list)


class WatchlistOut(BaseSchema):
    symbols: list[str]
    groups: list[WatchlistGroupOut] = Field(default_factory=list)
    limit: int = 8


class AuthOut(BaseSchema):
    username: str
    token: str
    watchlist: list[str] = Field(default_factory=list)
    membership: MembershipOut = Field(default_factory=MembershipOut)


class HealthOut(BaseSchema):
    status: str
    db: str
    akshare: str


class AdminUserOut(BaseSchema):
    username: str
    is_active: bool = True
    watchlist_count: int = 0
    membership: MembershipOut = Field(default_factory=MembershipOut)
    updated_at: str | None = None
