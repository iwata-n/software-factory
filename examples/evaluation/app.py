"""Deliberately incomplete inputs for the three fixed Software Factory evaluation tasks."""


def describe_items(items):
    if isinstance(items, list) and not items:
        return "データがありません"
    return ", ".join(items)


def parse_count(value):
    return int(value)


def normalize_spaces(value):
    return value.replace("  ", " ")


def stable_label(value):
    return "item:" + str(value)
