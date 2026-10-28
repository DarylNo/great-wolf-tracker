# Great Wolf booking API: endpoints

Captured from the public booking flow on greatwolf.com/niagara (browser network inspection, 2026-10-28).

## Availability (used by the collector)

```
GET https://prod.api.greatwolf.com/room-booking/v2/lodges/NIAGON/availability
```

| Param | Value | Notes |
|---|---|---|
| `arrival` | `YYYY-MM-DD` | |
| `departure` | `YYYY-MM-DD` | Collector always uses arrival + 1 (1-night stay) |
| `location` | `NIAGON` | Lodge code for Niagara |
| `numberOfAdults` | `2` | |
| `offerCode` | *(empty)* | No promo codes, by design |
| `kidsAges` | *(empty)* | |

- The browser also sends a `uuid` param. The call works without it, so the collector omits it.
- Works without cookies (credentials omitted). No login is involved.
- Public booking page for the same query: `https://www.greatwolf.com/niagara/plan?numberOfAdults=2&arrival=YYYY-MM-DD&departure=YYYY-MM-DD`

### Response

Top-level keys:

| Key | Notes |
|---|---|
| `suites[]` | One entry per suite type (27 for Niagara), **returned even when sold out** |
| `errors`, `errorCode` | `errorCode` is stored in `snapshots.error_code` |
| `offerType`, `promoDescription`, `planDescription` | |
| `isQualifyIdRequired`, `isIdMeValidationRequired` | |
| `pr`, `rr`, `defaultsApplied`, `defaultsReason`, `isProperty` | |

Each `suites[]` entry:

| Key | Notes |
|---|---|
| `suiteCode` | e.g. `SFSSB` (Family Suite, the reference suite) |
| `suiteBrand` | Display name |
| `suiteCategory` | `standard` / `themed` / `premium` / `cottage` |
| `availableRoomCount` | int, **0 = sold out**. Primary demand signal |
| `baseOccupancy`, `maxOccupancy`, `sortOrder` | |
| `rateFamilies[]` | See below |

Each `rateFamilies[]` entry: `title` (e.g. "Saver"), `subtitle`, `policyType`, `ratesDisplayRank`, `rates[]`.

Each `rates[]` entry:

| Key | Notes |
|---|---|
| `pricingKey` | Signed JWT booking token. **Never stored**: stripped from `raw_json` before saving |
| `rateCode`, `type` | |
| `rate`, `totalRate`, `totalNightlyRate` | Collector uses `totalNightlyRate` (falls back to `rate`) |
| `taxes`, `taxesAndFees{TAX}`, `advancedDeposit`, `roomAdjustmentTax` | |

### Live samples (1 night, 2 adults, captured 2026-10-28)

| Arrival | Rooms available | Suite types sold out | Min rate |
|---|---|---|---|
| Wed 2026-11-18 | 198 | 7 / 27 | $379.99 |
| Sat 2026-11-21 | 4 | 25 / 27 | $909.99 |
| Tue 2026-12-29 | 105 | 15 / 27 | $699.99 |

## Related endpoints (not used in V1)

- `GET https://prod.api.greatwolf.com/room-booking/v1/lodges/NIAGON/promo-banner`
- `GET https://prod.api.greatwolf.com/lodge-info/v1/lodges/NIAGON/applications/plan`
