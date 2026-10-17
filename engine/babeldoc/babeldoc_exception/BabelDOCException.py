class ScannedPDFError(Exception):
    def __init__(self, message):
        super().__init__(message)


class ExtractTextError(Exception):
    def __init__(self, message):
        super().__init__(message)


class InputFileGeneratedByBabelDOCError(Exception):
    def __init__(self, message):
        super().__init__(message)


class ContentFilterError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class TranslationRequestError(Exception):
    """A translation request failed for good: the translator's retries are used up.

    The paragraph translators do not fall back to simpler translation or keep
    the source text for it. They record the error with its page, stop sending
    new requests and fail the translation stage with
    ``TranslationRequestsFailedError``.
    """

    def __init__(self, message: str, page_number: int | None = None):
        super().__init__(message)
        self.message = message
        self.page_number = page_number  # 1-based; None if unknown


class TranslationRequestsFailedError(Exception):
    """The translation stage failed because translation requests failed for good."""

    def __init__(self, failures: list[TranslationRequestError]):
        first = failures[0]
        super().__init__(f"translation request failed on page {first.page_number}: {first.message}")
        self.failures = failures
