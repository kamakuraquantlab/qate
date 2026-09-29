from qate.core.model import FatalException


class ApiException(Exception):
    pass


class MaintenanceException(FatalException):
    pass


class StopTradingException(FatalException):
    pass
