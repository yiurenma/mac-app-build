"""Small dependency-free 5-field cron parser (minute hour day month weekday).

All times are naive local datetimes with minute resolution. Supports ``*``,
lists (``1,15``), ranges (``9-17``), steps (``*/10``, ``0-30/5``), month/weekday
names (``JAN``, ``MON-FRI``) and the ``@hourly/@daily/@weekly/@monthly`` macros.
Day-of-month and weekday follow classic cron: when both are restricted a day
matches if EITHER matches.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MACROS = {'@hourly': '0 * * * *', '@daily': '0 0 * * *', '@midnight': '0 0 * * *',
          '@weekly': '0 0 * * 0', '@monthly': '0 0 1 * *'}
MONTH_NAMES = {n: i for i, n in enumerate(
    ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'), 1)}
DAY_NAMES = {n: i for i, n in enumerate(('SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'))}
SEARCH_YEARS = 5


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class CronSpec:
    minutes: tuple
    hours: tuple
    days: frozenset
    months: frozenset
    weekdays: frozenset
    day_star: bool
    weekday_star: bool


def _number(token, names, label):
    token = token.strip().upper()
    if token in names:
        return names[token]
    if not token.isascii() or not token.isdigit():
        raise CronError(f'{label} 字段包含无法识别的值: {token or "(空)"}')
    return int(token)


def _field(text, low, high, label, names=None):
    names = names or {}
    values = set()
    if not text:
        raise CronError(f'{label} 字段不能为空')
    for part in text.split(','):
        base, slash, step_text = part.partition('/')
        step = 1
        if slash:
            if not step_text.isascii() or not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f'{label} 字段的步长必须是正整数: {part}')
            step = int(step_text)
        if base == '*':
            start, end = low, high
        elif '-' in base:
            a, _, b = base.partition('-')
            start, end = _number(a, names, label), _number(b, names, label)
        else:
            start = _number(base, names, label)
            end = high if slash else start
        if not (low <= start <= high and low <= end <= high):
            raise CronError(f'{label} 字段必须在 {low}～{high} 之间: {part}')
        if start > end:
            raise CronError(f'{label} 字段范围起点大于终点: {part}')
        values.update(range(start, end + 1, step))
    return values


def parse(expression):
    if not isinstance(expression, str):
        raise CronError('cron 表达式必须是文本')
    text = expression.strip()
    text = MACROS.get(text.lower(), text)
    parts = text.split()
    if len(parts) != 5:
        raise CronError('cron 需要 5 段：分 时 日 月 周，例如 "0 9 * * 1-5"')
    minute, hour, day, month, weekday = parts
    weekdays = {v % 7 for v in _field(weekday, 0, 7, '星期', DAY_NAMES)}
    return CronSpec(
        minutes=tuple(sorted(_field(minute, 0, 59, '分钟'))),
        hours=tuple(sorted(_field(hour, 0, 23, '小时'))),
        days=frozenset(_field(day, 1, 31, '日')),
        months=frozenset(_field(month, 1, 12, '月', MONTH_NAMES)),
        weekdays=frozenset(weekdays),
        day_star=day.startswith('*'),
        weekday_star=weekday.startswith('*'),
    )


def _day_matches(spec, moment):
    dom = moment.day in spec.days
    # Python: Monday=0; cron: Sunday=0.
    dow = (moment.weekday() + 1) % 7 in spec.weekdays
    if spec.day_star and spec.weekday_star:
        return True
    if spec.day_star:
        return dow
    if spec.weekday_star:
        return dom
    return dom or dow


def next_after(spec, after):
    """First matching minute strictly after ``after``; None if none within years."""
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366 * SEARCH_YEARS)
    while t <= limit:
        if t.month not in spec.months:
            year, month = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
            t = datetime(year, month, 1)
            continue
        if not _day_matches(spec, t):
            t = datetime(t.year, t.month, t.day) + timedelta(days=1)
            continue
        if t.hour not in spec.hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        later = [m for m in spec.minutes if m >= t.minute]
        if not later:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        return t.replace(minute=later[0])
    return None


def next_runs(expression, after, count=5):
    spec = parse(expression)
    runs, cursor = [], after
    while len(runs) < count:
        cursor = next_after(spec, cursor)
        if cursor is None:
            break
        runs.append(cursor)
    return runs


def latest_due(spec, first_due, now):
    """Collapse every occurrence in [first_due, now] into the most recent one.

    Returns ``(latest, missed)`` where ``missed`` counts the earlier occurrences
    that are intentionally not replayed.
    """
    # Count by calendar day, so a long sleep is never truncated to the first
    # 10,000 occurrences. Even years offline still catch up only the latest.
    day = first_due.replace(hour=0, minute=0, second=0, microsecond=0)
    latest, count = first_due, 0
    while day.date() <= now.date():
        if day.month in spec.months and _day_matches(spec, day):
            for hour in spec.hours:
                for minute in spec.minutes:
                    candidate = day.replace(hour=hour, minute=minute)
                    if first_due <= candidate <= now:
                        latest = candidate
                        count += 1
        day += timedelta(days=1)
    return latest, max(0, count - 1)


def minimum_interval_minutes(expression, after, *, samples=200):
    """Smallest gap among the next ``samples`` runs (None if fewer than two)."""
    runs = next_runs(expression, after, samples)
    if len(runs) < 2:
        return None
    return min(int((b - a).total_seconds() // 60) for a, b in zip(runs, runs[1:]))
