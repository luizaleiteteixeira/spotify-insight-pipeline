# Assignment 5 — Spotify Insight Pipeline dataset

## Start here

1. Unzip `spotify-insight-dataset.zip`.
2. Read `checkpoint_500.csv` with a CSV parser for the Class 6 development run.
3. Hand-label `golden_50_to_label.csv`. These 50 reviews are separate from the development and analysis samples. Do not place them in your prompts.
4. In Class 7, ingest and profile `spotify_reviews_18months.csv` (660,622 rows; 97,400,616 bytes), then coordinate model processing of `analysis_10000.csv`.
5. Follow the full assignment brief on the course site: `assignment5.html`.

The CSV size is 97.4 decimal MB (92.9 MiB). The ZIP download is smaller. Review text may contain embedded line breaks; physical line counts are not row counts.

## Provenance

- Creator: BwandoWando.
- Source: https://www.kaggle.com/datasets/bwandowando/3-4-million-spotify-google-store-reviews
- Version: 2, published November 17, 2023.
- Publisher-listed license: CC0: Public Domain.
- Pinned source download: https://www.kaggle.com/api/v1/datasets/download/bwandowando/3-4-million-spotify-google-store-reviews?datasetVersionNumber=2
- Source archive: 273,598,020 bytes; original CSV: 655,836,189 bytes; 3,377,423 rows.

This course extract includes all source rows dated 2022-05-17 (inclusive) through 2023-11-17 (exclusive). The last observed review is dated 2023-11-15. The original row index, author names, and author IDs were removed. `author_app_version` was renamed to `app_version`; other retained values are unchanged. Review text itself has not been redacted or translated.

## Fields

- `review_id`: original stable review ID.
- `review_text`: original customer text.
- `review_rating`: original star rating, 1–5; not an assigned severity label.
- `review_likes`: helpful-vote count in the source snapshot.
- `app_version`: source app-version string; often missing.
- `review_timestamp`: original timestamp; timezone unspecified.

The full extract has 13 empty review texts, 159,701 missing app versions, and no repeated review IDs. All these rows remain in the full CSV. Samples use unique nonempty reviews with valid ratings. The 500-review checkpoint is a subset of the 10,000-review analysis; the golden 50 is disjoint from both. Sample order is determined by SHA-256 with the fixed seed recorded in `manifest.json`.

## Limits

This is a historical snapshot of self-selected public app reviews. It contains no account revenue, plan tier, confirmed churn, or complete customer population. Missing versions and unclear or unsupported-language text need explicit handling. A review expressing cancellation intent does not prove that cancellation happened. Counts derived from the 10,000-review sample must be described as sample counts. Do not include synthetic evaluation cases in business results.

## Reproduce and verify

Download the pinned source ZIP above, then run:

```sh
python prepare_dataset.py /path/to/spotify-source.zip --output data
```

The script uses only Python's standard library. `manifest.json` records source and output SHA-256 checksums, exact byte sizes, counts, filtering, and sampling rules. No model calls are made during preparation.

Full CSV SHA-256:

```text
1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6
```
