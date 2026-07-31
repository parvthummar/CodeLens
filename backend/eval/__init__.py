"""Retrieval evaluation for CodeLens.

Run the scripts as modules from `backend/`:

    python -m eval.validate_golden_set
    python -m eval.index_corpus          # makes real API calls
    python -m eval.run_eval PROJECT_ID

A package rather than loose scripts so the scoring logic can be imported and
tested — the metrics are what every claim in step 5 rests on.
"""
