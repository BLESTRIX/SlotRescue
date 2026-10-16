"""Deferred occupancy guards and service/slot consistency."""

from alembic import op

revision = "bd72f9a0d418"
down_revision = "9871808b1030"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE FUNCTION validate_slot_interval() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE duration integer; member_role text;
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.state IN ('HELD','BOOKED') AND
     (NEW.starts_at, NEW.ends_at, NEW.staff_id, NEW.service_id) IS DISTINCT FROM
     (OLD.starts_at, OLD.ends_at, OLD.staff_id, OLD.service_id) THEN
    RAISE EXCEPTION 'Occupied slot cannot be edited' USING ERRCODE = '23514';
  END IF;
  SELECT duration_minutes INTO duration FROM services WHERE id = NEW.service_id FOR SHARE;
  SELECT role INTO member_role FROM users WHERE id = NEW.staff_id;
  IF NEW.ends_at - NEW.starts_at <> make_interval(mins => duration) OR member_role <> 'STAFF' THEN
    RAISE EXCEPTION 'Slot must match service duration and staff role' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER slot_interval BEFORE INSERT OR UPDATE OF starts_at, ends_at, service_id, staff_id
ON slots FOR EACH ROW EXECUTE FUNCTION validate_slot_interval();

CREATE FUNCTION validate_service_duration() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.duration_minutes <> OLD.duration_minutes AND EXISTS
    (SELECT 1 FROM slots WHERE service_id = NEW.id AND starts_at > clock_timestamp()) THEN
    RAISE EXCEPTION 'Future slots prevent duration changes' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER service_duration BEFORE UPDATE OF duration_minutes ON services
FOR EACH ROW EXECUTE FUNCTION validate_service_duration();

CREATE FUNCTION validate_slot_occupancy() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE sid integer; slot_state text; bookings_count integer; offers_count integer;
BEGIN
  IF TG_TABLE_NAME = 'slots' THEN sid := NEW.id;
  ELSE sid := COALESCE(NEW.slot_id, OLD.slot_id); END IF;
  SELECT state INTO slot_state FROM slots WHERE id = sid FOR UPDATE;
  SELECT count(*) INTO bookings_count FROM bookings WHERE slot_id = sid AND state = 'CONFIRMED';
  SELECT count(*) INTO offers_count FROM offers WHERE slot_id = sid AND state = 'ACTIVE';
  IF bookings_count + offers_count > 1 OR
     (slot_state = 'BOOKED' AND bookings_count <> 1) OR
     (slot_state = 'HELD' AND offers_count <> 1) OR
     (bookings_count = 1 AND slot_state <> 'BOOKED') OR
     (offers_count = 1 AND slot_state <> 'HELD') THEN
    RAISE EXCEPTION 'Slot occupancy and state disagree' USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER slots_occupancy AFTER INSERT OR UPDATE ON slots
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_slot_occupancy();
CREATE CONSTRAINT TRIGGER bookings_occupancy AFTER INSERT OR UPDATE OR DELETE ON bookings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_slot_occupancy();
CREATE CONSTRAINT TRIGGER offers_occupancy AFTER INSERT OR UPDATE OR DELETE ON offers
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_slot_occupancy();

CREATE FUNCTION immutable_occupant() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.slot_id, NEW.customer_id) IS DISTINCT FROM (OLD.slot_id, OLD.customer_id) THEN
    RAISE EXCEPTION 'Occupant identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER bookings_identity BEFORE UPDATE ON bookings
FOR EACH ROW EXECUTE FUNCTION immutable_occupant();
CREATE TRIGGER offers_identity BEFORE UPDATE ON offers
FOR EACH ROW EXECUTE FUNCTION immutable_occupant();
""")


def downgrade():
    op.execute("""
DROP TRIGGER offers_identity ON offers;
DROP TRIGGER bookings_identity ON bookings;
DROP FUNCTION immutable_occupant();
DROP TRIGGER offers_occupancy ON offers;
DROP TRIGGER bookings_occupancy ON bookings;
DROP TRIGGER slots_occupancy ON slots;
DROP FUNCTION validate_slot_occupancy();
DROP TRIGGER service_duration ON services;
DROP FUNCTION validate_service_duration();
DROP TRIGGER slot_interval ON slots;
DROP FUNCTION validate_slot_interval();
""")
