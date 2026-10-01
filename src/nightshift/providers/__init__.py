"""Provider adapters. Reasoning stays in the external CLI, not here."""

from nightshift.providers.fake import FakeProvider
from nightshift.providers.grok import GrokProvider


def get_provider(name: str):
    if name == "fake":
        return FakeProvider()
    if name == "grok":
        return GrokProvider()
    raise KeyError(name)
