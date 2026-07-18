# Privacy

Photographer Image Archive is local-first and contains no telemetry, account system, advertising SDK, or analytics.

The app sends network requests only when the user starts discovery, opens a remote preview, or downloads a selected public file. Requests may reach Wikidata, Wikimedia Commons, the photographer website selected or discovered for the query, and the hosts referenced by that public website.

Archive records, notes, ratings, hashes, downloaded files, and the SQLite database stay in the archive directory selected by the user. Dynamic-page rendering uses a temporary isolated Chromium profile. It does not import the user's browser cookies, history, extensions, or signed-in profile.

The app blocks loopback, private, link-local, and other non-public network destinations. Dynamic rendering is restricted to the validated official-site host and does not provide a general-purpose browser network path.

Release packages and public source do not contain local archives, databases, browsing state, logs, screenshots, download history, or machine-specific paths.
