"""Provider-independent caption failures with safe public error codes."""

class CaptionError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
