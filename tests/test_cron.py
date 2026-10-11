import unittest
from datetime import datetime

from appbuilder import cron


def dt(*a):
    return datetime(*a)


class CronParseTests(unittest.TestCase):
    def nxt(self, expr, after):
        return cron.next_after(cron.parse(expr), after)

    def test_every_n_minutes_and_hourly_macro(self):
        self.assertEqual(self.nxt('*/15 * * * *', dt(2026, 10, 10, 10, 7)), dt(2026, 10, 10, 10, 15))
        self.assertEqual(self.nxt('@hourly', dt(2026, 10, 10, 10, 0)), dt(2026, 10, 10, 11, 0))

    def test_strictly_after_even_on_exact_match(self):
        self.assertEqual(self.nxt('30 9 * * *', dt(2026, 10, 10, 9, 30)), dt(2026, 10, 11, 9, 30))

    def test_weekday_range_skips_weekend(self):
        # 2026-10-10 is a Saturday; next weekday 09:00 is Monday the 12th.
        self.assertEqual(self.nxt('0 9 * * 1-5', dt(2026, 10, 10, 12, 0)), dt(2026, 10, 12, 9, 0))
        self.assertEqual(self.nxt('0 9 * * MON-FRI', dt(2026, 10, 10, 12, 0)), dt(2026, 10, 12, 9, 0))

    def test_sunday_is_zero_or_seven(self):
        a = self.nxt('0 8 * * 0', dt(2026, 10, 10, 0, 0))
        b = self.nxt('0 8 * * 7', dt(2026, 10, 10, 0, 0))
        self.assertEqual(a, b)
        self.assertEqual(a, dt(2026, 10, 11, 8, 0))  # 2026-10-11 is a Sunday

    def test_lists_ranges_steps_and_month_names(self):
        self.assertEqual(self.nxt('0,30 9-10 * JAN,OCT *', dt(2026, 10, 10, 10, 30)), dt(2026, 10, 11, 9, 0))
        self.assertEqual(self.nxt('10-40/15 * * * *', dt(2026, 10, 10, 10, 0)), dt(2026, 10, 10, 10, 10))

    def test_day_and_weekday_both_restricted_use_or(self):
        # 13th OR Friday. 2026-10-10 is Saturday; next match is Mon the 12th? no: Fri 16th vs 13th -> 13th.
        self.assertEqual(self.nxt('0 0 13 * 5', dt(2026, 10, 10, 1, 0)), dt(2026, 10, 13, 0, 0))

    def test_month_and_year_rollover(self):
        self.assertEqual(self.nxt('0 0 1 1 *', dt(2026, 10, 10, 0, 0)), dt(2027, 1, 1, 0, 0))
        self.assertEqual(self.nxt('59 23 31 12 *', dt(2026, 12, 31, 23, 59)), dt(2027, 12, 31, 23, 59))

    def test_impossible_date_returns_none(self):
        self.assertIsNone(self.nxt('0 0 30 2 *', dt(2026, 10, 10, 0, 0)))

    def test_invalid_expressions_rejected(self):
        for bad in ['', '* * * *', '* * * * * *', '60 * * * *', '* 24 * * *', '* * 0 * *', '* * * 13 *',
                    '*/0 * * * *', '5-1 * * * *', 'a * * * *', '* * * * 8', '*/x * * * *', ',* * * * *']:
            with self.subTest(bad=bad), self.assertRaises(cron.CronError):
                cron.parse(bad)

    def test_next_runs_preview_is_increasing(self):
        runs = cron.next_runs('0 */6 * * *', dt(2026, 10, 10, 1, 0), 5)
        self.assertEqual(runs[0], dt(2026, 10, 10, 6, 0))
        self.assertEqual(runs, sorted(set(runs)))
        self.assertEqual(len(runs), 5)

    def test_latest_due_collapses_missed_runs_to_the_most_recent(self):
        spec = cron.parse('0 * * * *')
        # Due since 03:00; the Mac woke at 06:20 -> catch up 06:00 only, 3 skipped.
        latest, missed = cron.latest_due(spec, dt(2026, 10, 10, 3, 0), dt(2026, 10, 10, 6, 20))
        self.assertEqual((latest, missed), (dt(2026, 10, 10, 6, 0), 3))
        latest, missed = cron.latest_due(spec, dt(2026, 10, 10, 6, 0), dt(2026, 10, 10, 6, 0, 30))
        self.assertEqual((latest, missed), (dt(2026, 10, 10, 6, 0), 0))

    def test_minimum_interval(self):
        self.assertEqual(cron.minimum_interval_minutes('*/5 * * * *', dt(2026, 10, 10)), 5)
        self.assertEqual(cron.minimum_interval_minutes('0 9 * * *', dt(2026, 10, 10)), 1440)
        self.assertEqual(cron.minimum_interval_minutes('0,10 9 * * *', dt(2026, 10, 10)), 10)


if __name__ == '__main__':
    unittest.main()
