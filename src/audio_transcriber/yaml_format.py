"""YAML loading shared by configuration files."""
from __future__ import annotations

import yaml


class StringDateLoader(yaml.SafeLoader):
    """Keep date spelling, offsets and fractional precision on input."""


StringDateLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers
          if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
def _timestamp_string(loader: StringDateLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.constructor.ConstructorError(
            None, None, "Expected a scalar timestamp", node.start_mark
        )
    return loader.construct_scalar(node)


StringDateLoader.add_constructor("tag:yaml.org,2002:timestamp", _timestamp_string)
