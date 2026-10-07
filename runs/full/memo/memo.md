<!-- run full | model claude-sonnet-5 | prompt memo_v2@d321a25a | automatic check passed: True | human review: PENDING -->

## Recommendation

Spotify should direct next quarter's product effort toward **billing/support**, specifically resolving the **Premium-only controls** problem (ISS-BILLING-PREMIUM_ONLY_CONTROLS). This single issue has a priority_score of 174,087 [C004] — the highest of any issue in the dataset — driven by 58,940 complaints [C001] and mean severity 2.953631 [C003]. Customers report being unable to perform basic actions (seek, skip, select songs) without paying for Premium, a pattern of perceived unfair restriction rather than a simple bug.

## Evidence

- ISS-BILLING-PREMIUM_ONLY_CONTROLS: 58,940 complaints [C001], severity sum 174,087 [C002], mean severity 2.953631 [C003], priority_score 174,087 [C004].
- Representative quotes (short fragments): "I need to buy Premium t[...]" (`8964aaea-f4c7-4b01-94e0-a76b992018fd`); "I literally cannot select songs and have to queue them to play anything" (`d8be33b8-6758-4ae7-9e3e-6e7d02163930`); "it just plays a different song when I try" (`2d1b5fa6-5d80-4616-9b65-f6e7738e9edf`).
- At the area level, billing_support has complaint_count 70,856, severity_sum 204,819, sev4plus 2,419, and cancellation count 12,766 — the highest cancellation count of any area, suggesting this friction point is tied to expressed intent to leave.
- For context, the area-level numbers across all six areas (billing_support, usability, none, playback, content, access) are reported in the pack; billing_support's top issue dominates the overall issue ranking.

## Alternatives considered

- **Access** (login failures, ISS-ACCESS-LOGIN): highest mean severity (3.900112 [C027]) and area mean severity 3.838968, but low volume (8,029 complaints [C025], area complaint_count 12,749) keeps its priority_score (31,314 [C028]) well below the top billing issue.
- **Usability** (ads, redesign, library issues): largest area by complaint_count (75,230) but its leading issue (ads, ISS-USABILITY-ADS) has priority_score only 85,318 [C012], under half of the billing issue's score.
- **Playback** (stops/skips, crashes): area has highest sev4plus count (11,374) and strong individual severities (mean 3.195182 area-level; 3.185217 for stops/skips [C015]), but its top issue's priority_score (44,042 [C016]) is far below billing's leading issue.

## Risks and limitations

These are self-selected public app store reviews, not a random sample of all users; no revenue, subscription-tier, or confirmed-churn data is available. Cancellation language reflects stated intent only, not verified cancellations. The pack shows 1,596 quarantined records and 0 pending, which are excluded from this analysis and may contain additional signal. We cannot attribute root technical causes beyond what reviewers state, and severity scores are derived, not independently validated against business impact.

## What to measure next quarter

Track whether complaint volume and severity for ISS-BILLING-PREMIUM_ONLY_CONTROLS decline following any changes to free-tier playback controls or clearer communication about Premium gating. Monitor cancellation-intent language rates in billing_support-tagged reviews, and watch for any complaint migration to usability or access areas that might indicate a shift in user frustration rather than a true resolution.
