class LocalstackExit(Exception):
    """Raised to gracefully exit LocalStack."""

    def __init__(self, reason: str = None, code: int = 1):
        self.reason = reason
        self.code = code
        super().__init__(reason)
