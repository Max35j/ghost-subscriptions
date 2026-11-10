# Changelog

## 1.0.0

First release: `statement_import`, `statement_audit`, `statement_cancel_pack`, `statement_purge`, the `/subs`
command and the `monthly-audit` skill.

Fixed after the first end-to-end run in a real Hermes session:

- **Cancellation pack never targets a past charge.** When the statement ends a few days before today, the
  audit's `next_expected` (last charge + one period) can already be behind us. The pack now rolls it forward
  to the next charge still ahead, by calendar period, and says so in `warnings`.
- **`statement_purge` leaves the data folder empty.** The emptied `data.db` (and its `-wal` / `-shm` files) is
  removed as well; the result reports `database_removed`.
- Skill: answer follow-up questions from the audit result instead of re-parsing the raw file, and quote
  dates as returned.
- **A variable bill is no longer split into fake "plans".** A cable or energy bill whose amounts spread
  over several 10 %-wide clusters was treated as several concurrent plans and then dropped. The split now
  only happens when the charges are not one regular series as a whole and each cluster is one.
- **Label + detail columns are not doubled.** When a bank's detail column repeats the short label
  (Société Générale layout), the detail is used alone: cleaner merchant names, and refunds match their charge.
- **Cancellation letters are dated today.** The pack's `as_of` description now tells the model to leave it
  unset; before, a model could pass the statement's last day and the letters came out back-dated.
- `/subs` opens with a 👻. Banner and screenshots of a real session added under `assets/`.

After a critical review of the engine:

- **A price rise seen on the last 2 to 4 charges is one alert.** It used to be reported twice (as a step and as
  a latest-charge jump), which doubled the yearly impact shown by `/subs`.
- **A plain `Date, Description, Amount` file is read as generic.** A bank profile now needs at least one of
  its distinctive columns in the header; before, such a file could be reported as a Wise export.
- **American Express profile** (`amex`, purchases are positive amounts). A file where nearly every amount is
  positive and no card profile matched now carries a warning that says how to flip the sign.
- **50,000 rows in about two seconds.** The alias table is indexed and cached, and refunds, transfers and
  duplicates are matched without pairwise loops. Results are identical on the test ledgers and on 400 random ones.
- A truncated UTF-16 export no longer raises while decoding.
- The alias table no longer lists two AI services (290 aliases, 134 services).
