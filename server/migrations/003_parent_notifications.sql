-- 003_parent_notifications.sql — per-parent notification flags
--
-- `recordings.notified` is a single flag but there are two parent chats (mom,
-- dad). That collapses a partial failure — mom's send succeeds, dad's fails —
-- into either "notified" (wrong, dad never got it) or "not notified" (wrong,
-- mom would get a duplicate resend). Split it into one flag per parent so each
-- send is tracked independently; `notified` stays as the derived "sent to
-- every configured parent" flag, kept for cheap WHERE-clause filtering
-- (store.pending_notifications) without recomputing it from the two columns.
--
-- Plain ALTER TABLE ADD COLUMN, not IF NOT EXISTS-able — fine per db.migrate's
-- contract, since each migration file only ever runs once (schema_version).

ALTER TABLE recordings ADD COLUMN notified_mom INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recordings ADD COLUMN notified_dad INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_rec_notify ON recordings(origin, notified, deleted);
