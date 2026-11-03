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

    ``category`` is the failure category the translator gives: ``permanent``
    (a credential or configuration error, which a rerun cannot fix) or
    ``retryable``; the failure summary keeps it.
    """

    def __init__(
        self,
        message: str,
        page_number: int | None = None,
        category: str = "retryable",
    ):
        super().__init__(message)
        self.message = message
        self.page_number = page_number  # 1-based; None if unknown
        self.category = category


class TranslationRequestsFailedError(Exception):
    """The translation stage failed because translation requests failed for good.

    ``category`` is ``permanent`` when any of the failures is, else
    ``retryable``; ``cause`` is the first failure of that category, whose
    message says why.
    """

    def __init__(self, failures: list[TranslationRequestError]):
        self.failures = failures
        permanent = [f for f in failures if f.category == "permanent"]
        self.category = "permanent" if permanent else "retryable"
        self.cause = (permanent or failures)[0]
        super().__init__(
            f"translation request failed on page {self.cause.page_number}: {self.cause.message}"
        )
