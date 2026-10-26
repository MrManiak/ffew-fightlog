from django import template

register = template.Library()


@register.filter
def isk(value) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    for unit, size in (("b", 1e9), ("m", 1e6), ("k", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:.0f}"


@register.filter
def duration(seconds) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {sec:02d}s"


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(str(key), mapping.get(key))
    except AttributeError:
        return None
