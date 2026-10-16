"""Regular 13F deadlines; exceptional SEC closures require explicit period review."""

from datetime import date, timedelta

from .evidence import available_by, day, timestamp
from .holdings import quarter


def filing_deadline(report_period):
    end = day(quarter(report_period))
    deadline = end + timedelta(days=45)
    february_first = date(deadline.year, 2, 1)
    presidents_day = february_first + timedelta(days=(0 - february_first.weekday()) % 7 + 14)
    # Regular 13F dates are Feb 14, May 15, Aug 14, Nov 14; only the February
    # weekend roll can meet a recurring federal holiday. SEC FAQ 26 (2026).
    while deadline.weekday() >= 5 or deadline == presidents_day:
        deadline += timedelta(days=1)
    return deadline


def default_report_period(cutoff):
    today = timestamp(cutoff).date()
    for year in range(today.year, today.year - 3, -1):
        for month, last in ((12, 31), (9, 30), (6, 30), (3, 31)):
            end = date(year, month, last)
            if end < today and available_by(filing_deadline(end.isoformat()).isoformat(), cutoff):
                return end.isoformat()
    raise ValueError("No report period found")
