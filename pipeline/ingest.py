"""Stage 1 - Ingest. Code only, no model calls.
Reads every row of the full CSV with a quote-aware parser, profiles quality, verifies the
course manifest, and records the identity of the declared samples."""
from __future__ import annotations

import csv
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from .common import RAW, RUNS, RunLog, load_json, now, read_csv, sha256_file, write_json, write_csv

FULL = RAW / "spotify_reviews_18months.csv"
SAMPLES = {
    "golden": RAW / "golden_50_to_label.csv",
    "checkpoint": RAW / "checkpoint_500.csv",
    "analysis": RAW / "analysis_10000.csv",
}
EXPECTED_FIELDS = ["review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp"]
OUT = RUNS / "ingest"


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo), 1)


def main():
    log = RunLog(OUT, "ingest")
    log.event("start", file=str(FULL.name))
    manifest = load_json(RAW / "manifest.json")

    size = FULL.stat().st_size
    checksum = sha256_file(FULL)
    physical_lines = sum(1 for _ in open(FULL, "rb"))

    ids, dup_ids = set(), []
    ratings, months = Counter(), Counter()
    missing = Counter()
    lengths = []
    empty_text_ids, bad_rating_ids, bad_ts_ids = [], [], []
    multiline = 0
    min_ts = max_ts = None
    likes_total = 0
    versions = Counter()

    with open(FULL, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        n = 0
        for row in reader:
            n += 1
            rid = row["review_id"]
            if rid in ids:
                dup_ids.append(rid)
            ids.add(rid)
            for k in EXPECTED_FIELDS:
                if row.get(k) is None or row[k].strip() == "":
                    missing[k] += 1
            text = row["review_text"] or ""
            if text.strip() == "":
                empty_text_ids.append(rid)
            if "\n" in text or "\r" in text:
                multiline += 1
            lengths.append(len(text))
            r = row["review_rating"]
            if r in {"1", "2", "3", "4", "5"}:
                ratings[r] += 1
            else:
                bad_rating_ids.append(rid)
            try:
                ts = datetime.strptime(row["review_timestamp"], "%Y-%m-%d %H:%M:%S")
                months[ts.strftime("%Y-%m")] += 1
                min_ts = ts if min_ts is None or ts < min_ts else min_ts
                max_ts = ts if max_ts is None or ts > max_ts else max_ts
            except (ValueError, TypeError):
                bad_ts_ids.append(rid)
            try:
                likes_total += int(row["review_likes"] or 0)
            except ValueError:
                pass
            if row["app_version"]:
                versions[".".join(row["app_version"].split(".")[:3])] += 1
            if n % 100_000 == 0:
                log.event("progress", rows=n)

    lengths.sort()
    profile = {
        "records": n,
        "physical_lines_including_header": physical_lines,
        "note_physical_lines": "Physical lines exceed records because review_text contains embedded line breaks.",
        "reviews_with_embedded_newlines": multiline,
        "header": header,
        "duplicate_review_ids": len(dup_ids),
        "empty_review_text": len(empty_text_ids),
        "empty_review_text_ids": empty_text_ids,
        "missing_values_by_field": {k: missing.get(k, 0) for k in EXPECTED_FIELDS},
        "missing_app_version": missing.get("app_version", 0),
        "invalid_rating": len(bad_rating_ids),
        "invalid_timestamp": len(bad_ts_ids),
        "date_range": {"first": str(min_ts), "last": str(max_ts), "timezone": "unspecified in source"},
        "reviews_by_month": dict(sorted(months.items())),
        "partial_months": ["2022-05 (starts 05-17)", "2023-11 (ends 11-15)"],
        "reviews_by_rating": dict(sorted(ratings.items())),
        "review_likes_total": likes_total,
        "text_length_chars": {
            "min": lengths[0], "p10": pct(lengths, .10), "p25": pct(lengths, .25), "median": pct(lengths, .5),
            "mean": round(statistics.fmean(lengths), 1), "p75": pct(lengths, .75), "p90": pct(lengths, .90),
            "p99": pct(lengths, .99), "max": lengths[-1],
        },
        "top_app_versions_major_minor_patch": dict(versions.most_common(10)),
    }

    # Compare with the course manifest.
    mprof = manifest["profile"]
    checks = {
        "sha256_matches_manifest": checksum == manifest["files"]["spotify_reviews_18months.csv"]["sha256"],
        "bytes_match_manifest": size == manifest["files"]["spotify_reviews_18months.csv"]["bytes"],
        "records_match_manifest": n == mprof["records"],
        "empty_text_matches_manifest": len(empty_text_ids) == mprof["empty_review_text"],
        "missing_app_version_matches_manifest": missing.get("app_version", 0) == mprof["missing_app_version"],
        "no_duplicate_ids": len(dup_ids) == 0 == mprof["duplicate_review_ids"],
        "months_match_manifest": dict(sorted(months.items())) == manifest["reviews_by_month"],
        "ratings_match_manifest": dict(sorted(ratings.items())) == manifest["reviews_by_rating"],
    }

    # Samples: identity, membership in full file, subset/disjointness.
    sample_ids, sample_info = {}, {}
    for name, path in SAMPLES.items():
        rows = read_csv(path)
        sids = [r["review_id"] for r in rows]
        sample_ids[name] = sids
        mf = manifest["files"][path.name]
        sha = sha256_file(path)
        sample_info[name] = {
            "file": path.name, "rows": len(rows), "sha256": sha,
            "sha256_matches_manifest": sha == mf["sha256"],
            "all_ids_in_full_file": all(i in ids for i in sids),
            "unique_ids": len(set(sids)) == len(sids),
            "empty_texts": sum(1 for r in rows if not r["review_text"].strip()),
        }
        write_csv(OUT / f"sample_ids_{name}.csv", [{"review_id": i} for i in sids], ["review_id"])
    a, c, g = set(sample_ids["analysis"]), set(sample_ids["checkpoint"]), set(sample_ids["golden"])
    sample_checks = {
        "checkpoint_subset_of_analysis": c <= a,
        "golden_disjoint_from_analysis": not (g & a),
        "golden_disjoint_from_checkpoint": not (g & c),
    }

    accounting = {
        "full_file_records": n,
        "declared_for_model_analysis": len(a),
        "sampled_out_not_sent_to_model": n - len(a) - len(g),
        "golden_eval_only": len(g),
        "excluded_from_analysis_before_model": 0,
        "note": ("The course sampler drew analysis/golden from unique nonempty valid-rating reviews, so the 13 "
                 "empty-text rows are in 'sampled_out'. Missing app_version does not exclude a record."),
    }
    assert accounting["full_file_records"] == (accounting["declared_for_model_analysis"]
                                               + accounting["sampled_out_not_sent_to_model"]
                                               + accounting["golden_eval_only"])

    from .db import build_reviews_db
    database = build_reviews_db(FULL, sample_ids)
    checks["sqlite_row_count_matches"] = database["rows_in_db"] == n
    log.event("sqlite_loaded", **database)

    report = {"generated_at": now(), "source_file": FULL.name, "bytes": size, "sha256": checksum,
              "profile": profile, "manifest_checks": checks, "samples": sample_info,
              "sample_checks": sample_checks, "record_accounting": accounting, "database": database}
    write_json(OUT / "ingestion_report.json", report)

    data_manifest = {
        "generated_at": now(),
        "source": manifest["source"],
        "window": manifest["window"],
        "sampling": manifest["samples"],
        "files": {FULL.name: {"bytes": size, "sha256": checksum, "records": n},
                  **{v["file"]: {"rows": v["rows"], "sha256": v["sha256"]} for v in sample_info.values()}},
        "sample_id_lists": {k: f"runs/ingest/sample_ids_{k}.csv" for k in SAMPLES},
        "default_analysis_input": "analysis_10000.csv",
    }
    write_json(OUT / "data_manifest.json", data_manifest)

    ok = all(checks.values()) and all(sample_checks.values()) and all(
        s["sha256_matches_manifest"] and s["all_ids_in_full_file"] and s["unique_ids"] for s in sample_info.values())
    log.event("done", records=n, all_checks_pass=ok)
    for k, v in {**checks, **sample_checks}.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    if not ok:
        raise SystemExit("Ingestion checks failed - see runs/ingest/ingestion_report.json")


if __name__ == "__main__":
    main()
