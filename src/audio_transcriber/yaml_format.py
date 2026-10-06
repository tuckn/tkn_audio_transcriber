"""YAML styles shared by generated configuration files."""
from __future__ import annotations

import re
from typing import Any

import yaml

_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?$"
)
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


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


class GuidelineDumper(yaml.SafeDumper):
    """Quote dates and Windows paths without changing their values."""


def _represent_string(dumper: GuidelineDumper, value: str) -> yaml.ScalarNode:
    style = None
    if _DATE.fullmatch(value):
        style = '"'
    elif not _URL.match(value) and (
        re.match(r"^[A-Za-z]:", value) or "\\" in value or value.startswith("//")
    ):
        style = "'"
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


GuidelineDumper.add_representer(str, _represent_string)


def dump_yaml(value: dict[str, Any]) -> str:
    return yaml.dump(value, Dumper=GuidelineDumper, allow_unicode=True, sort_keys=False)
