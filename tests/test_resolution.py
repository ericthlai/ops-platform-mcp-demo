"""Name resolution must not guess a write target from missing input."""

import pytest

from mcp_server.resolution import pick_by_name


@pytest.mark.parametrize("query", ["", " ", "\t\n"])
def test_blank_query_does_not_select_the_only_available_item(query):
    with pytest.raises(ValueError, match="must not be blank"):
        pick_by_name("project", [{"id": 1, "name": "Orion"}], query)


def test_nonblank_query_preserves_unique_fragment_resolution():
    item = {"id": 1, "name": "Orion"}
    assert pick_by_name("project", [item], " ori ") == item
