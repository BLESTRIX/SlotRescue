"""Preserve historical appointment intervals when editing released slots."""

from alembic import op

revision = "c830a6e5419d"
down_revision = "bd72f9a0d418"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE FUNCTION preserve_slot_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.starts_at, NEW.ends_at, NEW.service_id, NEW.staff_id) IS DISTINCT FROM
     (OLD.starts_at, OLD.ends_at, OLD.service_id, OLD.staff_id) AND
     (EXISTS (SELECT 1 FROM bookings WHERE slot_id = OLD.id) OR
      EXISTS (SELECT 1 FROM offers WHERE slot_id = OLD.id)) THEN
    RAISE EXCEPTION 'Historical appointment details are immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER slot_history BEFORE UPDATE OF starts_at, ends_at, service_id, staff_id ON slots
FOR EACH ROW EXECUTE FUNCTION preserve_slot_history();
""")


def downgrade():
    op.execute("DROP TRIGGER slot_history ON slots; DROP FUNCTION preserve_slot_history();")
