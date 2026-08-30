from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/candle_flow.db"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    default_symbol: str = "000001.SZ"
    risk_per_trade: float = 1.0
    default_capital: float = 100000.0
    # Manual membership: set a long random key; used only by you to activate users.
    membership_admin_key: str = ""
    membership_wechat: str = ""
    membership_alipay_hint: str = ""
    membership_price_month: str = "39"
    membership_price_year: str = "299"
    membership_price_lifetime: str = "799"
    membership_note: str = "扫码付款后，在本页上传截图即可，不用加微信。管理员看到后通常当天开通。"
    public_base_url: str = "https://candle-flow.online"
    xunhupay_gateway: str = "https://api.xunhupay.com/payment/do.html"
    xunhupay_wechat_appid: str = ""
    xunhupay_wechat_secret: str = ""
    xunhupay_alipay_appid: str = ""
    xunhupay_alipay_secret: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
