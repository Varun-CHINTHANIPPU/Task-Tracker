from datetime import datetime, date, timedelta

FMT = "%Y-%m-%dT%H:%M:%S.%f%z"


def now():
    return datetime.now().astimezone()


def iso(dt=None):
    dt = dt or now()
    return dt.isoformat(timespec="seconds")


def local_date(dt=None):
    return (dt or now()).date()


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def date_str(value=None):
    if value is None:
        return local_date().isoformat()
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(value)


def add_days(day_string, days=1):
    return (date.fromisoformat(day_string) + timedelta(days=days)).isoformat()


def fmt_seconds(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"
