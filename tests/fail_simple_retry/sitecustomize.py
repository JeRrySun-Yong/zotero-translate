"""Test only: make the engine's simple one-paragraph translation of one paragraph fail.

Put this directory on ``PYTHONPATH`` and set ``ZT_TEST_FAIL_SIMPLE_RETRY`` to a
piece of a paragraph's source text: in the processes started that way, the
engine's simple one-paragraph prompt (``ILTranslator.generate_prompt_for_llm``,
used only to redo a paragraph after its batch) raises for that paragraph, as
an unexpected error would, before any request is sent (M4 ticket 15). The
paragraph then keeps its source, which the engine records as 逐段翻译出错.
"""

import os

_MARKER = os.environ.get("ZT_TEST_FAIL_SIMPLE_RETRY")

if _MARKER:
    from babeldoc.format.pdf.document_il.midend import il_translator

    _build = il_translator.ILTranslator.generate_prompt_for_llm

    def _failing(self, text, *args, **kwargs):
        if _MARKER in text:
            raise RuntimeError("test: the simple one-paragraph translation fails")
        return _build(self, text, *args, **kwargs)

    il_translator.ILTranslator.generate_prompt_for_llm = _failing
