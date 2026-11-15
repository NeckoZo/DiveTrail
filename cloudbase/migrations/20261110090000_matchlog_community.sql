-- CloudBase JWT claims are supplied by PostgREST, never by RPC input.
BEGIN;
CREATE SCHEMA IF NOT EXISTS matchlog_private;
REVOKE ALL ON SCHEMA matchlog_private FROM PUBLIC, anon, authenticated;

CREATE TABLE IF NOT EXISTS matchlog_private.community_matches (
    owner text NOT NULL,
    match_key text NOT NULL,
    day date NOT NULL,
    season text NOT NULL,
    split text NOT NULL,
    mode text NOT NULL,
    hero text NOT NULL,
    placement integer NOT NULL CHECK (placement BETWEEN 1 AND 30),
    damage integer NOT NULL CHECK (damage BETWEEN 0 AND 20000),
    PRIMARY KEY (owner, match_key)
);
CREATE INDEX IF NOT EXISTS community_segment
    ON matchlog_private.community_matches(season, split, mode, owner);
REVOKE ALL ON matchlog_private.community_matches FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION matchlog_private.anonymous_owner()
RETURNS text LANGUAGE plpgsql STABLE SET search_path = '' AS $$
DECLARE
    claims jsonb := COALESCE(NULLIF(current_setting('request.jwt.claims', true), ''), '{}')::jsonb;
    owner text := claims->>'sub';
BEGIN
    IF claims->>'role' IS DISTINCT FROM 'anon'
        OR claims->>'is_anonymous' IS DISTINCT FROM 'true'
        OR owner IS NULL OR owner IN ('','anon') OR length(owner)>128 THEN
        RAISE EXCEPTION 'Anonymous sign-in required' USING ERRCODE = '42501';
    END IF;
    RETURN owner;
END;
$$;
REVOKE ALL ON FUNCTION matchlog_private.anonymous_owner() FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION public.matchlog_submit_v1(matches jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    caller text := matchlog_private.anonymous_owner();
    item jsonb;
    affected integer;
BEGIN
    IF jsonb_typeof(matches) IS DISTINCT FROM 'array'
        OR octet_length(matches::text) > 64000 THEN
        RAISE EXCEPTION 'Invalid match batch' USING ERRCODE = '22023';
    END IF;
    IF jsonb_array_length(matches) NOT BETWEEN 1 AND 100 OR (
        SELECT count(DISTINCT record->>'key') FROM jsonb_array_elements(matches) AS records(record)
    ) <> jsonb_array_length(matches) THEN
        RAISE EXCEPTION 'Invalid or duplicate match keys' USING ERRCODE = '22023';
    END IF;
    FOR item IN SELECT jsonb_array_elements(matches) LOOP
        IF jsonb_typeof(item) <> 'object' OR NOT item ?& ARRAY[
            'key','day','season','split','mode','hero','placement','damage'
        ] OR item - ARRAY['key','day','season','split','mode','hero','placement','damage'] <> '{}'::jsonb
            OR COALESCE(item->>'key','') !~ '^[0-9a-f]{64}$'
            OR COALESCE(item->>'day','') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            OR COALESCE(item->>'season','') !~ '^S[0-9]{1,3}$'
            OR COALESCE(item->>'split','') NOT IN ('1','2')
            OR COALESCE(item->>'mode','') NOT IN ('ranked','casual','event')
            OR COALESCE(item->>'hero','') !~ '^[a-z][a-z0-9-]{0,29}$'
            OR jsonb_typeof(item->'placement') <> 'number'
            OR COALESCE(item->>'placement','') !~ '^[0-9]{1,2}$'
            OR jsonb_typeof(item->'damage') <> 'number'
            OR COALESCE(item->>'damage','') !~ '^[0-9]{1,5}$' THEN
            RAISE EXCEPTION 'Invalid match fields' USING ERRCODE = '22023';
        END IF;
        IF (item->>'day')::date NOT BETWEEN CURRENT_DATE-366 AND CURRENT_DATE+1 THEN
            RAISE EXCEPTION 'Match date outside allowed range' USING ERRCODE = '22023';
        END IF;
    END LOOP;
    -- Serialize batches from the same installation and bound its retained row count.
    PERFORM pg_advisory_xact_lock(hashtextextended(caller,0));
    IF (SELECT count(*) FROM matchlog_private.community_matches WHERE owner=caller) +
       (SELECT count(*) FROM jsonb_array_elements(matches) AS records(record)
        WHERE NOT EXISTS (SELECT 1 FROM matchlog_private.community_matches
            WHERE owner=caller AND match_key=record->>'key')) > 20000 THEN
        RAISE EXCEPTION 'Match limit reached' USING ERRCODE = '22023';
    END IF;
    INSERT INTO matchlog_private.community_matches(owner,match_key,day,season,split,mode,hero,placement,damage)
    SELECT caller, record->>'key', (record->>'day')::date, record->>'season', record->>'split',
        record->>'mode', record->>'hero', (record->>'placement')::integer, (record->>'damage')::integer
    FROM jsonb_array_elements(matches) AS records(record)
    ON CONFLICT (owner, match_key) DO UPDATE SET day=excluded.day,season=excluded.season,split=excluded.split,
        mode=excluded.mode,hero=excluded.hero,placement=excluded.placement,damage=excluded.damage;
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN jsonb_build_object('accepted', affected);
END;
$$;

CREATE OR REPLACE FUNCTION public.matchlog_summary_v1(requested_season text, requested_split text, requested_mode text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    caller text := matchlog_private.anonymous_owner();
    result jsonb;
BEGIN
    IF COALESCE(requested_season,'') !~ '^S[0-9]{1,3}$' OR COALESCE(requested_split,'') NOT IN ('1','2')
        OR COALESCE(requested_mode,'') NOT IN ('ranked','casual','event') THEN
        RAISE EXCEPTION 'Invalid statistics scope' USING ERRCODE = '22023';
    END IF;
    WITH players AS (
        SELECT avg(damage) AS damage, sum((placement=1)::integer) AS wins, count(*) AS games
        FROM matchlog_private.community_matches WHERE season=requested_season
            AND split=requested_split AND mode=requested_mode
        GROUP BY owner HAVING count(*) >= 3
    ) SELECT CASE WHEN count(*) < 5 THEN jsonb_build_object('ready',false)
        ELSE jsonb_build_object('ready',true,'players',count(*),'matches',sum(games),
            'average_damage',round(avg(damage),1),'win_percent',round(100.0*sum(wins)/sum(games),1),
            'damage_deciles',percentile_disc(ARRAY[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9])
                WITHIN GROUP (ORDER BY round(damage)))
        END INTO result FROM players;
    RETURN result;
END;
$$;
REVOKE ALL ON FUNCTION public.matchlog_submit_v1(jsonb) FROM PUBLIC, authenticated;
REVOKE ALL ON FUNCTION public.matchlog_summary_v1(text,text,text) FROM PUBLIC, authenticated;
GRANT EXECUTE ON FUNCTION public.matchlog_submit_v1(jsonb) TO anon;
GRANT EXECUTE ON FUNCTION public.matchlog_summary_v1(text,text,text) TO anon;
NOTIFY pgrst, 'reload schema';
COMMIT;
