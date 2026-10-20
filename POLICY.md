# Policy on referral commissions

ablit may earn a commission when a new user creates a marketplace account through a project referral link. The commission must never decide which machine ablit recommends or rents. This file states that rule, the tests that enforce it, and the rules for the links themselves.

## The steering rule

The ranked list of machines is a function only of the offers the marketplaces return, the catalog profile of the model, and the flags the user passed. Referral data is not an input to searching, filtering or ranking, and offers and backend drivers have no referral fields.

The code is arranged so that the rule holds by construction. Referral IDs live in `cli/ablit/referral/ids.toml`, and only the `ablit.referral` package reads that file. The planner in `ablit.select` and the drivers in `ablit.backends` cannot import `ablit.referral`, so they have no way to read an ID.

## The five tests

All five tests are in `cli/tests/test_steering.py`, and CI runs them on every push and pull request.

1. Import contract. An import-linter contract in `cli/.importlinter` forbids `ablit.select` and `ablit.backends` from importing `ablit.referral`, directly or through another module. A second test reads the source files to confirm the same thing, and a third starts a fresh interpreter to confirm that importing the planner and the drivers does not load the referral package.
2. Same result with and without IDs. The planner gives the same filtered set, the same order and the same selection when referral IDs are set for every marketplace, when none are set, and when the offers from the paying marketplaces are shuffled.
3. Lowest total wins. A `hypothesis` property test generates offer sets, including sets larger than 64 offers in which an offer with cheap transfer sits outside the first 64 by hourly price. The first-ranked offer must have the lowest total among all offers that pass the filters. Vast returns at most 64 rows per query, so the test runs the search rule against a simulated marketplace that does the same.
4. Cheaper without a program beats dearer with one. A backend that has no referral program wins over a more expensive backend that has one.
5. Ties break on names. When two offers have the same total, the order is decided by backend name and then by offer ID, alphabetically, and never by input order or by whether a program exists.

A change that breaks any of these tests must not be merged, even if the change looks harmless.

## Rules for referral links

1. Links appear only in `ablit setup`, only for a marketplace where the user has no account, and only next to the disclosure. A CI test scans every file in the repository, and every string that contains `ref_id=` or `?ref=` must sit next to the words "Paid link:" that start the disclosure.
2. The disclosure says who earns the commission, how much it is, who pays it, that ablit adds nothing to the user's prices, and that commissions never affect which machine ablit picks. The user sees three choices of equal weight, which are to sign up with the paid link, to sign up directly, or to skip.
3. A user who already has an account enters a key and sees no referral link.
4. When the project has no ID for a marketplace, the paid choice is not offered at all.
5. ablit prints links and never opens a browser.
6. ablit has no telemetry.
7. `ablit setup --no-referral`, `ABLIT_NO_REFERRAL=1` and `referral = false` in the config file each turn off all referral links, and the setting is saved.
8. The README section named "Paid links" lists each commission rate and the date it was checked. The maintainer checks the rates on the program pages before each release.

## Reporting a violation

If you find a code path where a commission can change a ranking, open an issue, or use the private reporting route in SECURITY.md if you prefer.
