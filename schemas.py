"""Tool schemas: what the model reads to decide when to call each tool."""

STATEMENT_IMPORT = {
    "name": "statement_import",
    "description": (
        "Import a bank statement export (CSV or OFX file on this computer) so its subscriptions can be "
        "audited. Use it when the user gives you a statement file, says they downloaded transactions from "
        "their bank, or asks to find their subscriptions and no statement is imported yet. The bank format is "
        "detected automatically (BNP Paribas, Societe Generale, Credit Agricole, Boursobank, La Banque Postale, "
        "LCL, Caisse d'Epargne, Revolut, N26, Qonto, Wise, Monzo, Starling, Chase, Bank of America, Capital One, "
        "OFX 1.x/2.x, or any CSV with recognisable column names). Re-importing the same or an overlapping file "
        "is safe: known transactions are not duplicated. Returns the profile used and rows read, skipped and "
        "rejected with reasons. If the format is not recognised it returns the columns found: then ask the user "
        "which column holds the date, description and amount, have them set column_mapping, and retry with "
        "profile='custom'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the .csv or .ofx file (absolute, or relative to the session's working folder; ~ allowed)."},
            "profile": {"type": "string", "description": "Optional bank profile id to force (e.g. 'revolut', 'societe_generale', 'ofx', 'custom'). Leave empty to auto-detect."},
        },
        "required": ["path"],
    },
}

STATEMENT_AUDIT = {
    "name": "statement_audit",
    "description": (
        "Detect subscriptions and recurring charges in the imported statements and report the yearly cost, "
        "the top 10, price increases, duplicate charges, overlapping services (two streaming or cloud plans), "
        "subscriptions that stopped, refunds, payment plans and internal transfers left out. Use it after "
        "statement_import, whenever the user asks what they pay for, how much subscriptions cost, whether "
        "something went up, or for a monthly review. Each subscription has an id, a periodicity, a confidence "
        "between 0 and 1 and a status (active or probably_cancelled). Variable-amount bills (energy, phone usage) "
        "are labelled amount_kind='variable': say so when you report them. Report low-confidence items as "
        "'to check', not as facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "confidence_threshold": {"type": "number", "description": "Optional override of the confidence threshold (0-1, default from settings, 0.6)."},
            "from": {"type": "string", "description": "Optional start date YYYY-MM-DD of the period to audit."},
            "to": {"type": "string", "description": "Optional end date YYYY-MM-DD of the period to audit."},
            "as_of": {"type": "string", "description": "Optional reference date YYYY-MM-DD used to decide whether a subscription stopped (default: today). Use the statement's last day for an old export."},
        },
        "required": [],
    },
}

STATEMENT_CANCEL_PACK = {
    "name": "statement_cancel_pack",
    "description": (
        "Write a cancellation pack for chosen subscriptions into the plugin's data folder: a CSV summary, a "
        "Markdown file with one ready-to-send cancellation letter per subscription (subject, recipient, "
        "contract reference if known, next charge date, notice, letter text) and an .ics calendar with a "
        "reminder 7 days before each next charge. Use it when the user decides to cancel one or more "
        "subscriptions found by statement_audit. It never sends anything: tell the user where the files are "
        "and that they must review and send the letters themselves."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "Subscription ids from statement_audit (e.g. ['netflix', 'canal'])."},
            "language": {"type": "string", "enum": ["en", "fr"], "description": "Letter language. Default from settings (en)."},
            "formats": {"type": "array", "items": {"type": "string", "enum": ["csv", "md", "ics"]}, "description": "Files to write. Default: all three."},
            "as_of": {"type": "string", "description": "Leave unset in normal use (default: today). The letters are dated with this date and the next charge and reminders are computed from it, so do not pass the statement's last day here."},
        },
        "required": ["ids"],
    },
}

STATEMENT_PURGE = {
    "name": "statement_purge",
    "description": (
        "Permanently delete everything ghost-subscriptions stored: imported transactions, import history and "
        "generated cancellation packs. Use it only when the user explicitly asks to delete, wipe or forget "
        "their statement data. It cannot be undone; confirm with the user before calling it, then pass "
        "confirm=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {"confirm": {"type": "boolean", "description": "Must be true: the user confirmed the deletion."}},
        "required": ["confirm"],
    },
}

ALL = [STATEMENT_IMPORT, STATEMENT_AUDIT, STATEMENT_CANCEL_PACK, STATEMENT_PURGE]
