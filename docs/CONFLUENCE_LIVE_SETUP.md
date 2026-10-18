# Turning Confluence on for the live demo

The code already works end-to-end (`coded_tools/sentinel/runbook_search.py` ->
`coded_tools/sentinel/confluence_runbooks.py`): once `SENTINEL_MODE=live` and the `CONFLUENCE_*` variables
below are set, `RunbookAgent` automatically searches your real Confluence space instead of the bundled
fixture, and the UI's Runbooks evidence badge switches from `FIXTURE` to `LIVE`. Nothing else needs to
change in code. This is almost entirely an account/credential setup step on your side.

## 1. Create a free Confluence Cloud site + space

1. Go to `https://www.atlassian.com/software/confluence` and start a free Cloud site (or reuse one you
   already have) with the account `harshakondaveeti05@gmail.com`.
2. Create one space. Any key works — e.g. key `SENTINEL`, name "Sentinel Runbooks".

## 2. Create one real runbook page

Create a page in that space with:

- **Title**: `Checkout Service Tax Lookup Failure Runbook`
- **Body** (paste as plain text/paragraphs — the exact wording below matters because
  `confluence_runbooks.py` extracts the "Recommended action" sentence with a regex, and `RunbookAgent`
  searches on "checkout-service", "tax", "country" and "pricing"):

```
Checkout Service Tax Lookup Failure Runbook

Symptoms: checkout-service returns 500 errors during order_total() when a customer's shipping country
has no entry in the tax rate table. Logs show a TypeError in app/pricing.py.

Root cause: a new deploy changed TAX_RATES without a safe default, so an unlisted country code looks
up None and the multiplication fails.

Recommended action: Patch app/pricing.py so TAX_RATES.get(country) falls back to a default rate instead
of returning None, open a pull request, and verify the fix in a sandbox before merging. Do not deploy a
hotfix directly to production.

Verification: Re-run a checkout order for an affected country after the fix merges and confirm the
order succeeds and the error rate returns to baseline.
```

## 3. Generate a Confluence API token

In Atlassian account settings -> Security -> API tokens, create a token scoped to that account. Copy it
once (it won't be shown again).

## 4. Set the real values in `.env`

Names already documented in `.env.example`:

```
CONFLUENCE_BASE_URL=https://<your-site>.atlassian.net
CONFLUENCE_EMAIL=harshakondaveeti05@gmail.com
CONFLUENCE_API_TOKEN=<the token from step 3>
CONFLUENCE_SPACE_KEYS=SENTINEL
```

Leave `CONFLUENCE_CLOUD_ID` and `CONFLUENCE_RUNBOOK_LABEL` unset unless you need them.

## 5. Verify

```bash
python scripts/live_demo.py preflight
```

now does a real reachability check (`confluence_status()` in `scripts/live_demo.py`), not just an
env-var presence check — it will say `[ok] Confluence reachable` once the site/space/token are right, or
tell you exactly what's wrong (missing field vs. an HTTP failure) if not.

Then trigger a real investigation and confirm the Runbooks evidence card in the UI shows `LIVE` with a
link into your new Confluence page.
