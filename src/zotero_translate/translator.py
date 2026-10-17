"""Our translator for the engine: all requests go through the pipeline model client.

It inherits ``OpenAITranslator`` rather than ``BaseTranslator``, because the
base class's default placeholders make the engine drop the letter "b"
(BabelDOC #624). The engine's own translation cache is bypassed
(``ignore_cache=True``): it lives in the shared engine cache folder and its
key ignores the endpoint, so test runs against the fake model could
otherwise pollute real runs.
"""

from __future__ import annotations

from babeldoc.translator.translator import OpenAITranslator

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
        return self.model_client.complete([{"role": "user", "content": text}])

    def do_translate(self, text, rate_limit_params: dict | None = None) -> str:
        return self.model_client.complete(self.prompt(text))
