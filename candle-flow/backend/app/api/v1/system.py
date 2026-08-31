from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user, require_user
from app.config import settings
from app.database import get_db
from app.models.payment_claim import PaymentClaim
from app.models.user_config import UserConfig
from app.schemas.common import ApiResponse
from app.schemas.system import (
    AdminUserOut,
    AuthOut,
    ConfigOut,
    HealthOut,
    MembershipOfferOut,
    MembershipOut,
    WatchlistGroupOut,
    WatchlistOut,
)
from app.services.akshare_client import akshare_client
from app.services.backtest_service import run_backtest
from app.services.kline_service import KlineService
from app.services.membership import (
    VALID_PLANS,
    WATCHLIST_LIMIT,
    activate_membership,
    membership_snapshot,
    watchlist_limit,
)
from app.services.pay_claim import APPROVED, PENDING as CLAIM_PENDING, REJECTED, claim_out, claim_path
from app.services.pay_qr import pay_qr_urls
from app.services.payment import online_channels
from app.services.phone import normalize_account
from app.services.stock_universe import resolve_symbol
from app.services.watchlist import (
    add_symbol_to_groups,
    create_group,
    delete_group,
    dump_watchlist,
    dump_watchlist_groups,
    flatten_groups,
    move_symbol,
    parse_watchlist,
    parse_watchlist_groups,
    remove_symbol_from_groups,
    rename_group,
    replace_symbols,
)
from app.utils.symbol import SymbolError

router = APIRouter()


def _hash_password(username: str, password: str) -> str:
    return sha256(f"candle-flow:{username}:{password}".encode("utf-8")).hexdigest()


def _legacy_hash(password: str) -> str:
    return sha256(f"candle-flow:{password}".encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_hex(24)


def _password_ok(cfg: UserConfig, password: str) -> bool:
    if cfg.password_hash == _hash_password(cfg.user_id, password):
        return True
    if cfg.user_id == "default" and cfg.password_hash == _legacy_hash(password):
        return True
    return False


def _membership_out(user: UserConfig | None) -> MembershipOut:
    return MembershipOut(**membership_snapshot(user))


def _get_or_create_config(db: Session) -> UserConfig:
    cfg = db.query(UserConfig).filter(UserConfig.user_id == "default").first()
    if not cfg:
        cfg = UserConfig(
            user_id="default",
            risk_per_trade=Decimal(str(settings.risk_per_trade)),
            default_symbol=settings.default_symbol,
            preferred_period="daily",
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _config_out(cfg: UserConfig | None) -> ConfigOut:
    if not cfg:
        return ConfigOut(
            risk_per_trade=Decimal(str(settings.risk_per_trade)),
            default_symbol=settings.default_symbol,
            preferred_period="daily",
            default_capital=Decimal(str(settings.default_capital)),
            has_password=False,
            username=None,
            watchlist=[],
            membership=_membership_out(None),
        )
    return ConfigOut(
        risk_per_trade=cfg.risk_per_trade,
        default_symbol=cfg.default_symbol,
        preferred_period=cfg.preferred_period,
        default_capital=Decimal(str(settings.default_capital)),
        has_password=bool(cfg.password_hash),
        username=None if cfg.user_id == "default" else cfg.user_id,
        watchlist=parse_watchlist(cfg.watchlist),
        membership=_membership_out(cfg),
    )


def _watchlist_out(cfg: UserConfig) -> WatchlistOut:
    groups = parse_watchlist_groups(cfg.watchlist)
    return WatchlistOut(
        symbols=flatten_groups(groups),
        groups=[WatchlistGroupOut(id=g.id, name=g.name, symbols=list(g.symbols)) for g in groups],
        limit=watchlist_limit(cfg),
    )


def _auth_out(cfg: UserConfig) -> AuthOut:
    return AuthOut(
        username=cfg.user_id,
        token=cfg.auth_token or "",
        watchlist=parse_watchlist(cfg.watchlist),
        membership=_membership_out(cfg),
    )


def _require_admin_key(key: str | None) -> None:
    expected = (settings.membership_admin_key or "").strip()
    given = (key or "").strip()
    if len(expected) < 8 or not given or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=403, detail="管理员密钥无效")


def _fmt_dt(value: datetime | None) -> str | None:
    if not value:
        return None
    return value.isoformat(sep=" ", timespec="seconds")


class AccountBody(BaseModel):
    phone: str | None = None
    username: str | None = None
    password: str = ""


def _account_id(body: AccountBody) -> str:
    if len((body.password or "").strip()) < 4:
        raise HTTPException(status_code=400, detail="口令至少 4 位")
    try:
        return normalize_account(body.phone or body.username or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class PasswordBody(BaseModel):
    password: str = Field(min_length=4, max_length=64)


class ConfigUpdateBody(BaseModel):
    risk_per_trade: float | None = None
    default_symbol: str | None = None
    default_capital: float | None = None
    preferred_period: str | None = None


class WatchlistBody(BaseModel):
    symbols: list[str] | None = None
    add: str | None = None
    remove: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    create_group: str | None = None
    rename_group: dict[str, str] | None = None
    delete_group: str | None = None
    move: dict[str, str] | None = None


class AdminMembershipBody(BaseModel):
    admin_key: str
    username: str
    plan: str
    days: int | None = None

    @field_validator("username")
    @classmethod
    def username_ok(cls, value: str) -> str:
        try:
            return normalize_account(value)
        except ValueError as e:
            raise ValueError(str(e)) from e

    @field_validator("plan")
    @classmethod
    def plan_ok(cls, value: str) -> str:
        plan = value.strip().lower()
        if plan not in VALID_PLANS:
            raise ValueError("无效套餐")
        return plan


def _resolve_watch_symbol(raw: str, db: Session) -> str:
    try:
        return resolve_symbol(raw, db)
    except SymbolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    return ApiResponse(
        data=HealthOut(
            status="ok" if db_status == "connected" else "degraded",
            db=db_status,
            akshare="available" if akshare_client.is_available() else "mock",
        )
    )


@router.get("/membership/offer")
def membership_offer():
    wechat_qr, alipay_qr = pay_qr_urls()
    return ApiResponse(
        data=MembershipOfferOut(
            price_month=settings.membership_price_month,
            price_year=settings.membership_price_year,
            price_lifetime=settings.membership_price_lifetime,
            wechat=settings.membership_wechat,
            alipay_hint=settings.membership_alipay_hint,
            wechat_qr=wechat_qr,
            alipay_qr=alipay_qr,
            note=settings.membership_note,
            free_watchlist=WATCHLIST_LIMIT,
            member_watchlist=WATCHLIST_LIMIT,
            online_wechat=online_channels()["wechat"],
            online_alipay=online_channels()["alipay"],
        )
    )


@router.get("/admin/users")
def admin_list_users(
    q: str = Query(""),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    _require_admin_key(x_admin_key)
    query = db.query(UserConfig).filter(UserConfig.user_id != "default")
    needle = q.strip()
    if needle:
        query = query.filter(UserConfig.user_id.contains(needle))
    rows = query.order_by(UserConfig.updated_at.desc()).all()
    return ApiResponse(
        data=[
            AdminUserOut(
                username=row.user_id,
                is_active=bool(row.is_active),
                watchlist_count=len(parse_watchlist(row.watchlist)),
                membership=_membership_out(row),
                updated_at=_fmt_dt(row.updated_at),
            )
            for row in rows
        ]
    )


@router.post("/admin/membership")
def admin_set_membership(body: AdminMembershipBody, db: Session = Depends(get_db)):
    _require_admin_key(body.admin_key)
    cfg = db.query(UserConfig).filter(UserConfig.user_id == body.username).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="用户不存在，请确认手机号")
    try:
        activate_membership(cfg, body.plan, body.days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    db.commit()
    db.refresh(cfg)
    return ApiResponse(data={"username": cfg.user_id, "membership": _membership_out(cfg)})


class AdminCreateUserBody(BaseModel):
    admin_key: str
    username: str
    password: str = Field(min_length=4, max_length=64)
    plan: str = "free"
    days: int | None = None

    @field_validator("username")
    @classmethod
    def username_ok(cls, value: str) -> str:
        try:
            return normalize_account(value)
        except ValueError as e:
            raise ValueError(str(e)) from e

    @field_validator("plan")
    @classmethod
    def plan_ok(cls, value: str) -> str:
        plan = value.strip().lower()
        if plan not in VALID_PLANS:
            raise ValueError("无效套餐")
        return plan


class AdminDeleteUserBody(BaseModel):
    admin_key: str
    username: str

    @field_validator("username")
    @classmethod
    def username_ok(cls, value: str) -> str:
        try:
            return normalize_account(value)
        except ValueError as e:
            raise ValueError(str(e)) from e


@router.post("/admin/users")
def admin_create_user(body: AdminCreateUserBody, db: Session = Depends(get_db)):
    _require_admin_key(body.admin_key)
    if body.username == "default":
        raise HTTPException(status_code=400, detail="不能使用保留账号名")
    exists = db.query(UserConfig).filter(UserConfig.user_id == body.username).first()
    if exists:
        raise HTTPException(status_code=400, detail="该账号已存在")
    cfg = UserConfig(
        user_id=body.username,
        risk_per_trade=Decimal(str(settings.risk_per_trade)),
        default_symbol=settings.default_symbol,
        preferred_period="daily",
        password_hash=_hash_password(body.username, body.password),
        auth_token=_new_token(),
        watchlist=dump_watchlist([]),
        membership_plan="free",
    )
    try:
        if body.plan != "free":
            activate_membership(cfg, body.plan, body.days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return ApiResponse(
        data=AdminUserOut(
            username=cfg.user_id,
            is_active=bool(cfg.is_active),
            watchlist_count=len(parse_watchlist(cfg.watchlist)),
            membership=_membership_out(cfg),
            updated_at=_fmt_dt(cfg.updated_at),
        )
    )


@router.post("/admin/users/delete")
def admin_delete_user(body: AdminDeleteUserBody, db: Session = Depends(get_db)):
    _require_admin_key(body.admin_key)
    if body.username == "default":
        raise HTTPException(status_code=400, detail="不能删除系统默认账号")
    cfg = db.query(UserConfig).filter(UserConfig.user_id == body.username).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="用户不存在")

    from app.models.payment import PaymentOrder
    from app.models.payment_claim import PaymentClaim
    from app.services.pay_claim import delete_claim_file

    claims = db.query(PaymentClaim).filter(PaymentClaim.user_id == body.username).all()
    for row in claims:
        if row.image_file:
            delete_claim_file(row.image_file)
        db.delete(row)
    db.query(PaymentOrder).filter(PaymentOrder.user_id == body.username).delete(synchronize_session=False)
    db.delete(cfg)
    db.commit()
    return ApiResponse(data={"username": body.username, "deleted": True})


@router.get("/admin/claims")
def admin_list_claims(
    status: str = Query("pending"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    _require_admin_key(x_admin_key)
    query = db.query(PaymentClaim)
    needle = (status or "").strip().lower()
    if needle and needle != "all":
        query = query.filter(PaymentClaim.status == needle)
    rows = query.order_by(PaymentClaim.id.desc()).limit(100).all()
    return ApiResponse(data=[claim_out(row) for row in rows])


@router.get("/admin/claims/{claim_id}/image")
def admin_claim_image(
    claim_id: int,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    _require_admin_key(x_admin_key)
    row = db.query(PaymentClaim).filter(PaymentClaim.id == claim_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="凭证不存在")
    path = claim_path(row.image_file)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(path)


class ClaimReviewBody(BaseModel):
    admin_key: str
    action: str


@router.post("/admin/claims/{claim_id}/review")
def admin_review_claim(claim_id: int, body: ClaimReviewBody, db: Session = Depends(get_db)):
    _require_admin_key(body.admin_key)
    action = (body.action or "").strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="请选择开通或驳回")
    row = db.query(PaymentClaim).filter(PaymentClaim.id == claim_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if row.status != CLAIM_PENDING:
        raise HTTPException(status_code=400, detail="该凭证已处理")
    cfg = db.query(UserConfig).filter(UserConfig.user_id == row.user_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="用户不存在")
    if action == "approve":
        try:
            activate_membership(cfg, row.plan)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        row.status = APPROVED
    else:
        row.status = REJECTED
    row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(row)
    return ApiResponse(data={"claim": claim_out(row), "membership": _membership_out(cfg)})


@router.get("/config")
def get_config(user: UserConfig | None = Depends(get_optional_user)):
    return ApiResponse(data=_config_out(user))


@router.post("/config")
def update_config(body: ConfigUpdateBody, user: UserConfig = Depends(require_user), db: Session = Depends(get_db)):
    if body.risk_per_trade is not None:
        user.risk_per_trade = Decimal(str(body.risk_per_trade))
    if body.default_symbol:
        user.default_symbol = body.default_symbol.strip()
    if body.preferred_period in ("daily", "weekly"):
        user.preferred_period = body.preferred_period
    db.commit()
    db.refresh(user)
    return ApiResponse(data=_config_out(user))


@router.get("/config/watchlist")
def get_watchlist(user: UserConfig | None = Depends(get_optional_user)):
    if not user:
        return ApiResponse(
            data=WatchlistOut(
                symbols=[],
                groups=[WatchlistGroupOut(id="default", name="默认", symbols=[])],
                limit=WATCHLIST_LIMIT,
            )
        )
    return ApiResponse(data=_watchlist_out(user))


@router.post("/config/watchlist")
def update_watchlist(body: WatchlistBody, user: UserConfig = Depends(require_user), db: Session = Depends(get_db)):
    groups = parse_watchlist_groups(user.watchlist)
    limit = watchlist_limit(user)

    if body.create_group:
        try:
            groups = create_group(groups, body.create_group)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    if body.rename_group:
        gid = str(body.rename_group.get("id") or "").strip()
        name = str(body.rename_group.get("name") or "").strip()
        try:
            groups = rename_group(groups, gid, name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    if body.delete_group:
        try:
            groups = delete_group(groups, body.delete_group)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    if body.symbols is not None:
        resolved: list[str] = []
        for raw in body.symbols:
            item = _resolve_watch_symbol(raw, db)
            if item not in resolved:
                resolved.append(item)
        try:
            groups = replace_symbols(groups, resolved, max_size=limit)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    if body.add:
        try:
            groups = add_symbol_to_groups(
                groups,
                _resolve_watch_symbol(body.add, db),
                group_id=body.group_id,
                group_name=body.group_name,
                max_size=limit,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    if body.remove:
        try:
            target = _resolve_watch_symbol(body.remove, db)
        except HTTPException:
            target = body.remove
        groups = remove_symbol_from_groups(groups, target)

    if body.move:
        sym = str(body.move.get("symbol") or "").strip()
        gid = str(body.move.get("group_id") or "").strip()
        if sym and gid:
            try:
                resolved = _resolve_watch_symbol(sym, db)
            except HTTPException:
                resolved = sym
            try:
                groups = move_symbol(groups, resolved, gid)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

    user.watchlist = dump_watchlist_groups(groups)
    db.commit()
    db.refresh(user)
    return ApiResponse(data=_watchlist_out(user))


@router.post("/auth/register")
def register(body: AccountBody, db: Session = Depends(get_db)):
    account = _account_id(body)
    exists = db.query(UserConfig).filter(UserConfig.user_id == account).first()
    if exists:
        raise HTTPException(status_code=400, detail="该账号已注册")
    cfg = UserConfig(
        user_id=account,
        risk_per_trade=Decimal(str(settings.risk_per_trade)),
        default_symbol=settings.default_symbol,
        preferred_period="daily",
        password_hash=_hash_password(account, body.password),
        auth_token=_new_token(),
        watchlist=dump_watchlist([]),
        membership_plan="free",
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return ApiResponse(data=_auth_out(cfg))


@router.post("/auth/login")
def login(body: AccountBody, db: Session = Depends(get_db)):
    account = _account_id(body)
    cfg = db.query(UserConfig).filter(UserConfig.user_id == account).first()
    if not cfg or not cfg.password_hash or not _password_ok(cfg, body.password):
        raise HTTPException(status_code=401, detail="账号或口令错误")
    cfg.auth_token = _new_token()
    db.commit()
    db.refresh(cfg)
    return ApiResponse(data=_auth_out(cfg))


@router.get("/auth/me")
def auth_me(user: UserConfig = Depends(require_user)):
    return ApiResponse(data=_auth_out(user))


@router.post("/auth/setup")
def setup_password(body: PasswordBody, db: Session = Depends(get_db)):
    cfg = _get_or_create_config(db)
    if cfg.password_hash:
        raise HTTPException(status_code=400, detail="口令已设置，请先登录")
    cfg.password_hash = _legacy_hash(body.password)
    db.commit()
    return ApiResponse(data={"ok": True})


@router.get("/backtest/{symbol}")
def backtest_symbol(
    symbol: str,
    db: Session = Depends(get_db),
):
    try:
        symbol = resolve_symbol(symbol, db)
    except SymbolError as e:
        return ApiResponse(code=400101, message=str(e), data=None)
    klines, _ = KlineService(db).get_recent_klines(symbol, limit=250)
    data = run_backtest(klines)
    data["symbol"] = symbol
    return ApiResponse(data=data)
