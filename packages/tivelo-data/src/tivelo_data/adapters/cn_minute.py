"""Map labeled source minutes onto complete intervals in a pinned exchange session."""

from datetime import timedelta

from .cn_daily import CN

MINUTE = timedelta(minutes=1)


def regular_slots(sessions, request, label):
    """Whole one-minute intervals from the first continuous opening through close."""
    slots = {}
    if not sessions:
        return slots, None
    offset = MINUTE if label == "end" else timedelta()
    for session in sessions:
        start = max(session.start, request.start)
        end = min(session.end, request.end)
        while start + MINUTE <= end:
            key = (start + offset).astimezone(CN).replace(tzinfo=None)
            slots[key] = session.session_id
            start += MINUTE
    terminal = sessions[-1].end - MINUTE + offset
    return slots, terminal.astimezone(CN).replace(tzinfo=None)
