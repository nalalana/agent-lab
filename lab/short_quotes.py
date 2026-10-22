"""Lab-only short-quotation prompt candidate; production validation is unchanged."""
from apps.agent_runtime.response_verification import ModelResponseVerifier


SHORT_QUOTE_GUIDANCE = """Copy each quote as the shortest sufficient continuous substring of source.content. Multiple quotations may jointly support one claim. For JSON content, preserve literal backslashes, quotation marks and newline escapes exactly; never quote a decoded or rewritten string. Prefer shorter fragments that do not cross escape boundaries. Still verify the entire claim: quotations must retain the relevant qualifications, attribution, entity and time, including uncertainty, negation and exceptions. Do not select only favorable fragments or treat substring presence as proof of entailment. These copying rules do not change source authority or any verification requirement."""


class ShortQuoteVerifier(ModelResponseVerifier):
    def __init__(self, model_gateway, *, prompt_suffix="", strategy="evidence_scope"):
        super().__init__(model_gateway, prompt_suffix=prompt_suffix + "\n" + SHORT_QUOTE_GUIDANCE,
                         strategy=strategy)
