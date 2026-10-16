"""Seven-step HTTP demo against a running development stack, including real workers."""

import argparse
import os
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

import httpx


def request(client, method, path, payload=None, key=None):
    headers = {"X-CSRF-Token": client.cookies.get("csrf", "")}
    if key:
        headers["Idempotency-Key"] = key
    response = client.request(method, "/api/v1" + path, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()


def sign_in(client, email):
    client.get("/api/v1/auth/csrf").raise_for_status()
    return request(
        client,
        "POST",
        "/auth/login",
        {"email": email, "password": os.getenv("DEMO_PASSWORD", "DemoPassphrase!2026")},
    )


def until(check, label, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {label}. Check worker/Beat logs and /staff/jobs.")


def collection(client, path, **filters):
    items, page = [], 1
    while True:
        response = client.get("/api/v1" + path, params={**filters, "page": page, "page_size": 100})
        response.raise_for_status()
        result = response.json()
        items.extend(result["items"])
        if len(items) >= result["total"]:
            return items
        page += 1


def run(base_url):
    if os.getenv("APP_ENV", "development") != "development":
        raise SystemExit("This demo uses development accounts and requires APP_ENV=development.")
    with ExitStack() as stack:
        staff, a, b, c = [
            stack.enter_context(httpx.Client(base_url=base_url, timeout=15)) for _ in range(4)
        ]
        member = sign_in(staff, "staff@slotrescue.test")
        for client, email in [
            (a, "a@slotrescue.test"),
            (b, "b@slotrescue.test"),
            (c, "c@slotrescue.test"),
        ]:
            sign_in(client, email)
        # A separate demo service isolates matching from previous demo waitlist requests.
        service = request(
            staff,
            "POST",
            "/staff/services",
            {"name": f"Demo recovery {uuid.uuid4().hex[:6]}", "duration_minutes": 30},
        )
        existing = collection(staff, "/staff/slots", staff_id=member["id"])
        latest = max(
            (datetime.fromisoformat(s["ends_at"]) for s in existing), default=datetime.now(UTC)
        )
        start = max(datetime.now(UTC) + timedelta(days=7), latest + timedelta(hours=1))
        metric_start, metric_end = (
            (datetime.now(UTC) - timedelta(days=1)).date(),
            (start + timedelta(days=2)).date(),
        )

        def metrics():
            response = staff.get(
                "/api/v1/staff/dashboard",
                params={"start": str(metric_start), "end": str(metric_end)},
            )
            response.raise_for_status()
            return response.json()

        before = metrics()["recovered_bookings"]
        slot = request(
            staff,
            "POST",
            "/staff/slots",
            {"service_id": service["id"], "staff_id": member["id"], "starts_at": start.isoformat()},
        )
        until(
            lambda: any(
                s["id"] == slot["id"] and s["state"] == "AVAILABLE"
                for s in collection(staff, "/staff/slots", service_id=service["id"])
            ),
            "initial allocation",
        )
        direct = request(a, "POST", "/bookings", {"slot_id": slot["id"]}, str(uuid.uuid4()))
        print(f"1. Customer A booked slot {slot['id']} (booking {direct['id']}).", flush=True)
        window = {
            "service_id": service["id"],
            "earliest_start": (start - timedelta(hours=1)).isoformat(),
            "latest_end": (start + timedelta(hours=1)).isoformat(),
        }
        request(b, "POST", "/waitlist", window)
        request(c, "POST", "/waitlist", window)
        print("2. Customers B and C joined, in that order.", flush=True)
        request(a, "POST", f"/bookings/{direct['id']}/cancel")
        print("3. A canceled; the durable allocator now owns the opening.", flush=True)

        def find_offer(client):
            return next(
                (
                    o
                    for o in collection(client, "/offers/me")
                    if o["slot_id"] == slot["id"] and o["state"] == "ACTIVE"
                ),
                None,
            )

        def wait_for_offer_email(offer_id):
            # Keep the offer active until SMTP processing completes, so the demo
            # also leaves inspectable offer emails instead of deliberately skipping stale mail.
            until(
                lambda: any(
                    job["dedupe_key"] == f"email:offer:{offer_id}" and job["state"] == "SUCCEEDED"
                    for job in collection(staff, "/staff/jobs")
                ),
                "offer email delivery",
            )

        first = until(lambda: find_offer(b), "B's first offer")
        wait_for_offer_email(first["id"])
        print(f"4. B received first offer {first['id']} and its email.", flush=True)
        request(b, "POST", f"/offers/{first['id']}/decline")
        print("5. B declined; allocation proceeds to the next eligible entry.", flush=True)
        second = until(lambda: find_offer(c), "C's next offer")
        wait_for_offer_email(second["id"])
        recovered = request(c, "POST", f"/offers/{second['id']}/accept", key=str(uuid.uuid4()))
        assert recovered["source"] == "WAITLIST"
        print(f"6. C accepted offer {second['id']} (booking {recovered['id']}).", flush=True)
        after = metrics()["recovered_bookings"]
        assert after == before + 1
        print(
            f"7. Dashboard recovered bookings: {before} -> {after} ({metric_start} to {metric_end}, end exclusive).",
            flush=True,
        )
        print(f"Review {base_url}/staff and Mailpit at http://localhost:8025", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    run(parser.parse_args().base_url)
