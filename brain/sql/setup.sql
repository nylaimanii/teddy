-- Teddy's brain in Snowflake. `python -m brain.setup_snowflake` runs this; {WH} = SNOWFLAKE_WAREHOUSE from .env.

-- Let Cortex route to models hosted in other regions (needs ACCOUNTADMIN; already on for our account).
ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';

CREATE DATABASE IF NOT EXISTS TEDDY;
CREATE SCHEMA IF NOT EXISTS TEDDY.CORE;
USE WAREHOUSE {WH};
USE SCHEMA TEDDY.CORE;

-- What the bear's camera saw, where (0..1 camera coords, x=0 is the bear's left), and the saved frame.
CREATE TABLE IF NOT EXISTS SIGHTINGS (
  LABEL      STRING,
  X          FLOAT,
  Y          FLOAT,
  W          FLOAT,
  H          FLOAT,
  FRAME_PATH STRING,
  TS         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Everything the bear did or heard: intents, gestures, moods, alerts...
CREATE TABLE IF NOT EXISTS EVENTS (
  KIND STRING,
  DATA VARIANT,
  TS   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Flat view for Cortex Analyst / text-to-SQL (no VARIANT digging needed).
CREATE OR REPLACE VIEW EVENTS_FLAT AS
SELECT TS,
       TO_DATE(TS)                    AS DAY,
       KIND,
       DATA:source::STRING            AS SOURCE,
       DATA:intent::STRING            AS INTENT,
       DATA:object::STRING            AS OBJECT,
       DATA:score::INT                AS MOOD_SCORE,
       DATA:mood::STRING              AS MOOD,
       DATA:status::STRING            AS STATUS,
       DATA:heart_rate::FLOAT         AS HEART_RATE,
       DATA:text::STRING              AS TEXT,
       DATA:reply::STRING             AS REPLY
FROM EVENTS;

-- Reference PDFs. Server-side encryption is required by AI_PARSE_DOCUMENT.
CREATE STAGE IF NOT EXISTS DOCS
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

-- Camera frames behind sightings (the bear's photo memory).
CREATE STAGE IF NOT EXISTS FRAMES
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

-- Cortex Analyst semantic model.
CREATE STAGE IF NOT EXISTS MODELS ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

-- Mood check-ins: a feeling label + time, never an image. Parent-only.
CREATE TABLE IF NOT EXISTS MOODS (
  TS       TIMESTAMP_NTZ,
  KID      STRING,
  LABEL    STRING,   -- calm | happy | sad | frustrated | upset
  RAW      STRING,   -- what senses/ reported (neutral, angry, ...)
  CONF     FLOAT,
  ACTIVITY STRING    -- what Teddy was helping with at the time
);
