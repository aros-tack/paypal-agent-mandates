# Security notes

* Sandbox only. `live_check` refuses to run against `api-m.paypal.com`; the PayPal client defaults to the sandbox host.
* Credentials are read from the environment at call time and are never logged, stored in the ledger, or written to evidence files.
* The agent has no approve tool. Capture requires PayPal to report the order APPROVED for the exact recorded amount and currency.
* Item text is treated as untrusted input by the AI reviewer; the reviewer can only add restrictions.
* The audit log is hash-chained (`Ledger.verify_chain`); webhook events are accepted only after PayPal's `verify-webhook-signature` and are de-duplicated by event id.
