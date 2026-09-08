# Part C: Productionising the Pipeline

## How I would load and schedule this on GCP + BigQuery

- **Ingestion**: Source files (xlsx from client systems) would land in a GCS bucket via a scheduled upload or client SFTP → GCS transfer. Cloud Functions or Eventarc would trigger the pipeline on new file arrival.
- **Transformation**: The Python pipeline would run inside a **Cloud Run job** (containerised via Docker), keeping it serverless and cost-efficient for short batch workloads. For heavier orchestration, **Cloud Composer (Airflow)** would manage the DAG: ingest → clean → validate → transform → load.
- **Loading to BigQuery**: The final `bookings_clean` table would be written to BigQuery using the BigQuery Storage Write API (`google-cloud-bigquery` Python client). The quarantine table would land in a separate dataset (`flux_quarantine`).
- **Scheduling**: Cloud Scheduler triggers the Cloud Run job (or Composer DAG) on a nightly cron, e.g. `0 3 * * *`.
- **BigQuery table design**: Partition `bookings` by `check_in` (DATE) and cluster by `property_id` — this makes monthly trend queries and per-property aggregations fast and cheap.

---

## How I would monitor pipeline health and catch data-quality problems early

- **Pipeline-level logging**: Each stage emits structured JSON logs to Cloud Logging. Alerts fire (via Cloud Monitoring) if the job errors or if quarantine row count exceeds a threshold (e.g. >5% of total rows).
- **Row-count assertions**: After each stage, assert that the output row count is within expected bounds (e.g. never fewer than 150 clean bookings for a full-season extract). Fail fast if violated.
- **Data-quality checks as BigQuery views**: The SQL DQ checks (Q5) are materialised as a scheduled query that runs after each load. If any check returns `issue_count > 0`, a Pub/Sub message triggers an alert to the data team.
- **dbt (optional next step)**: In a mature setup, dbt tests (`not_null`, `unique`, `accepted_values`, `relationships`) would run after each load, with test failures blocking downstream reporting models.
- **Quarantine monitoring dashboard**: A simple Looker Studio report on the `flux_quarantine` dataset lets the team review rejected records daily without touching code.

---

## Version control and CI/CD

- **Git**: The pipeline repo follows trunk-based development. All changes go via pull request; main branch is protected.
- **Structure**:
  ```
  flux-pipeline/
    pipeline.py
    sql/queries.sql
    tests/test_pipeline.py    ← unit tests (bonus)
    Dockerfile
    requirements.txt
    .github/workflows/ci.yml
  ```
- **CI (GitHub Actions)**:
  - On every PR: run `pytest` (unit tests for clean/validate functions), lint with `ruff`, check SQL syntax.
  - On merge to main: build and push the Docker image to Artifact Registry, then trigger a Cloud Run job in the staging environment.
- **CD**: A manual approval gate before promoting to production. The production Cloud Run job always uses a pinned image tag (not `latest`).
- **Secrets**: No credentials in code. Cloud Run uses a service account with least-privilege IAM roles (BigQuery Data Editor, GCS Object Viewer).

---

## Main assumptions and trade-offs given the time limit

- **Currency rates are static**: The fx_rates file contains a single rate per currency. In production I'd version these by date and join on the booking's created_at date to get the rate at time of booking.
- **"Implausible" thresholds are heuristic**: MAX_NIGHTS=90 and MAX_GUESTS=30 are reasonable starting points for luxury lodge bookings. In production these would be property-specific and reviewed with the client.
- **Quarantine vs. drop**: I quarantine (not silently drop) all bad records so the team can investigate and potentially recover them. A record like B9004 (missing num_guests) might be fixable from the source system.
- **No deduplication on content**: Duplicate booking_ids with different values are quarantined. In production, a more sophisticated dedup (e.g. keep the most recently updated record) would require `updated_at` metadata from the source system.
- **Mixed dates in same column**: The `DD/MM/YYYY` vs. Excel-serial mix is a real-world problem. My parser handles both, but a production fix would push back on the source to standardise before export.
- **Single-currency FX**: All ZAR conversions use the single provided rate. For multi-period reporting, historical FX rates would be needed.
