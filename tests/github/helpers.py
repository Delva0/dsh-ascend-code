"""Keep diagnostic assertions independent of the upstream experiment recorder."""

from ascend_github.core.registry import ToolRegistry
from ascend_github.core.storage import ToolStorage


class RecordingStorage(ToolStorage):
    def __init__(self, root):
        super().__init__(root, observer=self.observe)

    def observe(self, kind, **data):
        if kind == "diagnostic":
            self.write("diagnostics/" + data["name"], data["value"])


async def call(tools, query, **kwargs):
    return await ToolRegistry((tools, "github")).call("github", "test", {"query": query, **kwargs})
