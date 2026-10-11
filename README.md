# onboard

**LLM-assisted data onboarding for enterprise systems.**

Every enterprise system migration starts the same way: a customer hands over a messy legacy export, with cryptic column names, duplicate records, broken hierarchies, mixed date formats and free-text values, and someone spends days mapping and cleaning it by hand. `onboard` lets an LLM make the fuzzy calls (column mapping, value normalization, ambiguous records), wraps every decision in deterministic checks so it can't silently corrupt data, routes the edge cases to a human reviewer, and proves the whole thing with evals against a known answer key. The worked example is a CMMS/EAM migration (assets, PM schedules, work orders) for a synthetic mining site.

> Status: work in progress. The synthetic dataset and the legacy-export corruptor are in place; the validator, mapper and evals come next.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
onboard generate   # clean target-schema dataset -> data/clean
onboard corrupt    # legacy export + answer key -> data/legacy, data/ground_truth
pytest
```

All data is fully synthetic. MIT licensed.
