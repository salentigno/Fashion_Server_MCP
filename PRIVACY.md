# Privacy Policy — Fashion Trends MCP

**Last updated:** May 2026

## 1. About this project

Fashion Trends MCP is a private, internal analytical tool used by a small number of fashion industry consultants to analyze public trend data from multiple sources. It is not a consumer-facing product and is not distributed publicly.

## 2. Data we access

This application accesses **only publicly available data** through the official APIs of the following providers:

- Pinterest Trends API — public keyword trend data
- Bing Webmaster API — aggregate keyword search volume
- eBay Browse API — public marketplace listings
- Etsy Open API v3 — public listings and shop data
- Wikipedia Pageviews API — public article view counts
- GDELT Project — public news coverage data
- Reddit API (PRAW) — public posts and comments
- DuckDuckGo Autocomplete — public search suggestions

We do not access private user data, private messages, accounts, or any information that is not publicly available through the official APIs.

## 3. Data we store

The application uses only **short-lived in-memory caching** to reduce redundant API calls and improve performance.

- Cache lifetime: maximum 1 hour
- Cache scope: in-memory only, not persisted to disk
- No user data is stored
- No third-party tracking is used
- No analytics are collected

When the server restarts, all cached data is cleared.

## 4. Data we share

We do not share, sell, transfer, or distribute any data obtained through these APIs to third parties. The data is used exclusively for internal trend analysis by authorized users.

## 5. User accounts

This application does not create user accounts. It does not require Pinterest users to authorize their personal accounts. We only consume aggregated, public trend data.

## 6. Compliance

This application complies with the Terms of Service of each integrated API provider, including:

- [Pinterest Platform Policy](https://developers.pinterest.com/terms/)
- [Bing Webmaster Terms](https://www.microsoft.com/en-us/legal/intellectualproperty/copyright)
- [eBay Developer Agreement](https://developer.ebay.com/api-docs/static/developer-license-agreement.html)
- [Etsy API Terms of Use](https://www.etsy.com/legal/api/terms)
- [Reddit Developer Terms](https://www.redditinc.com/policies/data-api-terms)
- [GDELT Terms of Use](https://www.gdeltproject.org/about.html)

## 7. Data retention and deletion

Since no user data is stored persistently, there is no retention policy beyond the in-memory cache described above.

If a user wishes to request that any cached data referencing their public content be cleared, they may contact us at the email below.

## 8. Security

The application runs in a private environment with restricted access. API credentials are stored in environment variables, never committed to source control, and only accessible to authorized administrators.

## 9. Changes to this policy

This policy may be updated to reflect changes in the application or in third-party API requirements. The latest version will always be available at the same URL.

## 10. Contact

For any questions regarding this Privacy Policy or how the application uses data, please contact:

**[tu_email@ejemplo.com]**

Replace the email above with your actual contact email before publishing.
