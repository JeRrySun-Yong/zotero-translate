"""Prompt rules for user model profiles only (M4 ticket 15, ADR 0005 supplement).

A user profile's task appends these blocks to its prompts; the built-in
profile "Gemini" never gets them, so its request bodies, and the cache keys
of its cached answers, stay as they were (``tests/test_prompt_protections.py``
pins them). The prompts' shared text is the same for every profile: a
user profile's prompt is Gemini's plus these blocks, nothing else.

- ``BATCH_RULES`` goes into every translation batch prompt, after the
  rule blocks (``TranslationConfig.batch_extra_rules``).
- ``REVIEW_RULES`` goes into every review prompt, after the instructions
  (``BatchReviewer``'s ``extra_rules``).

They fix what the DeepSeek diagnosis found (ticket 14): a value of a set
or a JSON string translated. The examples are generic; an example taken
from the acceptance papers would bias the papers the rules are judged on.

``PipelineSettings.user_profile_rules`` turns them on: every user profile's
task (``load_profile_settings``), and ``run --user-profile-rules`` (for
development, so that a user profile's file can be used with ``run`` as it
is). Gemini's two endpoint files cannot turn them on.
"""

LITERAL_VALUES_RULE = (
    "- Values inside formulas and set notation (e.g. {low, medium, high}), string values in "
    'JSON or code (e.g. "status": "ready"), identifiers, constants and other literals stay '
    "exactly as in the WHERE; never translate such a value."
)

BATCH_RULES = "## Literal Values\n" + LITERAL_VALUES_RULE.replace("WHERE", "input") + "\n\n"

REVIEW_RULES = "## Literal values\n" + LITERAL_VALUES_RULE.replace("WHERE", "source")
