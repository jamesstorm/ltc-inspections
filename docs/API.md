# Data API

The data comes from the Ontario Ministry of Long-Term Care public reporting site:
`https://publicreports.mltc.gov.on.ca` (successor to the old `publicreporting.ltchomes.net`,
which now redirects there).

It is a **Salesforce Experience Cloud** site. The pages are client-side rendered, but the
underlying data is served by a **public, unauthenticated "guest" Apex API** — the same
requests the site's own UI makes. This tool replays those requests.

## Endpoint

```
POST https://publicreports.mltc.gov.on.ca/webruntime/api/apex/execute?language=en-CA&asGuest=true&htmlEncode=false
Content-Type: application/json
Origin: https://publicreports.mltc.gov.on.ca
Referer: https://publicreports.mltc.gov.on.ca/
```

No authentication. The request body is a JSON envelope:

```json
{
  "namespace": "",
  "classname": "@udd/01pOH000000GbLN",
  "method": "<methodName>",
  "isContinuation": false,
  "params": { },
  "cacheable": false
}
```

> ⚠️ `classname` is a Salesforce Apex class id. It is stable today but **can change if the
> site is redeployed**. If calls start failing (unknown class), re-discover it by opening the
> site and reading the XHR calls in DevTools → Network — they target
> `.../webruntime/api/apex/execute` and carry this value in the body.

## Methods

### 1. `getHomeDetailsbyNameOrLocarion` — search homes
Find homes by name or location (city).

Request: `params = { "homeNameOrLocation": "Kitchener" }`
Response: `returnValue` is an array of `{ Id, Home_Name__c, Home_Number__c }`.

`Home_Number__c` is the home's identifier used by the next two methods. (Note the
"Locarion" spelling is in the source, not a typo on our side.)

### 2. `getHomeDetails` — list a home's reports
Request: `params = { "accountNumber": "<Home_Number>" }`
Response: `returnValue.reportList` is a JSON-**encoded string** containing an array of
report records:
`{ Id, Home_Name__c, Home_City__c, Home_Postal_Code__c, Inspection_Number__c,
Inspection_Type__c, Date_LR_Issued__c, Report_Type__c, ... }`.

`Id` is the inspection id used by method 3; `Date_LR_Issued__c` is the report issue date.

### 3. `getHomeInspectionDetails` — full report
Request: `params = { "inspectionId": "<Id>", "homeId": "<Home_Number>", "usePublish": true }`
Response: `returnValue.inspectionDetails` — one record with the report content, including:

- `WN_Details__c` — **the findings** (written notifications, compliance orders, and AMP
  fines) as HTML. This is what we parse into per-finding records.
- `Intake_Details__c` — onsite dates and what triggered the inspection.
- `Intake_Type__c` — e.g. `Critical Incident`, `Complaint`, `Proactive Compliance Inspection`, `Follow up`.
- `Inspection_Protocols__c` — the protocol areas examined (IPAC, falls, skin & wound, …).
- `Complied__c` — previously-issued orders found to be in compliance.
- `Home_Name__c`, `Home_City__c`, `Home_Postal_Code__c`, `Licensee__c`,
  `Inspection_Number__c`, `Date_LR_Issued__c`.

## robots.txt

The site serves the Salesforce default robots.txt: `User-agent: *` / `Allow: /`. It
explicitly permits automated access to the site (only password-reset is disallowed).

## Etiquette

The tool is deliberately polite:

- **Rate-limited** — a 0.25 s delay between report-detail calls (configurable via `DELAY`).
- **Incremental** — reports are cached in SQLite; a re-fetch only pulls reports not
  already cached, so refreshes are cheap rather than full re-crawls.
- **Identifiable** — a descriptive `User-Agent`.

See [`methodology.md`](methodology.md) for how the fetched data is turned into the metrics,
and the project README for the acceptable-use note.
