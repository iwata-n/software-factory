"""Deliberately incomplete inputs for the three fixed Software Factory evaluation tasks."""


def describe_items(items):
    return ", ".join(items)


def parse_count(value):
    if not 1 <= len(value) <= 9 or not all("0" <= char <= "9" for char in value):
        raise ValueError("count must contain one to nine ASCII digits")
    return int(value)


def normalize_spaces(value):
    return value.replace("  ", " ")


def stable_label(value):
    return "item:" + str(value)
