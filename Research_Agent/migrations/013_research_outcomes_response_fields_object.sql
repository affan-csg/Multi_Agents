-- Keep the optional response-key diagnostic an object, as required by Company.
-- Preserve every legacy array item under {"missing": [...]} rather than dropping it.
UPDATE companies
SET research_outcomes = CASE
    WHEN research_outcomes IS NULL THEN '{}'::jsonb
    ELSE jsonb_build_object(
        '_legacy_research_outcomes', jsonb_build_object('value', research_outcomes),
        '_missing_response_fields', '{}'::jsonb
    )
END
WHERE research_outcomes IS NULL
   OR jsonb_typeof(research_outcomes) <> 'object';

UPDATE companies
SET research_outcomes = jsonb_set(
    research_outcomes,
    '{_missing_response_fields}',
    CASE
        WHEN jsonb_typeof(research_outcomes->'_missing_response_fields') = 'array'
            THEN jsonb_build_object('missing', research_outcomes->'_missing_response_fields')
        WHEN jsonb_typeof(research_outcomes->'_missing_response_fields') = 'object'
            THEN research_outcomes->'_missing_response_fields'
        WHEN research_outcomes ? '_missing_response_fields'
            THEN jsonb_build_object('legacy_value', research_outcomes->'_missing_response_fields')
        ELSE '{}'::jsonb
    END,
    true
)
WHERE jsonb_typeof(research_outcomes) = 'object'
    AND research_outcomes ? '_missing_response_fields'
    AND jsonb_typeof(research_outcomes->'_missing_response_fields') <> 'object';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'companies_research_outcomes_response_fields_object'
          AND conrelid = 'companies'::regclass
    ) THEN
        ALTER TABLE companies
            ADD CONSTRAINT companies_research_outcomes_response_fields_object
            CHECK (
                jsonb_typeof(research_outcomes) = 'object'
                AND (
                    NOT (research_outcomes ? '_missing_response_fields')
                    OR jsonb_typeof(research_outcomes->'_missing_response_fields') = 'object'
                )
            );
    END IF;
END
$$;