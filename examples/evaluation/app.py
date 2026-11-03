"""Deliberately incomplete inputs for the three fixed Software Factory evaluation tasks."""


def describe_items(items):
    return ", ".join(items)


def parse_count(value):
    return int(value)


def normalize_spaces(value):
    return " ".join(segment for segment in value.split(" ") if segment)


def stable_label(value):
    return "item:" + str(value)
