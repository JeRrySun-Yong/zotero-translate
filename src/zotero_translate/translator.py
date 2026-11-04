"""Our translator for the engine: all requests go through the pipeline model client.

It inherits ``OpenAITranslator`` rather than ``BaseTranslator``, because the
base class's default placeholders make the engine drop the letter "b"
(BabelDOC #624). The engine's own translation cache is bypassed
(``ignore_cache=True``): it lives in the shared engine cache folder and its
key ignores the endpoint, so test runs against the fake model could
otherwise pollute real runs; the pipeline model client keeps its own
response cache.

Requests are retried by the pipeline model client. A request that fails for
good is raised as the engine's ``TranslationRequestError``, with the
failure category of the ``ModelCallError``, which makes the engine fail the
translation stage instead of keeping the source text. The
inherited OpenAI SDK retry on HTTP 429 does not apply: our overrides replace
the decorated methods.
"""

from __future__ import annotations

from babeldoc.babeldoc_exception.BabelDOCException import TranslationRequestError
from babeldoc.translator.translator import OpenAITranslator

from zotero_translate.model_client import ModelCallError
from zotero_translate.model_client import ModelClient


class PipelineTranslator(OpenAITranslator):
    # The engine's cache limits the name to 20 characters.
    name = "zotero-translate"

    def __init__(self, client: ModelClient, lang_in: str = "en", lang_out: str = "zh"):
        # The parent builds an OpenAI SDK client that we never use; give it a
        # placeholder key so the real key stays only in the pipeline model client.
        super().__init__(
            lang_in=lang_in,
            lang_out=lang_out,
            model=client.model,
            base_url=client.settings.base_url,
            api_key="unused",
            ignore_cache=True,
        )
        self.model_client = client

    def do_llm_translate(self, text, rate_limit_params: dict | None = None):
        if text is None:  # the engine probes LLM support with None
            return None
        params = rate_limit_params or {}
        return self._complete(
            [{"role": "user", "content": text}],
            params.get("validate_output"),
            params.get("page_number"),
        )

    def do_translate(self, text, rate_limit_params: dict | None = None) -> str:
        page = (rate_limit_params or {}).get("page_number")
        return self._complete(self.prompt(text), page=page)

    def _complete(self, messages: list[dict], validate=None, page: int | None = None) -> str:
        try:
            return self.model_client.complete(messages, validate=validate, page=page)
        except ModelCallError as e:
            raise TranslationRequestError(str(e), category=e.category) from e
