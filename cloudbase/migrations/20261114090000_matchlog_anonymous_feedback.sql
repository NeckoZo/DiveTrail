-- Keep the v1 RPC for old clients and attach optional feedback to the first batch.
BEGIN;

ALTER TABLE matchlog_private.capture_log_batches
    ADD COLUMN IF NOT EXISTS feedback text;

CREATE OR REPLACE FUNCTION public.matchlog_submit_diagnostics_v2(batch jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    caller text := matchlog_private.anonymous_owner();
    note text := batch->>'feedback';
    event_count integer;
    written integer;
BEGIN
    IF jsonb_typeof(batch) IS DISTINCT FROM 'object'
        OR octet_length(batch::text) > 64000
        OR jsonb_typeof(batch->'feedback') IS DISTINCT FROM 'string'
        OR char_length(note) NOT BETWEEN 1 AND 1000
        OR replace(replace(note, chr(9), ''), chr(10), '') ~ '[[:cntrl:]]'
        OR note !~ '[^[:space:]]'
        OR batch->'index' IS DISTINCT FROM '0'::jsonb
        OR jsonb_typeof(batch->'events') IS DISTINCT FROM 'array'
        OR (batch->>'receipt' ~ '^[0-9a-f]{64}$') IS DISTINCT FROM TRUE
        OR (batch->>'version' ~ '^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$') IS DISTINCT FROM TRUE
        OR (batch->>'range' IN ('24h', 'all', 'none')) IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'Invalid anonymous feedback' USING ERRCODE = '22023';
    END IF;

    event_count := jsonb_array_length(batch->'events');
    IF event_count = 0 THEN
        IF batch->'total' IS DISTINCT FROM '1'::jsonb THEN
            RAISE EXCEPTION 'Invalid feedback batch count' USING ERRCODE = '22023';
        END IF;
        INSERT INTO matchlog_private.capture_log_batches AS saved
            (owner, receipt, batch_index, batch_total, requested_range, client_version, events, feedback)
        VALUES (caller, batch->>'receipt', 0, 1, batch->>'range', batch->>'version', '[]'::jsonb, note)
        ON CONFLICT ON CONSTRAINT capture_log_batches_pkey DO UPDATE SET feedback = EXCLUDED.feedback
        WHERE saved.events = EXCLUDED.events AND saved.feedback = EXCLUDED.feedback
            AND saved.batch_total = EXCLUDED.batch_total AND saved.requested_range = EXCLUDED.requested_range
            AND saved.client_version = EXCLUDED.client_version;
    ELSE
        -- V1 validates every diagnostic event and the remainder of the batch.
        PERFORM public.matchlog_submit_diagnostics_v1(batch - 'feedback');
        UPDATE matchlog_private.capture_log_batches AS saved SET feedback = note
        WHERE saved.owner = caller AND saved.receipt = batch->>'receipt' AND saved.batch_index = 0
            AND (saved.feedback IS NULL OR saved.feedback = note);
    END IF;
    GET DIAGNOSTICS written = ROW_COUNT;
    IF written <> 1 THEN
        RAISE EXCEPTION 'Conflicting feedback batch' USING ERRCODE = '22023';
    END IF;
    RETURN jsonb_build_object('accepted', event_count);
END;
$$;

REVOKE ALL ON FUNCTION public.matchlog_submit_diagnostics_v2(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.matchlog_submit_diagnostics_v2(jsonb) TO anon;
NOTIFY pgrst, 'reload schema';
COMMIT;
