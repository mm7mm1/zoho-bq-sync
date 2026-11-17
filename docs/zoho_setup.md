# Zoho developer org setup

## 1. Org

Sign up for a Zoho CRM **developer edition** (free). Zoho creates it in the data center where
your Zoho account lives, whichever sign-up page you start from. An EU account gets
`developer.zoho.eu/crm` and the `developer.zohoapis.eu` API host: set `ZOHO_DC=eu-developer`.
A US account gets `developer.zohoapis.com`: set `ZOHO_DC=us-developer`. The token response
reports `www.zohoapis.*` for these orgs, which answers 401. If your org is a regular edition,
use its DC (`eu`, `in`, ...).

## 2. Custom module `Wedding Bookings`

Setup -> Customization -> Modules and Fields -> Create New Module.
Singular "Wedding Booking", plural "Wedding Bookings". The API name must be
`Wedding_Bookings` (check it under Setup -> Developer Hub -> APIs -> API names).

| Field label | Type | API name (must match) |
|---|---|---|
| Wedding Booking Name (default) | Single Line | `Name` |
| Partner 1 Name | Single Line | `Partner_1_Name` |
| Partner 2 Name | Single Line | `Partner_2_Name` |
| Contact Email | Email | `Contact_Email` |
| Ceremony Type | Pick List: Civil Marriage, Symbolic Ceremony, Religious Ceremony, Vow Renewal | `Ceremony_Type` |
| Destination Country | Single Line | `Destination_Country` |
| Ceremony City | Single Line | `Ceremony_City` |
| Wedding Date | Date | `Wedding_Date` |
| Guest Count | Number | `Guest_Count` |
| Package Price | Currency | `Package_Price` |
| Booking Status | Pick List: Inquiry, Documents Pending, Confirmed, Completed, Cancelled | `Booking_Status` |
| Documents Verified | Checkbox | `Documents_Verified` |
| Consultation At | Date/Time | `Consultation_At` |

## 3. Subform `Booked Services`

In the same layout, drag a **Subform** onto the page. Label it "Booked Services", which gives
the API name `Booked_Services`. Add these fields:

| Field label | Type | API name |
|---|---|---|
| Service Name | Single Line | `Service_Name` |
| Service Category | Pick List: Legal & Registration, Document Translation, Venue, Photography, Floral & Decor, Catering, Transport, Officiant | `Service_Category` |
| Vendor Name | Single Line | `Vendor_Name` |
| Service Date | Date | `Service_Date` |
| Quantity | Number | `Quantity` |
| Unit Price | Currency | `Unit_Price` |
| Service Confirmed | Checkbox | `Service_Confirmed` |

Do **not** create `Apostille Deadline` yet. You add it live during the demo.

## 4. OAuth Self Client

1. Go to https://api-console.zoho.com -> Add Client -> **Self Client**, and copy the Client ID
   and Secret.
2. Generate Code with scope
   `ZohoCRM.modules.ALL,ZohoCRM.settings.modules.READ,ZohoCRM.settings.fields.READ`,
   duration 10 minutes, and a description.
3. Exchange it right away. The refresh token goes to a 0600 file and is never printed:

```bash
export ZOHO_CLIENT_ID=1000.XXXX ZOHO_CLIENT_SECRET=xxxx
python scripts/exchange_grant_token.py --dc eu-developer --code 1000.yyyy --out secrets/zoho-oauth.json
```

For production, issue a separate read-only token for the pipeline
(`ZohoCRM.modules.READ,ZohoCRM.settings.modules.READ,ZohoCRM.settings.fields.READ`). The write
scope is only for the seed script.

## 5. Validate and seed

```bash
python scripts/seed_demo_data.py check
python scripts/seed_demo_data.py create --count 8
python scripts/seed_demo_data.py list
```

`create --dry-run` prints the generated JSON without calling Zoho.
