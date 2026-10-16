from app.core.errors import DomainError

TRANSITIONS = {
    "Slot": {
        "AVAILABLE": {"BOOKED", "ALLOCATING", "CLOSED"},
        "ALLOCATING": {"HELD", "AVAILABLE", "CLOSED"},
        "HELD": {"BOOKED", "ALLOCATING", "CLOSED"},
        "BOOKED": {"ALLOCATING", "CLOSED"},
        "CLOSED": set(),
    },
    "Booking": {"CONFIRMED": {"CANCELED", "COMPLETED"}, "CANCELED": set(), "COMPLETED": set()},
    "Offer": {
        "ACTIVE": {"ACCEPTED", "DECLINED", "EXPIRED", "REVOKED"},
        "ACCEPTED": set(),
        "DECLINED": set(),
        "EXPIRED": set(),
        "REVOKED": set(),
    },
    "WaitlistEntry": {"ACTIVE": {"FULFILLED", "WITHDRAWN"}, "FULFILLED": set(), "WITHDRAWN": set()},
}


def transition(record, state):
    if state not in TRANSITIONS[type(record).__name__].get(record.state, set()):
        raise DomainError("invalid_transition", f"Cannot change {record.state} to {state}.")
    record.state = state
