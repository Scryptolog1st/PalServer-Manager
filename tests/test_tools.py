from palserver_manager.tools import TOOLS


def test_tool_ids_unique():
    ids = [tool.id for tool in TOOLS]
    assert len(ids) == len(set(ids))
    assert len(TOOLS) > 8
