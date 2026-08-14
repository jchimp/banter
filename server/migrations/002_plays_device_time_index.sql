-- 002_plays_device_time_index.sql — index plays(device_id, played_at) for
-- store.last_played_recording_id()'s "most recent play for this device" lookup.
--
-- The existing idx_plays_dev(device_id, recording_id) narrows by device_id but
-- doesn't cover played_at, so `ORDER BY played_at DESC LIMIT 1` still needs a
-- temp b-tree sort over every play row for that device (confirmed via
-- EXPLAIN QUERY PLAN). This index lets sqlite walk the index in played_at order
-- and stop at the first row.

CREATE INDEX IF NOT EXISTS idx_plays_dev_time ON plays(device_id, played_at DESC);
