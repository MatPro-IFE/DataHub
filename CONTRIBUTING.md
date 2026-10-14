# Contributing to DataHub

Thank you for considering a contribution. Bug reports, questions, documentation fixes and code changes are all welcome.

## Reporting bugs and asking for features

Open an issue with the matching template. For a bug, include the DataHub version, how you run it (Compose, host OS), the relevant part of your project schema with names changed if they are confidential, and the log output of the failing service (`docker compose logs dashboard ml-api`).

Do not report security problems in a public issue. Follow [SECURITY.md](SECURITY.md) instead.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Keep DataHub generic: anything specific to one project belongs in a project schema, not in the code. If a change needs a new schema option, document it in `docs/SCHEMA_GUIDE.md` and use it in `config/schema.example.yaml`.
3. Run the checks that CI runs:

   ```bash
   pip install -e ".[demo,fields,test]" reuse bandit pip-audit
   python -m pytest -q tests
   reuse lint
   bandit -q -r dashboard ml-api datahub_sdk -ll
   ```

   For changes to the services, also start the stack and run `python examples/smoke_test.py --data demo_data` (see the README).
4. Add an entry to `CHANGELOG.md` and open a pull request that explains what the change does and why.

## Licence of contributions

DataHub is released under the Apache License 2.0. By submitting a contribution you agree that it is licensed under the same terms (section 5 of the licence). Documentation in `docs/` is licensed under CC BY 4.0. New source files should start with an SPDX header:

```python
# SPDX-FileCopyrightText: 2026 <your name or organisation>
#
# SPDX-License-Identifier: Apache-2.0
```

## Code of conduct

Be respectful and constructive in issues, reviews and discussions. Maintainers may remove comments or block accounts that are abusive.
