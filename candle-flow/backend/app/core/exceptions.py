class AppException(Exception):
    def __init__(self, code: int, message: str, http_status: int = 400):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class DataSourceError(AppException):
    """AKShare 等数据源不可用或返回空数据"""

    def __init__(self, message: str = "无法获取真实行情数据，请检查网络后重试"):
        super().__init__(code=500101, message=message, http_status=502)
