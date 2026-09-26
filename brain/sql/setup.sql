-- Teddy's brain in Snowflake. Run once (python -m brain.setup_snowflake runs this for you).
-- Needs ACCOUNTADMIN for the cross-region line; everything else works with SYSADMIN.

-- Lets Cortex use models hosted in other regions (so llama/mistral are available everywhere).
ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';

CREATE WAREHOUSE IF NOT EXISTS TEDDY_WH WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE;
CREATE DATABASE IF NOT EXISTS TEDDY;
CREATE SCHEMA IF NOT EXISTS TEDDY.CORE;
USE WAREHOUSE TEDDY_WH;
USE SCHEMA TEDDY.CORE;

-- What the bear's camera saw, and where (0..1 camera coords).
CREATE TABLE IF NOT EXISTS SIGHTINGS (
  TS    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
  LABEL STRING,
  X     FLOAT,
  Y     FLOAT
);

-- Everything the bear did or heard: intents, gestures, moods, alerts...
CREATE TABLE IF NOT EXISTS EVENTS (
  TS   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
  KIND STRING,
  DATA VARIANT
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

-- Reference PDFs (first aid, CPR, homework help...). SSE encryption is required by PARSE_DOCUMENT.
CREATE STAGE IF NOT EXISTS DOCS
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

-- Stage for the Cortex Analyst semantic model.
CREATE STAGE IF NOT EXISTS MODELS ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

CREATE TABLE IF NOT EXISTS DOC_CHUNKS (
  FILE   STRING,
  TITLE  STRING,
  DOMAIN STRING,
  CHUNK  STRING
);
