---
id: data_platform
title: Data platform ownership
use_when: [data platform, data engineering, warehouse, snowflake, redshift, analytics, pipelines, etl]
---
Acme Learning's analytics ran on an on-premises Postgres warehouse that took nightly jobs eleven hours to finish and failed about once a week. I led the migration to Snowflake with a small data engineering team: we rebuilt the pipelines as tested, version-controlled transformations, ran the old and new systems side by side for a quarter, and retired the old one only when every report matched. Nightly loads now finish in under an hour, failures are rare enough to be investigated individually, and analysts self-serve the data they used to request through tickets.
