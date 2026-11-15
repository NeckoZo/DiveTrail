-- Anonymous capture events are stored apart from match statistics.
BEGIN;

CREATE TABLE IF NOT EXISTS matchlog_private.capture_log_batches (
    owner text NOT NULL,
    receipt text NOT NULL,
    batch_index integer NOT NULL,
    batch_total integer NOT NULL,
    requested_range text NOT NULL,
    client_version text NOT NULL,
    events jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, receipt, batch_index)
);
REVOKE ALL ON matchlog_private.capture_log_batches FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION public.matchlog_submit_diagnostics_v1(batch jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    caller text := matchlog_private.anonymous_owner();
    entry jsonb;
    entries jsonb;
    batch_index integer;
    batch_total integer;
    affected integer;
    event_codes constant text[] := ARRAY[
        'no_profile', 'no_game', 'manual_requested', 'paused', 'resumed', 'manual_expired',
        'focus_lost', 'black_frame', 'capture_error', 'hotkey_unavailable', 'hotkey_available', 'seen',
        'queued', 'deferred', 'queue_skipped', 'duplicate', 'reading', 'recognized',
        'unknown', 'wrong_scope', 'game_key_missing', 'round_relinked', 'unstable', 'tally_pending',
        'overview_only', 'missing_rp', 'identity_unconfirmed', 'record_blocked', 'saved', 'partial',
        'auxiliary', 'lobby_only', 'ignored', 'unchanged', 'review', 'recognition_error',
        'fallback', 'profile_waiting', 'profile_lobby', 'profile_reading', 'profile_mode', 'profile_name_missing',
        'profile_confirming', 'profile_ready', 'profile_timeout', 'profile_capture_error', 'retry_saved', 'open_round'
    ];
    phases constant text[] := ARRAY[
        'hud', 'lobby', 'lobby_candidate', 'settlement', 'summary', 'rank',
        'rank_overview', 'death_recap', 'postmatch', 'spectator', 'profile', 'training',
        'intro', 'legend_select', 'loading', 'manual', 'probe', 'unknown',
        'other'
    ];
BEGIN
    IF jsonb_typeof(batch) IS DISTINCT FROM 'object' OR octet_length(batch::text) > 64000 THEN
        RAISE EXCEPTION 'Invalid capture log batch' USING ERRCODE = '22023';
    END IF;
    IF (batch->>'receipt' ~ '^[0-9a-f]{64}$') IS DISTINCT FROM TRUE
        OR (batch->>'range' IN ('24h','all')) IS DISTINCT FROM TRUE
        OR (batch->>'version' ~ '^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$') IS DISTINCT FROM TRUE
        OR jsonb_typeof(batch->'index') IS DISTINCT FROM 'number'
        OR jsonb_typeof(batch->'total') IS DISTINCT FROM 'number'
        OR (batch->>'index' ~ '^[0-9]{1,4}$') IS DISTINCT FROM TRUE
        OR (batch->>'total' ~ '^[0-9]{1,4}$') IS DISTINCT FROM TRUE
        OR jsonb_typeof(batch->'events') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Invalid capture log batch metadata' USING ERRCODE = '22023';
    END IF;
    batch_index := (batch->>'index')::integer;
    batch_total := (batch->>'total')::integer;
    entries := batch->'events';
    IF batch_total NOT BETWEEN 1 AND 2000 OR batch_index NOT BETWEEN 0 AND batch_total-1
        OR jsonb_array_length(entries) NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION 'Invalid capture log batch size' USING ERRCODE = '22023';
    END IF;

    FOR entry IN SELECT value FROM jsonb_array_elements(entries) LOOP
        IF jsonb_typeof(entry) IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid capture event' USING ERRCODE = '22023';
        END IF;
        IF jsonb_array_length(entry) <> 5 OR jsonb_typeof(entry->0) IS DISTINCT FROM 'number'
            OR (entry->>0 ~ '^[0-9]{12,13}$') IS DISTINCT FROM TRUE
            OR (entry->>1 = ANY(event_codes)) IS DISTINCT FROM TRUE
            OR NOT (entry->2 = 'null'::jsonb OR COALESCE(entry->>2 = ANY(phases),false))
            OR NOT (entry->3 = 'null'::jsonb OR (jsonb_typeof(entry->3) = 'number' AND entry->>3 ~ '^[1-9][0-9]{0,9}$'))
            OR NOT (entry->4 = 'null'::jsonb OR (jsonb_typeof(entry->4) = 'number' AND entry->>4 ~ '^[1-9][0-9]{0,9}$')) THEN
            RAISE EXCEPTION 'Invalid capture event fields' USING ERRCODE = '22023';
        END IF;
    END LOOP;

    INSERT INTO matchlog_private.capture_log_batches AS saved
        (owner, receipt, batch_index, batch_total, requested_range, client_version, events)
    VALUES (caller, batch->>'receipt', batch_index, batch_total, batch->>'range', batch->>'version', entries)
    ON CONFLICT ON CONSTRAINT capture_log_batches_pkey DO UPDATE SET events = EXCLUDED.events
    WHERE saved.events = EXCLUDED.events AND saved.batch_total = EXCLUDED.batch_total
        AND saved.requested_range = EXCLUDED.requested_range AND saved.client_version = EXCLUDED.client_version;
    GET DIAGNOSTICS affected = ROW_COUNT;
    IF affected <> 1 THEN
        RAISE EXCEPTION 'Conflicting capture log batch' USING ERRCODE = '22023';
    END IF;
    RETURN jsonb_build_object('accepted', jsonb_array_length(entries));
END;
$$;
REVOKE ALL ON FUNCTION public.matchlog_submit_diagnostics_v1(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.matchlog_submit_diagnostics_v1(jsonb) TO anon;
COMMIT;
